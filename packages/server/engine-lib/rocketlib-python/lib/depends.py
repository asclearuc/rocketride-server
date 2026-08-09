# =============================================================================
# MIT License
#
# Copyright (c) 2026 Aparavi Software AG
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
# =============================================================================

"""
Dependency management for RocketRide Engine.

Two modes of operation:
  - Library mode: import and call depends(requirements_file)
  - Main mode: engine depends.py [uv pip arguments]
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from glob import glob
from typing import Optional

# Conditional imports for cross-platform file locking (both are built-in)
if os.name == 'nt':
    import msvcrt
else:
    import fcntl

# engLib is built into engine.exe, always available
from engLib import args as engine_args, debug, monitorStatus, error

# Sibling stdlib-only modules backing the per-environment scoped install.
import ast_deps
import pkg_families
import venv_env

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REQUIREMENTS_GLOBS = [
    'requirement*.txt',
    'nodes/**/requirement*.txt',
    'ai/**/requirement*.txt',
]

# Override files: unlike constraints, uv overrides REPLACE what packages
# declare. Discovered like requirement files; see packages/ai/src/ai/overrides.txt
# for the policy comment. Named 'overrides.txt' so REQUIREMENTS_GLOBS
# ('requirement*.txt') never sweeps them into the combined requirements.
OVERRIDES_GLOBS = [
    'overrides.txt',
    'nodes/**/overrides.txt',
    'ai/**/overrides.txt',
]


# How the startup compile narrows when ROCKETRIDE_SERVER_USE_VENV forces scoping on. The per-node
# files leave: keeping them would require every node in the installation to be mutually
# satisfiable, so two nodes with conflicting pins could not coexist at all — the engine would fail
# to start before any per-environment logic runs. The tree baseline stays, and the pattern is
# narrowed rather than dropped to keep it: `nodes/requirements.txt` is not a node dependency, it is
# the Python-backend floor the engine process itself runs on, and `nodes/**` matched it only by
# accident (`**` matches zero directories).
_SCOPED_GLOB_REPLACEMENTS = {'nodes/**/requirement*.txt': 'nodes/requirement*.txt'}

# Bootstrap tools install outside any constraint, so pin them or they float to 'latest' and a
# later install downgrades them. Lockstep with packages/server/scripts/tasks.js.
_BOOTSTRAP_TOOL_VERSIONS: dict[str, str] = {
    'wheel': '0.47.0',
    'setuptools': '82.0.1',
    'uv': '0.11.25',
}


def _tool_spec(name: str) -> str:
    """Pinned pip spec for a bootstrap tool; bare name if unpinned."""
    version = _BOOTSTRAP_TOOL_VERSIONS.get(name)
    return f'{name}=={version}' if version else name


# ---------------------------------------------------------------------------
# Active environment
# ---------------------------------------------------------------------------

# Guards the registry, the active context and the reentrant-lock depths below. These are
# process-wide by construction, not thread-local: they decide where `uv --target` writes,
# and the overlay they correspond to lives on the process-wide sys.path.
_state_lock = threading.RLock()

# env key -> context. Entries live for the process, so re-activating an environment
# restores what it already installed instead of reinstalling it.
_registry: dict[str, venv_env.EnvContext] = {}

# The environment installs currently go to; None means the base runtime.
_active: Optional[venv_env.EnvContext] = None

# The overlay currently on sys.path. At most one can be, which makes this process state
# rather than a field on a context.
_inserted_overlay: Optional[str] = None


def _base_env() -> venv_env.EnvContext:
    """The base runtime as a context: cache/ for metadata, lib/site-packages as target."""
    with _state_lock:
        ctx = _registry.get(venv_env.BASE_KEY)
        if ctx is None:
            ctx = venv_env.EnvContext(
                key=venv_env.BASE_KEY,
                paths=venv_env.base_paths(engine_cache_dir(), _get_site_packages()),
                is_overlay=False,
            )
            _registry[venv_env.BASE_KEY] = ctx
        return ctx


def active_env() -> venv_env.EnvContext:
    """The environment installs currently go to (the base runtime unless one is active)."""
    with _state_lock:
        return _active or _base_env()


def register_env(directory: str) -> venv_env.EnvContext:
    """Get (creating once) the context for the overlay in ``directory``."""
    key = os.path.normcase(os.path.abspath(directory))
    with _state_lock:
        ctx = _registry.get(key)
        if ctx is None:
            ctx = venv_env.EnvContext(key=key, paths=venv_env.env_paths(directory), is_overlay=True)
            _registry[key] = ctx
        return ctx


def activate_env(ctx: Optional[venv_env.EnvContext]) -> Optional[venv_env.EnvContext]:
    """Make ``ctx`` the install target (``None`` = base); return the previous one."""
    global _active
    with _state_lock:
        previous, _active = _active, ctx
        return previous


@contextmanager
def use_env(ctx: Optional[venv_env.EnvContext]):
    """Install into ``ctx`` for the duration of the block, then restore the previous one.

    Switches the lock, the constraints file, the ``uv --target`` destination and the
    already-installed record together — that is the whole point of the context object.

    It deliberately does **not** touch ``sys.path``: applying an overlay is
    :func:`ensure_env_scoped`'s job, because the insert has to respect the mock-shim
    ordering and has to displace whatever overlay was there before. Activating an
    environment while imports still resolve through another one's overlay resolves
    dependencies into one place and imports them from another.
    """
    previous = activate_env(ctx)
    try:
        yield ctx
    finally:
        activate_env(previous)


# ---------------------------------------------------------------------------
# Install progress (per operation, not per environment)
# ---------------------------------------------------------------------------


class _InstallProgress:
    """Sidecar + heartbeat state for one install operation.

    One instance per held lock rather than one set of module globals, so a nested
    install cannot clear the outer one's state — losing the heartbeat there would let
    the task startup timeout fire in the middle of a long silent ``uv`` run.
    """

    def __init__(self, sidecar_path: Optional[str] = None):
        """Create progress state; ``sidecar_path`` is ``None`` when no lock is held."""
        self.sidecar_path = sidecar_path
        self.start_time = time.time()
        self.last_message: Optional[str] = None
        # (name, display) — display carries the size suffix uv reports.
        self.downloading: list[tuple[str, str]] = []
        self._thread: Optional[threading.Thread] = None
        self._stop: Optional[threading.Event] = None
        self._depth = 0

    def write(self, message: str):
        """Record ``message`` and mirror it to the sidecar for waiting processes."""
        self.last_message = message
        if not self.sidecar_path:
            return
        try:
            with open(self.sidecar_path, 'w', encoding='utf-8') as f:
                f.write(f'{self.start_time}\n{message}\n')
        except OSError:
            pass

    def start_heartbeat(self):
        """Start (or re-enter) the heartbeat that keeps the task startup timeout alive."""
        self._depth += 1
        if self._thread is not None:
            return
        self._stop = threading.Event()

        def _loop(stop_event: threading.Event):
            # Bound to this instance, not to whatever is on top of the stack: the
            # operation that started the heartbeat is the one it should narrate.
            while not stop_event.wait(5.0):
                if self.last_message:
                    monitorStatus(self.last_message)

        self._thread = threading.Thread(target=_loop, args=(self._stop,), daemon=True)
        self._thread.start()

    def stop_heartbeat(self, force: bool = False):
        """Stop the heartbeat once the outermost caller is done (or when ``force``)."""
        if self._depth > 0:
            self._depth -= 1
        if not force and self._depth > 0:
            return
        self._depth = 0
        if self._stop:
            self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None
        self._stop = None


# Bottom of the stack: progress reported outside any lock still reaches the monitor, it
# just has no sidecar to write to. Having it always present spares every caller a None check.
_progress_stack: list[_InstallProgress] = [_InstallProgress()]


def _progress() -> _InstallProgress:
    """The progress state of the innermost install operation."""
    return _progress_stack[-1]


def _start_heartbeat():
    """Start the heartbeat of the current install operation."""
    _progress().start_heartbeat()


def _stop_heartbeat():
    """Stop the heartbeat of the current install operation (refcounted)."""
    _progress().stop_heartbeat()


def updateProgress(message: str):
    """
    Send a status update to the engine monitor and write the progress sidecar.

    Tracks uv "Downloading <pkg>" / "Downloaded <pkg>" lines to build a
    combined status of all in-flight downloads, e.g. "Downloading torch,
    transformers".  Non-download lines are passed through as-is.

    The in-flight set belongs to the innermost install operation, so two operations
    holding different locks do not report each other's downloads.
    """
    debug(f'  [uv] {message}')
    stripped = message.strip()
    progress = _progress()
    downloading = progress.downloading

    # uv emits "Downloading <name> (<size>)" when a download starts
    if stripped.startswith('Downloading '):
        display = stripped[len('Downloading ') :]
        # Extract bare name for matching, e.g. "stripe (1.4MiB)" -> "stripe"
        name = display[: display.index(' (')] if ' (' in display else display
        if name and not any(n == name for n, _ in downloading):
            downloading.append((name, display))
        # Emit combined status with sizes, e.g. "Downloading torch (2.7GiB), stripe (1.4MiB)"
        combined = f'Downloading {", ".join(d for _, d in downloading)}'
        monitorStatus(combined)
        progress.write(combined)
        return

    # uv emits "Downloaded <name>" when a download finishes
    if stripped.startswith('Downloaded '):
        name = stripped[len('Downloaded ') :]
        if ' (' in name:
            name = name[: name.index(' (')]
        downloading[:] = [(n, d) for n, d in downloading if n != name]
        # If other downloads are still in flight, show them
        if downloading:
            combined = f'Downloading {", ".join(d for _, d in downloading)}'
            monitorStatus(combined)
            progress.write(combined)
        else:
            monitorStatus(message)
            progress.write(message)
        return

    # Any non-download line clears the tracking (new phase)
    downloading.clear()
    monitorStatus(message)
    progress.write(message)


def _read_progress(path: str) -> str:
    """Read the progress sidecar written by the lock holder."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.read().strip().splitlines()
        if len(lines) < 2:
            return ''
        # Line 0 = unix timestamp, line 1 = status message
        started = float(lines[0])
        elapsed = int(time.time() - started)
        return f'{lines[1]} ({elapsed}s)'
    except (OSError, ValueError):
        return ''


# lock path -> how deep this process is inside it. Byte-range locks are per file
# description, so a second open() of a path we already hold is refused exactly like a
# foreign holder — and the wait loop below would then poll forever against ourselves.
_lock_depth: dict[str, int] = {}


class FileLock:
    """
    Simple cross-platform file lock using exclusive file access.

    While the lock is held, callers use ``updateProgress()`` instead of
    ``monitorStatus()`` so that a sidecar file is kept up to date for
    waiting processes to read.

    Reentrant **within this process**: re-acquiring a path we already hold just counts
    up. Between processes nothing changes — a different process still waits and still
    reads the sidecar to report what the holder is doing.
    """

    def __init__(self, lock_path: str, poll_interval: float = 1.0):
        """Initialize the file lock with path and polling interval."""
        self.lock_path = lock_path
        self.poll_interval = poll_interval
        self._file = None
        self._sidecar_path = lock_path.replace('.lock', '.progress')
        self._key = os.path.normcase(os.path.abspath(lock_path))
        self._reentered = False

    def __enter__(self):
        """Acquire the file lock, blocking until it is available."""
        with _state_lock:
            if _lock_depth.get(self._key, 0) > 0:
                _lock_depth[self._key] += 1
                self._reentered = True
                return self

        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)

        while True:
            try:
                self._file = open(self.lock_path, 'wb')
                if os.name == 'nt':
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Lock acquired — this operation owns the sidecar until it releases
                with _state_lock:
                    _lock_depth[self._key] = 1
                _progress_stack.append(_InstallProgress(self._sidecar_path))
                return self
            except (OSError, BlockingIOError):
                if self._file:
                    self._file.close()
                    self._file = None
                # Read what the lock holder is doing and include it in our status
                detail = _read_progress(self._sidecar_path)
                if detail:
                    monitorStatus(f'Waiting — {detail}')
                else:
                    monitorStatus('Waiting for another installation to complete...')
                time.sleep(self.poll_interval)

    def __exit__(self, *args):
        """Release the file lock and clean up progress sidecar."""
        if self._reentered:
            with _state_lock:
                _lock_depth[self._key] = max(0, _lock_depth.get(self._key, 1) - 1)
            return

        with _state_lock:
            _lock_depth.pop(self._key, None)
        if len(_progress_stack) > 1:
            _progress_stack.pop().stop_heartbeat(force=True)
        try:
            os.remove(self._sidecar_path)
        except OSError:
            pass
        if self._file:
            self._file.close()
            self._file = None


# ---------------------------------------------------------------------------
# Environment Bootstrap
# ---------------------------------------------------------------------------


def _get_executable_dir() -> str:
    """Get the directory containing the Python executable."""
    return os.path.dirname(os.path.abspath(sys.executable))


def engine_cache_dir(create: bool = False) -> str:
    """Return (and create if needed) the engine cache directory (``<executable dir>/cache``).

    Single source of truth for the cache location.

    Args:
        create: Create directory if indicated.

    Returns:
        Absolute path to the engine cache directory.
    """
    path = os.path.join(_get_executable_dir(), 'cache')
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def model_cache_dir(name: str, create: bool = True) -> str:
    """Return (and create if required) a per-model cache directory under the engine cache.

    Args:
        name: Subdirectory name for this model's weights/assets.
        create: Create directory if indicated

    Returns:
        Absolute path to the created ``<engine cache>/models/<name>`` directory.
    """
    path = os.path.join(engine_cache_dir(), 'models', name)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def _get_combined_path() -> str:
    """Path to the concatenated requirements file (the constraints-compile input)."""
    return _base_env().paths.combined


def _get_constraints_path() -> str:
    """Path to the compiled constraints file applied (``-c``) to every install."""
    return _base_env().paths.constraints


def _constraints_args(constraints_path: str, exe_dir: str) -> list[str]:
    """Return uv ``-c`` args if the constraints file exists and is non-empty, else ``[]``.

    Relative to exe_dir (the subprocess cwd) — uv splits the value on whitespace.
    """
    if os.path.exists(constraints_path) and os.path.getsize(constraints_path) > 0:
        return ['-c', os.path.relpath(constraints_path, exe_dir)]
    return []


def _get_overrides_path() -> str:
    """Path of the combined overrides file in the engine cache."""
    return os.path.join(engine_cache_dir(), 'overrides-combined.txt')


def _override_args(exe_dir: str) -> list[str]:
    """Return uv ``--override`` args if the combined overrides file is non-empty, else ``[]``.

    Relative to exe_dir (the subprocess cwd) — uv splits the value on whitespace.
    """
    overrides_path = _get_overrides_path()
    if os.path.exists(overrides_path) and os.path.getsize(overrides_path) > 0:
        return ['--override', os.path.relpath(overrides_path, exe_dir)]
    return []


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """
    Run a subprocess command, keeping stdin open until process exits.

    Uses Popen with threads to read stdout/stderr while keeping stdin
    open until the process naturally terminates.

    When spawning engine.exe subprocesses, adds --monitor=App to prevent
    stdin monitor from interfering with the parent process.
    """
    import threading

    # If running engine.exe as subprocess, add --monitor=App
    if args and args[0] == sys.executable:
        args = [args[0], '--monitor=App'] + args[1:]

    debug(f'Running: {" ".join(args)}')

    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        errors='replace',
    )

    # Read stdout/stderr in background threads to avoid blocking
    stdout_data = []
    stderr_data = []

    def read_stdout():
        stdout_data.append(proc.stdout.read())

    def read_stderr():
        stderr_data.append(proc.stderr.read())

    stdout_thread = threading.Thread(target=read_stdout)
    stderr_thread = threading.Thread(target=read_stderr)
    stdout_thread.start()
    stderr_thread.start()

    # Wait for process to exit (stdin stays open)
    proc.wait()

    # Now close stdin (process already exited)
    proc.stdin.close()

    # Wait for output threads to finish
    stdout_thread.join()
    stderr_thread.join()

    stdout = stdout_data[0] if stdout_data else ''
    stderr = stderr_data[0] if stderr_data else ''

    result = subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)

    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, args, stdout, stderr)

    return result


def _pip_available():
    """Check if pip module is available."""
    try:
        import importlib.util

        return importlib.util.find_spec('pip') is not None
    except Exception:
        return False


def _ensure_pip():
    """Ensure pip is available using ensurepip."""
    if _pip_available():
        debug('pip is available')
        return

    updateProgress('Bootstrapping pip...')

    # Use _run which keeps stdin open until process exits
    try:
        result = _run([sys.executable, '-m', 'ensurepip', '--upgrade'], check=False)

        if result.returncode != 0:
            # Check if pip is available anyway (might have succeeded before error)
            if _pip_available():
                return
            raise RuntimeError(f'Failed to bootstrap pip: {result.stderr}')
    except Exception:
        # Check if pip got installed despite exception
        if _pip_available():
            return
        raise


def _uv_abs_path() -> str:
    """Get the absolute path to the uv executable based on platform."""
    exe_dir = _get_executable_dir()
    return os.path.join(exe_dir, 'Scripts', 'uv.exe') if os.name == 'nt' else os.path.join(exe_dir, 'bin', 'uv')


def _uv_available() -> bool:
    """Check if uv executable exists."""
    return os.path.isfile(_uv_abs_path())


def _wheel_available() -> bool:
    """Check if wheel module is available."""
    try:
        import importlib.util

        return importlib.util.find_spec('wheel') is not None
    except Exception:
        return False


def _ensure_wheel():
    """Ensure wheel is installed (needed for building packages with --no-build-isolation)."""
    if _wheel_available():
        debug('wheel is available')
        return

    updateProgress('Installing wheel...')
    result = _run(
        [sys.executable, '-m', 'pip', 'install', _tool_spec('wheel'), '--quiet', '--disable-pip-version-check'],
        check=False,
    )

    if result.returncode != 0:
        error(f'Failed to install wheel: {result.stderr}')
        raise RuntimeError('Failed to install wheel')

    debug('wheel installed successfully')


def _setuptools_available() -> bool:
    """Check if setuptools module is available."""
    try:
        import importlib.util

        return importlib.util.find_spec('setuptools') is not None
    except Exception:
        return False


def _ensure_setuptools():
    """Ensure setuptools is installed in the parent env.

    Required because we compile/install with --no-build-isolation, which means uv
    builds source-only wheels (e.g. docopt 0.6.2, transitively pulled by kokoro →
    misaki → num2words) against the parent env. Several legacy sdists declare
    setup.py-style builds without listing setuptools in build-system.requires,
    so uv can't auto-bootstrap them. Mirroring _ensure_wheel so the parent env
    has the standard PEP 517 backend available at compile/install time.
    """
    if _setuptools_available():
        debug('setuptools is available')
        return

    updateProgress('Installing setuptools...')
    result = _run(
        [sys.executable, '-m', 'pip', 'install', _tool_spec('setuptools'), '--quiet', '--disable-pip-version-check'],
        check=False,
    )

    if result.returncode != 0:
        error(f'Failed to install setuptools: {result.stderr}')
        raise RuntimeError('Failed to install setuptools')

    # Verify installation
    if not _setuptools_available():
        raise RuntimeError('setuptools installed but not found')

    debug('setuptools installed successfully')


def _ensure_uv():
    """Ensure uv is installed."""
    if _uv_available():
        debug('uv is available')
        return

    updateProgress('Installing uv...')
    result = _run(
        [sys.executable, '-m', 'pip', 'install', _tool_spec('uv'), '--quiet', '--disable-pip-version-check'],
        check=False,
    )

    if result.returncode != 0:
        error(f'Failed to install uv: {result.stderr}')
        raise RuntimeError('Failed to install uv')

    # Verify installation
    if not _uv_available():
        raise RuntimeError('uv installed but not found')


def pip(*args) -> bool:
    """
    Run pip command in a platform-independent way.

    This is a simple wrapper around 'python -m pip' for use by modules
    that need to manage packages (e.g., ai.common.opencv for cleanup).

    Usage:
        from depends import pip
        pip('uninstall', '-y', 'opencv-python')
        pip('install', 'some-package>=1.0')

    Args:
        *args: Arguments to pass to pip

    Returns:
        True if command succeeded, False otherwise
    """
    cmd = [sys.executable, '-m', 'pip'] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', check=False)
    return result.returncode == 0


def _apply_pywin32_hack():
    """
    Apply pywin32 path hack on Windows if needed.

    pywin32 uses a .pth file to add paths to sys.path at Python startup.
    If pywin32 is installed during a running session, those paths won't
    be available until the next Python restart. This hack adds them manually.
    """
    if platform.system() != 'Windows':
        return

    # Check if pywin32 is installed
    try:
        import importlib.metadata

        importlib.metadata.version('pywin32')
    except Exception:
        return  # Not installed, nothing to do

    # Check if pywintypes is already importable (hack not needed)
    try:
        import pywintypes

        _ = pywintypes
        return
    except ImportError:
        pass

    debug('Applying pywin32 path hack...')
    # The active environment's target, so a pywin32 that landed in an overlay is found.
    site_path = active_env().paths.site_packages
    pywin32_paths = ['win32', 'win32/lib', 'Pythonwin']

    for subpath in pywin32_paths:
        full_path = os.path.abspath(os.path.join(site_path, subpath))
        if full_path not in sys.path and os.path.exists(full_path):
            sys.path.append(full_path)
            debug(f'  Added: {full_path}')


def _get_site_packages() -> str:
    """Get the site-packages directory path (platform-specific)."""
    exe_dir = _get_executable_dir()
    if os.name == 'nt':
        return os.path.join(exe_dir, 'lib', 'site-packages')
    else:
        # Unix: lib/python3.X/site-packages
        version = f'python{sys.version_info.major}.{sys.version_info.minor}'
        return os.path.join(exe_dir, 'lib', version, 'site-packages')


def _ensure_site_packages():
    """Ensure site-packages directory exists and is in sys.path."""
    site_packages = _get_site_packages()

    # Create if doesn't exist
    if not os.path.exists(site_packages):
        os.makedirs(site_packages, exist_ok=True)
        debug(f'Created site-packages: {site_packages}')

    # Ensure it's in sys.path
    if site_packages not in sys.path:
        sys.path.append(site_packages)
        debug(f'Added site-packages to sys.path: {site_packages}')


def bootstrap():
    """Bootstrap the environment: ensure pip, uv, wheel, setuptools."""
    _ensure_site_packages()  # Must be first!
    _ensure_pip()
    _ensure_wheel()  # Needed for building packages with --no-build-isolation
    _ensure_setuptools()  # Same reason — required by sdists with legacy setup.py builds
    _ensure_uv()


# ---------------------------------------------------------------------------
# Constraints Management
# ---------------------------------------------------------------------------


def _find_requirement_files() -> list[str]:
    """Find all requirement files matching the glob patterns.

    With scoping forced on the node globs are narrowed, not dropped (see
    :data:`_SCOPED_GLOB_REPLACEMENTS`): per-node files leave the startup compile, the tree
    baseline stays. In auto and legacy mode the set is unchanged, byte for byte.
    """
    executable_dir = _get_executable_dir()
    found = []

    patterns = REQUIREMENTS_GLOBS
    if venv_env.use_venv_mode() == venv_env.USE_ON:
        patterns = [_SCOPED_GLOB_REPLACEMENTS.get(p, p) for p in patterns]

    for pattern in patterns:
        full_pattern = os.path.join(executable_dir, pattern)
        matches = glob(full_pattern, recursive=True)
        for path in matches:
            abs_path = os.path.abspath(path)
            if os.path.isfile(abs_path) and abs_path not in found:
                found.append(abs_path)

    return found


def _find_override_files() -> list[str]:
    """Find all override files matching OVERRIDES_GLOBS."""
    executable_dir = _get_executable_dir()
    found = []
    for pattern in OVERRIDES_GLOBS:
        full_pattern = os.path.join(executable_dir, pattern)
        for path in glob(full_pattern, recursive=True):
            abs_path = os.path.abspath(path)
            if os.path.isfile(abs_path) and abs_path not in found:
                found.append(abs_path)
    return found


def _compute_hash(file_paths: list[str]) -> str:
    """Compute a fast hash from file metadata (mtime + size)."""
    hasher = hashlib.md5()
    for path in sorted(file_paths):
        stat = os.stat(path)
        entry = f'{path}:{stat.st_size}:{stat.st_mtime_ns}\n'
        hasher.update(entry.encode())
    return hasher.hexdigest()


def _load_stored_hash(hash_file: str) -> Optional[str]:
    """Load the stored hash from file."""
    try:
        with open(hash_file, 'r') as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def _save_hash(hash_file: str, hash_value: str):
    """Save the hash to file."""
    with open(hash_file, 'w') as f:
        f.write(hash_value)


def _combine_requirements(file_paths: list[str], output_path: str):
    """Concatenate all requirement files into one (base and scoped share one combiner).

    Delegates so both paths get the same ``-r`` handling: the combined file lives in a
    different directory than its sources, and uv resolves an include relative to the file
    holding the line.
    """
    venv_env.write_combined(file_paths, output_path)


class CompileFailed(RuntimeError):
    """A ``uv pip compile`` that returned non-zero, carrying uv's own explanation.

    The detail is kept as an attribute because the second, aligned compile has to read it:
    a failure naming a family member is a namespace conflict with a container remedy, and a
    failure naming nothing of the sort is an ordinary compile failure that merely surfaced
    there. Reporting the second as the first would send an operator to split a pipeline over
    an unreachable index.
    """

    def __init__(self, detail: str):
        super().__init__(f'Failed to compile constraints: {detail[:800]}')
        self.detail = detail


def _run_uv_compile(combined_path: str, constraints_path: str) -> None:
    """One ``uv pip compile`` pass, ``combined_path`` -> ``constraints_path``."""
    if not _uv_available():
        raise RuntimeError('uv executable not found')

    exe_dir = _get_executable_dir()
    updateProgress('Compiling constraints...')

    args = [
        _uv_abs_path(),
        'pip',
        'compile',
        combined_path,
        '--output-file',
        constraints_path,
        '--python',
        sys.executable,  # Explicitly specify Python version to avoid mismatch
        '--index-strategy',
        'unsafe-best-match',  # Check all indexes for best version
        '--no-build-isolation',  # Don't create temp venvs (engine.exe can't create venvs)
        '--emit-index-url',  # Preserve --extra-index-url etc. so install/dry-run can find packages (e.g. torch+cu128)
    ]
    args.extend(_override_args(exe_dir))
    debug(f'Compile: {args}')
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.PIPE,
        encoding='utf-8',
        errors='replace',
        cwd=exe_dir,
    )

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or '').strip()
        error(f'Failed to compile constraints: {detail}')
        raise CompileFailed(detail)

    debug(f'Constraints compiled: {constraints_path}')


def _compile_constraints(constraints_path: str):
    """Compile the global union, then align any package family it turned up."""
    _compile_constraints_at(_get_combined_path(), constraints_path)


def ensure_constraints() -> str:
    """
    Ensure the constraints file is up to date.

    Returns the path to the constraints file.
    """
    paths = _base_env().paths
    os.makedirs(paths.env_dir, exist_ok=True)

    hash_file = paths.hash_file
    combined_path = paths.combined
    constraints_path = paths.constraints

    # Find all requirement files
    req_files = _find_requirement_files()
    override_files = _find_override_files()
    if not req_files:
        debug('No requirement files found')
        return constraints_path

    # Hash the includes too: a `-r`-referenced file shapes the resolution, so it has to be
    # able to invalidate it, and an override shapes it exactly as much — so both go through
    # the same walk. And the family declarations, which live in lib/ where the walk never
    # looks: without that, editing a declared namespace version changes nothing and the
    # documented remedy silently does nothing. Environments holding no family member keep
    # their bytes unchanged and do not rebuild for this.
    current_hash = pkg_families.combine_hash(
        _compute_hash(venv_env.resolve_includes(req_files + override_files)),
        pkg_families.hash_contribution(constraints_path),
    )
    stored_hash = _load_stored_hash(hash_file)

    # Check if rebuild is needed. The derived overrides cache is part of the
    # predicate: install-time _override_args() reads that file, so a missing
    # one (partially cleared cache) while override files exist — or a stale
    # non-empty one after overrides were removed — must trigger a rebuild,
    # not be silently reused.
    overrides_path = _get_overrides_path()
    overrides_cache_nonempty = os.path.exists(overrides_path) and os.path.getsize(overrides_path) > 0
    if (
        current_hash == stored_hash
        and os.path.exists(constraints_path)
        and bool(override_files) == overrides_cache_nonempty
    ):
        debug('Constraints are up to date')
        return constraints_path

    debug('Requirements changed, rebuilding constraints...')
    updateProgress('Rebuilding constraints...')

    # Combine all requirements
    _combine_requirements(req_files, combined_path)

    # Combine all overrides (empty file list yields a zero-byte file, treated as absent)
    _combine_requirements(override_files, _get_overrides_path())

    # Compile with uv
    _compile_constraints(constraints_path)

    # Save new hash
    _save_hash(hash_file, current_hash)

    return constraints_path


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


def _base_excludes() -> tuple[str, ...]:
    """The exclusions that hold for every install, family machinery aside.

    `uv` is bootstrapped by depends.py and pip-installing it crashes on Windows. That is
    the whole set, and it is **platform-independent**: the plain-`onnxruntime` line that
    used to sit here was a hand-written family rule, and it now comes from
    `pkg_families.excluded()` like every other member.

    Re-adding a member here would not merely duplicate the family's own exclusions — it
    would disarm the family. This set is what the trigger's dry-run is given, and a
    dry-run resolved without a member reports no member, so the ordered passes never run
    and the namespace goes missing as an ImportError. See :func:`_install_dry_run`.
    """
    return ('uv',)


def _write_excludes_file(extra: tuple[str, ...] = ()) -> str:
    """Write uv's resolution-excludes file **content-addressed** and return its path.

    One rewritten `cache/excludes.txt` was safe only while the content was a constant and
    every caller wanted the same bytes. Neither holds now: the set depends on which
    families *this* install covers — which under the base runtime varies per requirements
    file, not per environment — and the trigger's dry-run needs the smaller base set alive
    at the same moment as an install's larger one.

    So the path is derived from the content: callers computing the same exclusions land on
    the same bytes at the same path, callers computing different ones cannot overwrite each
    other, and "who might be writing this file right now" stops being a question rather than
    getting an answer. Written via a temporary file and renamed, so a concurrent reader
    never sees a partial one.
    """
    seen: set[str] = set()
    lines: list[str] = []
    for name in tuple(_base_excludes()) + tuple(extra):
        key = name.strip().lower()
        if key and key not in seen:
            seen.add(key)
            lines.append(name.strip())
    content = '\n'.join(lines) + '\n'
    digest = hashlib.md5(content.encode('utf-8')).hexdigest()[:12]
    excludes_path = os.path.join(engine_cache_dir(create=True), f'excludes-{digest}.txt')
    if not os.path.exists(excludes_path):
        temporary = f'{excludes_path}.{os.getpid()}.tmp'
        with open(temporary, 'w', encoding='utf-8') as f:
            f.write(content)
        os.replace(temporary, excludes_path)
    return excludes_path


# ---------------------------------------------------------------------------
# Satisfied-verdict cache
# ---------------------------------------------------------------------------
#
# ``_processed`` spans one process, so every cold engine re-ran the 20-40s uv
# resolve only to conclude that nothing was missing (#2089). Persist that
# verdict instead, keyed by everything a resolve consults.
#
# Two consequences worth knowing. The fingerprint is the whole installed set,
# so installing anything invalidates every file's verdict: on a host where
# nodes install lazily, each install costs one extra resolve per requirements
# file before things settle again. And the key cannot see the resolve's own
# arguments, so _VERDICT_SCHEMA below is bumped whenever those change.
#
# The check and the write both run under the engine-global ``install.lock``
# via depends(), which is what makes the non-atomic _save_hash write safe.
# Per-node locks (#2089 ask 2) would remove that guarantee.

# Bump when the resolve's inputs or semantics change (uv arguments, what the
# key covers), so verdicts recorded under the old behaviour are not reused.
_VERDICT_SCHEMA = '1'


def _verdict_path(requirements_path: str) -> str:
    """Path of the cached verdict for a requirements file."""
    digest = hashlib.md5(os.path.abspath(requirements_path).encode('utf-8')).hexdigest()
    return os.path.join(engine_cache_dir(), 'satisfied', f'{digest}.hash')


def _file_digest(path: str) -> str:
    """Content digest of a file, or empty string when it does not exist."""
    try:
        with open(path, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()
    except OSError:
        return ''


# A requirements line that pulls in another file: ``-r``/``--requirement`` and
# ``-c``/``--constraint``, with the path after whitespace or ``=``.
_INCLUDE_DIRECTIVE = re.compile(r'^\s*(?:-r|--requirement|-c|--constraint)(?:\s+|=)(?:"([^"]+)"|\'([^\']+)\'|(\S+))')


def _requirements_closure(requirements_path: str) -> list[str]:
    """``requirements_path`` plus every file it pulls in through ``-r`` / ``-c``, recursively.

    Included paths resolve relative to the including file, as pip and uv resolve
    them. Each file is visited once, so an include cycle terminates.
    """
    closure: list[str] = []
    pending = [os.path.abspath(requirements_path)]
    while pending:
        path = pending.pop(0)
        if path in closure:
            continue
        closure.append(path)
        try:
            # errors='replace' because this decode only feeds directive
            # scanning: the digest is taken from the bytes in _file_digest, so a
            # non-UTF-8 requirements file must not fail the node it belongs to.
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                # A trailing backslash continues the line, as in pip and uv.
                lines = f.read().replace('\\\n', '').splitlines()
        except OSError:
            continue
        for line in lines:
            match = _INCLUDE_DIRECTIVE.match(line)
            if match:
                target = match.group(1) or match.group(2) or match.group(3)
                pending.append(os.path.normpath(os.path.join(os.path.dirname(path), target)))
    return closure


def _installed_fingerprint() -> Optional[str]:
    """Digest of the *.dist-info / *.egg-info names in site-packages, or ``None``.

    The names carry versions, so an install, upgrade or uninstall changes it.

    ``None`` when the directory cannot be listed. Hashing an empty list instead
    would return the same digest every time, so the key would stop noticing
    installs and every verdict would stay satisfied for ever — a silent stale
    hit. Refusing the key resolves every time instead, which is the safe
    direction. ``bootstrap()`` creates this directory before any key is
    computed, so on the real path it always exists.
    """
    try:
        entries = [e for e in os.listdir(_get_site_packages()) if e.endswith(('.dist-info', '.egg-info'))]
    except OSError:
        return None
    return hashlib.md5('\n'.join(sorted(entries)).encode('utf-8')).hexdigest()


def _verdict_key(requirements_path: str, constraints_path: str) -> Optional[str]:
    """Key under which a "satisfied" verdict for ``requirements_path`` is valid.

    Everything that can change what a resolve concludes is part of it,
    including every file the requirements file includes through ``-r`` / ``-c``.

    Returns ``None`` when the key cannot be computed honestly — a directive
    names something that is not a readable file, or site-packages cannot be
    listed. In
    both cases something the resolve depends on is invisible here, so the key
    would be stable over changing inputs. Refusing it degrades to resolving
    every time, which is the safe direction — a stable key over an unseen
    input keeps reporting "satisfied" after that input gains a dependency.
    """
    closure = _requirements_closure(requirements_path)
    if not all(os.path.isfile(path) for path in closure):
        return None

    installed = _installed_fingerprint()
    if installed is None:
        return None

    parts = [_VERDICT_SCHEMA, sys.executable, sys.version]
    parts.extend(_file_digest(path) for path in closure)
    parts.extend(
        [
            _file_digest(constraints_path),
            _file_digest(_get_overrides_path()),
            _excludes_content(),
            installed,
        ]
    )
    # NUL-separated: none of the parts can contain it, so field boundaries
    # stay unambiguous even though two of them are multi-line text.
    return hashlib.md5('\0'.join(parts).encode('utf-8')).hexdigest()


def _verdict_cached(requirements_path: str, constraints_path: str) -> bool:
    """True when a previous resolve recorded this requirements file as satisfied under the current key.

    An unreadable cache, or a key the closure refuses to produce, is a miss —
    never an error. The cache is an optimisation: anything it cannot answer
    must fall back to resolving, not fail the node that asked.
    """
    try:
        stored = _load_stored_hash(_verdict_path(requirements_path))
        if stored is None:
            return False
        key = _verdict_key(requirements_path, constraints_path)
        return key is not None and stored == key
    except Exception as e:  # noqa: BLE001 - a cache read must never fail an install
        debug(f'  Could not read the satisfied verdict ({e}); resolving instead')
        return False


def _save_verdict(requirements_path: str, constraints_path: str):
    """Record that ``requirements_path`` is satisfied under the current key.

    Best effort, like the progress sidecar: the verdict only saves the next
    process a resolve, so a cache that cannot be written must not fail an
    install that succeeded.
    """
    try:
        key = _verdict_key(requirements_path, constraints_path)
        if key is None:
            debug(f'  Not recording a verdict: an include of {requirements_path} does not resolve')
            return
        verdict_file = _verdict_path(requirements_path)
        os.makedirs(os.path.dirname(verdict_file), exist_ok=True)
        _save_hash(verdict_file, key)
    except Exception as e:  # noqa: BLE001 - a cache write must never fail an install
        debug(f'  Could not record the satisfied verdict ({e}); the next process will resolve again')


def _target_site() -> Optional[str]:
    """The overlay ``uv --target`` should write to, or ``None`` for the base runtime.

    uv reads the target directory, so packages the scoped install already placed there
    are reported as satisfied instead of being reinstalled into the base runtime.
    """
    ctx = active_env()
    return ctx.paths.site_packages if ctx.is_overlay else None


def _target_args() -> list[str]:
    """``uv --target <overlay>`` when an overlay is active, else ``[]``."""
    site = _target_site()
    return ['--target', site] if site else []


# ---------------------------------------------------------------------------
# Shared-namespace package families
# ---------------------------------------------------------------------------


class RestartRequired(RuntimeError):
    """The environment is correct; this **process** cannot run on it.

    Raised when a family's namespace is already in ``sys.modules`` and the environment
    provides a different build of it. A loaded extension module cannot be replaced under a
    live interpreter, and a ``sys.path`` insert does not re-import what is already loaded —
    so the environment is finished and recorded, and then the *run* is refused. Refusing the
    build instead would be untrue: there may be nothing to install at all.
    """


_facts_cache: Optional[pkg_families.Facts] = None


def _facts() -> pkg_families.Facts:
    """The environment facts, one instance per process (each fact resolves lazily inside)."""
    global _facts_cache
    with _state_lock:
        if _facts_cache is None:
            _facts_cache = pkg_families.Facts(exe_dir=_get_executable_dir())
        return _facts_cache


@dataclass
class _FamilyWork:
    """One family in play for an install: what it holds the namespace at, and what is left."""

    family: pkg_families.Family
    version: str
    members: tuple[pkg_families.Member, ...]
    passes: tuple[pkg_families.InstallPass, ...]


def _read_resolution(constraints_path: str) -> dict[str, str]:
    """The environment's compiled resolution, or empty when it has not been compiled yet."""
    try:
        with open(constraints_path, 'r', encoding='utf-8') as fh:
            return pkg_families.parse_resolution(fh.read())
    except OSError:
        return {}


def _family_work(constraints_path: str, target_site: str, trigger: Optional[list[str]] = None) -> list[_FamilyWork]:
    """The families this install must handle, and the ordered passes each still owes.

    **Two questions, deliberately kept apart.** *When* the step runs is per call and comes
    from ``trigger`` — the packages this install would actually touch, from its dry-run.
    *What* it installs is a property of the environment, read from its compiled
    ``constraints.txt``. Keying the step off the resolution would drag every opencv wheel
    the installation resolves into the very first ``depends()`` of startup, which asks for
    nothing from opencv; drawing the set from the call would let a subset arriving later be
    the only member written and take the namespace from a superset already there.

    ``trigger=None`` means "presence in the resolution is the trigger", which is the overlay
    path: :func:`_install_target` installs the whole combined file in one go and has no
    dry-run, so a family present in the resolution is by construction a family being
    installed. An implementer looking for a dry-run there will not find one.
    """
    resolved = _read_resolution(constraints_path)
    if not resolved:
        return []
    installed = pkg_families.installed_versions(target_site)
    facts = _facts()
    wanted = None if trigger is None else {pkg_families.normalize(name) for name in trigger}

    work: list[_FamilyWork] = []
    for family in pkg_families.members_in(resolved):
        if wanted is not None and not any(pkg_families.normalize(m.dist) in wanted for m in family.members):
            continue
        version = pkg_families.align(family, resolved)
        if not version:
            continue
        members = pkg_families.install_set(family, resolved, facts)
        work.append(
            _FamilyWork(
                family=family,
                version=version,
                members=members,
                passes=pkg_families.install_batches(family, version, members, installed),
            )
        )
    return work


def _loaded_family_version(family) -> Optional[str]:
    """The *distribution* version behind an already-imported family namespace.

    Deliberately not ``module.__version__``. A resolution and a ``*.dist-info`` both speak
    distribution versions, and for this family the two vocabularies never agree: every
    ``opencv-*`` wheel carries a build component (``4.13.0.92``) that ``cv2.__version__``
    (``4.13.0``) drops. Comparing across that gap makes a correctly installed environment
    read as shadowed on every call — measured, after it failed the whole nodes lane.

    Reads the site the module was actually loaded from, which is the only thing that can
    answer "another environment" in the first place. The **last member present wins**,
    matching the family's own rule that the widest variant writes the namespace last.
    Returns ``None`` when the origin cannot be established; a caller that cannot tell must
    not refuse.
    """
    module = sys.modules.get(family.import_name)
    if module is None:
        return None
    paths = list(getattr(module, '__path__', None) or [])
    if paths:
        site = os.path.dirname(paths[0])
    else:
        file = getattr(module, '__file__', None)
        if not file:
            return None
        site = os.path.dirname(os.path.dirname(file))
    installed = pkg_families.installed_versions(site)
    owner_version = None
    for member in family.members:
        key = pkg_families.normalize(member.dist)
        if key in installed:
            owner_version = installed[key]
    return owner_version


def _shadowing(work: _FamilyWork) -> Optional[str]:
    """Why this process cannot run on the environment it just built, or ``None``.

    Two shapes, one answer. The namespace may be **about to be rewritten** under a live
    interpreter — on Windows that write fails on the locked extension, on Linux it succeeds
    and the running process keeps serving the old module while the environment reports the
    new one, which is the worse of the two because it is silent. Or the environment may
    simply **provide a different version** than the one already loaded: under the default
    ``auto`` the base and an overlay align over different input sets, so they legitimately
    differ, and an overlay reaching the process as a ``sys.path`` insert does not re-import
    what is already there.

    Only the *write* half is load-bearing here, because only here is it known that something
    is about to be laid down. The version half also lives in :func:`_shadowing_check`, which
    runs outside the gates — this one would never see the case where there is nothing to do.
    """
    loaded = sys.modules.get(work.family.import_name)
    if loaded is None:
        return None
    loaded_version = _loaded_family_version(work.family)
    if work.passes:
        return (
            f'{work.family.import_name} is already imported in this process'
            f' (version {loaded_version or "unknown"}) and the environment installs'
            f' {work.family.name} at {work.version}'
        )
    if loaded_version and loaded_version != work.version:
        return (
            f'{work.family.import_name} was imported from another environment at'
            f' {loaded_version}; this one provides {work.version}'
        )
    return None


def _shadowing_check(constraints_path: str) -> Optional[RestartRequired]:
    """Refuse the run when a family's namespace is loaded here at another version.

    **Deliberately outside every gate**, unlike the rest of the family step, and that is a
    correction rather than a flourish: shadowing is at its most likely exactly when there is
    *nothing to do*. An overlay whose hash matches is never rebuilt, so its install path never
    runs; a ``depends()`` call whose requirements are satisfied returns at its gate. Put this
    check behind either and the common case — parent imported ``cv2`` from base, pipeline then
    runs on an already-built overlay that holds a different one — is never noticed at all, and
    the pipeline silently uses the wrong build.

    Costs nothing when it does not apply: a ``sys.modules`` lookup per registered family, and
    the resolution is read only once one of them is actually loaded.
    """
    loaded = [family for family in pkg_families.families() if family.import_name in sys.modules]
    if not loaded:
        return None
    resolved = _read_resolution(constraints_path)
    if not resolved:
        return None
    for family in loaded:
        if not any(pkg_families.normalize(m.dist) in resolved for m in family.members):
            continue
        version = pkg_families.align(family, resolved)
        loaded_version = _loaded_family_version(family)
        if version and loaded_version and loaded_version != version:
            return RestartRequired(
                f'{family.import_name} was imported from another environment at {loaded_version};'
                f' this one provides {version}. A sys.path insert does not re-import a loaded'
                ' module, so restart the engine to pick it up.'
            )
    return None


def _run_family_passes(work: _FamilyWork, constraints_path: str, target_site: Optional[str]) -> None:
    """Install the family's members explicitly, in declared order, widest last.

    Goes through ``build_install_argv`` rather than a hand-rolled argv: that builder exists
    because while there were two ways to construct an install command, a flag added to one
    silently diverged from the other. The ``-c`` matters beyond tidiness here — the compile
    runs with ``--emit-index-url`` so the index URLs reach an install *through* the
    constraints file, and a pass installing by explicit spec has no other source of them.
    """
    if not work.passes:
        return
    exe_dir = _get_executable_dir()
    # The BASE exclusions, never the family's own — measured, because the failure is silent.
    # uv's `--excludes` excludes from *resolution*, so a pass handed its family's set drops the
    # very member it was asked to install: every pass reports success, nothing lands, and the
    # namespace is simply absent afterwards. The base set keeps nothing out of a pass's
    # resolution these days — it is `uv` alone — and the ordering this pass exists to impose is
    # enforced by running one spec at a time, not by exclusions.
    excludes_rel = os.path.relpath(_write_excludes_file(), exe_dir)
    for step in work.passes:
        spec = f'{step.dist}=={step.version}'
        updateProgress(f'Installing {work.family.name}: {spec}')
        argv = venv_env.build_install_argv(
            uv_path=_uv_abs_path(),
            python_exe=sys.executable,
            specs=[spec],
            target_site=target_site,
            excludes_path=excludes_rel,
            reinstall_packages=(step.dist,) if step.force_reinstall else (),
        )
        argv.extend(_constraints_args(constraints_path, exe_dir))
        debug(f'Family install: {argv}')
        result = subprocess.run(
            argv,
            cwd=exe_dir,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            check=False,
            stdin=subprocess.PIPE,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or '').strip()
            error(f'Family install failed for {spec}: {detail}')
            raise RuntimeError(_family_install_message(work, spec, detail))


def _family_install_message(work: _FamilyWork, spec: str, detail: str) -> str:
    """The refusal text when an ordered pass cannot install ``<member>==<version>``.

    A **declared** version that cannot be found is not a conflict between consumers —
    nobody disagrees, the authored number is wrong (a release can be withdrawn, exactly as
    onnxruntime 1.20.1 was for the -gpu build). Naming a container there would send an
    operator to split a pipeline over a number they could change in one line.
    """
    if work.family.namespace_version:
        return (
            f'{work.family.name}: the declared namespace version {work.version} could not be installed'
            f' ({spec}). This value is authored in lib/pkg_families/{work.family.name}.py, not derived'
            f' from any consumer, so no pipeline split can help — change the declaration.\n{detail[:800]}'
        )
    return f'{work.family.name}: failed to install {spec} at the derived version.\n{detail[:800]}'


# ---------------------------------------------------------------------------
# Probes — proving the environment that was just built
# ---------------------------------------------------------------------------

# The one lever this item ships. Global rather than per family on purpose: an operator
# reaching for it at 2am wants the pipeline up, not a taxonomy, and the log already names
# which probe was downgraded. It is deliberately NOT a switch on the registry — emptying
# that would hand the namespace back to uv's ordering and break `cv2` silently — and not a
# downgrade of the *conflict* refusal, which is about two consumers and has a real remedy.
PROBE_STRICT_ENV = 'ROCKETRIDE_PKG_PROBE_STRICT'

# Generous rather than tight. The work is one import of a large extension module on a
# machine that may still be busy installing; the timeout exists to bound a hang, not to
# police performance, and tripping it is *inconclusive* rather than a failure.
PROBE_TIMEOUT_SECONDS = 180

# (env_dir, family) pairs already re-probed in this process. The marker makes a failure
# survive the gates, and without this it would also make every later `depends()` call in the
# same start spawn another subprocess.
_reprobed: set[tuple[str, str]] = set()


def _probe_strict() -> bool:
    """Whether a failed probe stops the build. ``ROCKETRIDE_PKG_PROBE_STRICT=0`` says no."""
    return (os.environ.get(PROBE_STRICT_ENV) or '').strip() != '0'


def _run_probe(work: _FamilyWork, site: str) -> tuple[str, str]:
    """Run the family's probe against ``site`` in a subprocess; return ``(verdict, detail)``.

    **Never in the resident engine**, which may already hold a different member of this
    namespace in ``sys.modules`` and would answer about the wrong one.

    The script is written to the engine cache and run as ``sys.executable <script>`` — the
    engine's own documented invocation shape. A ``-c`` flag would be shorter and there is no
    evidence the binary has one: nothing in `engine-core` parses it, and every invocation in
    the tree passes a script path.
    """
    probe = work.family.probe
    facts = _facts()
    met = pkg_families.probes.needs_met(probe.needs, facts)
    if met is not True:
        # Unknown facts skip too, and say so. "GPU box, probe skipped" is the one shape that
        # looks like success while checking nothing, so it is never silent.
        reason = 'a required fact is unknown' if met is None else 'its facts do not apply here'
        return pkg_families.probes.SKIPPED, f'{reason} ({facts.describe()})'

    script = pkg_families.probes.probe_script(
        work.family.name,
        probe.code,
        site,
        tuple(m.dist for m in work.members),
        work.version,
    )
    digest = hashlib.md5(script.encode('utf-8')).hexdigest()[:12]
    path = os.path.join(engine_cache_dir(create=True), f'probe-{work.family.name}-{digest}.py')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(script)

    updateProgress(f'Proving {work.family.name} at {work.version}')
    try:
        result = subprocess.run(
            [sys.executable, path],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            check=False,
            stdin=subprocess.PIPE,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return pkg_families.probes.INCONCLUSIVE, f'the probe did not finish within {PROBE_TIMEOUT_SECONDS}s'
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    output = (result.stdout or '') + (result.stderr or '')
    answer = pkg_families.probes.parse_verdict(output)
    if answer is None:
        tail = ' '.join(output.split())[-400:]
        return (
            pkg_families.probes.INCONCLUSIVE,
            f'the probe exited with code {result.returncode} and reported no verdict: {tail}',
        )
    return answer


def _probe_failure_message(work: _FamilyWork, verdict: str, detail: str) -> str:
    """What the operator is told, including the lever that family has — or the cause.

    `cv2` has **no** version lever: its failures are a missing system library or a lost
    namespace race, and naming a knob that repairs neither would send someone to change a
    number. onnxruntime has one, and it is the declared `namespace_version`.
    """
    if verdict == pkg_families.probes.INCONCLUSIVE:
        remedy = 'This is a statement about the probe, not about the package — fix the machine or the probe.'
    elif work.family.namespace_version:
        remedy = (
            f'Adjust `namespace_version` in lib/pkg_families/{work.family.name}.py '
            '(it is in the drift hash, so an edit rebuilds the environments that hold this family).'
        )
    else:
        remedy = 'No version change repairs this — fix the cause named above.'
    return (
        f'{work.family.name}: the environment built at {work.version} did not prove out '
        f'[{verdict}]. {detail}\n{_facts().describe()}\n{remedy}'
    )


def _apply_probe(work: _FamilyWork, env_dir: str, site: str) -> None:
    """Run the probe and act on its answer: clear, record, or stop the build.

    Four bookkeeping behaviours meet here and making any of them uniform is a bug. **Only a
    pass clears the marker** — a skip proves nothing, so a previous failure survives it. A
    hard failure **raises**, which in an overlay also withholds `mark_installed` and so
    rebuilds as well as re-probes. A **downgraded** failure records the build (the hash is
    written and the next start does not rebuild) but still writes the marker, so the
    exemption ends when the operator ends it rather than lasting until an unrelated drift.
    """
    if work.family.probe is None:
        return
    verdict, detail = _run_probe(work, site)

    if verdict == pkg_families.probes.PASS:
        pkg_families.probes.update_marker(env_dir, work.family.name, None)
        debug(f'{work.family.name}: probe passed ({detail})')
        return

    if verdict == pkg_families.probes.SKIPPED:
        # Logged where an operator sees it, not folded into debug: a skip must never read
        # like a pass.
        monitorStatus(f'{work.family.name}: probe skipped — {detail}')
        return

    message = _probe_failure_message(work, verdict, detail)
    if _probe_strict():
        pkg_families.probes.update_marker(env_dir, work.family.name, pkg_families.probes.UNPROVED_FAILED)
        error(message)
        raise RuntimeError(message)

    pkg_families.probes.update_marker(env_dir, work.family.name, pkg_families.probes.UNPROVED_DOWNGRADED)
    monitorStatus(f'{PROBE_STRICT_ENV}=0: {work.family.name} is running UNPROVED. {message}')


def _reprove_unproved(constraints_path: str, env_dir: str, target_site: Optional[str]) -> None:
    """Re-run the probe of any family a previous build left unproved, and nothing else.

    This is the one thing that reaches past both gates. A measured-broken environment has a
    matching `requirements.hash` and every member already at `V`, so the ordered passes are
    skipped and the probe would never run again — the environment would go silently into
    service on the second attempt. One subprocess, no rebuild.
    """
    unproved = pkg_families.probes.read_marker(env_dir)
    if not unproved:
        return
    site = target_site or active_env().paths.site_packages
    resolved = _read_resolution(constraints_path)
    if not resolved:
        return
    facts = _facts()
    for family in pkg_families.members_in(resolved):
        if family.name not in unproved:
            continue
        key = (os.path.normcase(env_dir), family.name)
        with _state_lock:
            if key in _reprobed:
                continue
        version = pkg_families.align(family, resolved)
        if not version:
            continue
        work = _FamilyWork(
            family=family,
            version=version,
            members=pkg_families.install_set(family, resolved, facts),
            passes=(),
        )
        debug(f'{family.name}: marked unproved, re-running the probe alone')
        _apply_probe(work, env_dir, site)
        # Recorded on the way out, never on the way in: the guard is a cost rule, not an
        # exemption. A hard failure raises, and every later call in this process must raise
        # with it rather than sail past an environment the marker says was measured broken.
        with _state_lock:
            _reprobed.add(key)


def _handle_families(
    work_list: list[_FamilyWork],
    constraints_path: str,
    target_site: Optional[str],
) -> Optional[RestartRequired]:
    """Run every family's ordered passes, then report whether this process may use the result.

    With two families in play the steps run per family in registry order and the first
    failure stops the build — there is no partial-success state to design, because a family
    that installed correctly before another failed is simply part of an environment that did
    not finish.

    Returns the refusal rather than raising it, so a caller with bookkeeping to protect can
    record the environment **first**. Recording after the refusal would make the restart the
    message asks for repeat the whole build, and the operator would watch the fix appear not
    to take.
    """
    # Derived from the target rather than from `active_env()`: on the overlay path this runs
    # inside `_compile_and_install`, which is *before* the environment is activated, so the
    # active context is still base and would name the wrong marker file.
    env_dir = os.path.dirname(target_site) if target_site else active_env().paths.env_dir
    site = target_site or active_env().paths.site_packages
    deferred: Optional[RestartRequired] = None
    for work in work_list:
        reason = _shadowing(work)
        _run_family_passes(work, constraints_path, target_site)
        if work.passes:
            # Nothing installed means nothing new to prove; the probe follows the same gate
            # as the passes. What crosses that gate is the unproved marker, elsewhere.
            # Ordered before the restart refusal on purpose: if the probe says the
            # environment is wrong, that is the answer, and "restart and try again" would be
            # advice about a different problem.
            _apply_probe(work, env_dir, site)
        if reason is not None and deferred is None:
            deferred = RestartRequired(f'{reason}. Restart the engine to pick it up.')
    return deferred


def _family_exclusions(work_list: list[_FamilyWork]) -> tuple[str, ...]:
    """Members to keep out of the **main** install, for the families in play.

    Excluding a member does not mean it is not installed — for one in the install set it
    means "not installed by *that* run", because the main install would let uv pick the
    order and the order is the whole point. A member outside the set is excluded outright.
    """
    return tuple(dist for work in work_list for dist in pkg_families.excluded(work.family))


def _install_dry_run(requirements_path: str, constraints_path: str, excludes_path: str) -> list[str]:
    """
    Run uv pip install --dry-run and return list of packages that would be installed.

    Returns empty list if all requirements are already satisfied.
    Raises RuntimeError if dependency resolution fails.

    ``excludes_path`` is a parameter rather than a call to :func:`_write_excludes_file`
    because this answer drives two decisions with opposite needs, and one of them breaks
    if it is given the family exclusions: the family **trigger** asks "would this install
    touch a member", and a dry-run resolved without the members answers "no" by
    construction — the ordered passes would then never run and the namespace would vanish
    from every environment as an ImportError rather than a build failure. So it is given
    the **base** set only, and the caller subtracts family members from the list before
    asking whether there is other work.
    """
    if not _uv_available():
        raise RuntimeError('uv executable not found')

    exe_dir = _get_executable_dir()
    args = [
        _uv_abs_path(),
        'pip',
        'install',
        '--python',
        sys.executable,
        '-r',
        requirements_path,
        '--index-strategy',
        'unsafe-best-match',
        '--no-build-isolation',
        '--dry-run',
        '--no-color',
    ]

    # uv splits --excludes on whitespace, so an absolute path with a space (macOS
    # "Application Support") breaks resolution; pass it relative to the cwd (exe_dir).
    # See #1256.
    args.extend(['--excludes', os.path.relpath(excludes_path, exe_dir)])

    args.extend(_constraints_args(constraints_path, exe_dir))
    args.extend(_override_args(exe_dir))
    args.extend(_target_args())

    debug(f'Dry-run: {args}')
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        check=False,
        stdin=subprocess.PIPE,
        cwd=exe_dir,
    )

    if result.returncode != 0:
        output = (result.stderr + result.stdout).strip()
        debug(f'Dry-run failed (rc={result.returncode}): {output[:500]}')
        error(f'Dependency resolution failed for {requirements_path}: {output}')
        raise RuntimeError(f'Dependency resolution failed: {output[:200]}')

    # Parse packages from output — lines starting with "+ "
    packages = []
    for line in (result.stderr + result.stdout).splitlines():
        line = line.strip()
        if line.startswith('+ '):
            # Line format: "+ package==version" or "+ package[extra]==version"
            pkg = line[2:].strip()
            if '==' in pkg:
                pkg = pkg.split('==')[0]
            if '[' in pkg:
                pkg = pkg.split('[')[0]
            packages.append(pkg)

    return packages


def _install_requirements(requirements_path: str, constraints_path: str):
    """
    Install requirements using uv with constraints.

    Runs a dry-run first to check if anything needs installing. If all
    requirements are satisfied, skips the install entirely. Otherwise,
    streams download and install progress through updateProgress().
    """
    debug(f'Installing requirements from: {requirements_path}')

    # Skip empty requirements files (comments/blanks only) to avoid uv warnings
    with open(requirements_path, 'r', encoding='utf-8') as f:
        has_deps = any(line.strip() and not line.strip().startswith('#') for line in f)
    if not has_deps:
        debug(f'  Empty requirements file, skipping: {requirements_path}')
        return

    # A previous process already resolved this file against this environment
    # and found it satisfied: nothing to install, and nothing to resolve.
    if _verdict_cached(requirements_path, constraints_path):
        debug(f'  Satisfied verdict cached, skipping resolve: {requirements_path}')
        return

    # Start heartbeat early — the dry-run can block on uv's internal lock
    # for minutes, and we need monitorStatus events to keep the task startup
    # timeout alive during that time.
    _start_heartbeat()
    try:
        return _install_requirements_inner(requirements_path, constraints_path)
    finally:
        _stop_heartbeat()


def _install_requirements_inner(requirements_path: str, constraints_path: str):
    """Inner install logic, runs under the heartbeat thread."""
    import importlib

    exe_dir = _get_executable_dir()

    # The dry-run gets the BASE exclusions only. Handing it the family set would make its
    # answer "no member will be installed" by construction, and the trigger below could
    # never fire. See _install_dry_run.
    base_excludes = _write_excludes_file()
    packages = _install_dry_run(requirements_path, constraints_path, base_excludes)
    debug(f'Dry-run found {len(packages)} packages to install: {packages}')

    # What the families owe here. The trigger is this call's dry-run; the install set comes
    # from the environment's resolution.
    target_site = _target_site()
    family_work = _family_work(constraints_path, target_site or active_env().paths.site_packages, packages)

    # A member's presence in the dry-run list is not work the caller owes — it is the
    # family's business. Without the subtraction a member excluded *by design* (plain
    # onnxruntime on Linux) reads as permanently missing and every call reinstalls the world.
    members = pkg_families.all_member_dists()
    real_work = [name for name in packages if pkg_families.normalize(name) not in members]
    family_has_work = any(work.passes for work in family_work)

    if not real_work and not family_has_work:
        debug(f'All requirements satisfied: {requirements_path}')
        # Both of these run ahead of the gate on purpose, and for the same reason: an
        # environment with nothing to do is precisely where a measured-broken build and a
        # namespace loaded from somewhere else go unnoticed. The probe goes first — if it
        # says the environment is wrong, a restart is advice about a different problem.
        _reprove_unproved(constraints_path, active_env().paths.env_dir, target_site)
        shadowed = _shadowing_check(constraints_path)
        if shadowed is not None:
            raise shadowed
        _save_verdict(requirements_path, constraints_path)
        return

    if real_work:
        # Format status message: show up to 5 packages, or 4 + "..." if more than 5
        if len(real_work) <= 5:
            pkg_list = ', '.join(real_work)
        else:
            pkg_list = ', '.join(real_work[:4]) + ', ...'
        updateProgress(f'Installing {pkg_list}')

        # Build uv command — same builder as the scoped install, so the two install paths
        # cannot drift apart on flags. Relative --excludes/-c: uv splits them on whitespace
        # (#1256).
        uv_args = venv_env.build_install_argv(
            uv_path=_uv_abs_path(),
            python_exe=sys.executable,
            requirements_path=requirements_path,
            target_site=target_site,
            excludes_path=os.path.relpath(_write_excludes_file(_family_exclusions(family_work)), exe_dir),
        )
        uv_args.extend(_constraints_args(constraints_path, exe_dir))
        uv_args.extend(_override_args(exe_dir))

        # Run uv and stream output (heartbeat is already running from the caller)
        debug(f'Install: {uv_args}')
        proc = subprocess.Popen(
            uv_args,
            cwd=exe_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
        )
        output_lines = []
        for line in proc.stdout:
            line = line.rstrip()
            output_lines.append(line)
            updateProgress(line)
        proc.wait()

        if proc.returncode != 0:
            output_text = '\n'.join(output_lines)
            error(f'Installation failed: {output_text}')
            # Include last few lines of output in the error for debugging
            last_lines = output_lines[-10:] if len(output_lines) > 10 else output_lines
            error_detail = '\n'.join(last_lines)
            raise RuntimeError(f'Failed to install {requirements_path}\n{error_detail}')

    # The ordered family passes run after the main install and inside the same heartbeat
    # window the caller opened.
    deferred = _handle_families(family_work, constraints_path, target_site)

    # Invalidate import caches so Python can find newly installed packages
    importlib.invalidate_caches()

    # Clear the path importer cache for the install target to force a re-scan
    sys.path_importer_cache.pop(active_env().paths.site_packages, None)

    # The install changed the installed set: record the verdict against it.
    _save_verdict(requirements_path, constraints_path)

    debug(f'Installed: {requirements_path}')

    # The base runtime has no per-install bookkeeping to protect — ensure_constraints wrote
    # its hash back at compile time and each depends() call is gated by its own dry-run — so
    # raising directly here is correct. The overlay path defers instead; see _install_target.
    if deferred is not None:
        raise deferred


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def depends(requirements: Optional[str] = None):
    """
    Install dependencies from a requirements file.

    This is the main entry point for library mode. It:
    1. Bootstraps the environment (pip, uv, platform hacks)
    2. Ensures constraints are up to date
    3. Installs the specified requirements with constraints

    Everything comes from the **active environment** (:func:`active_env`): its lock, its
    constraints, its install target, and its record of what is already installed. Under
    an overlay the requirements land there rather than in the base runtime.

    Args:
        requirements: Path to a requirements.txt file. If None, only
                      ensures the environment and constraints are ready.
    """
    debug(f'depends({requirements})')
    ctx = active_env()

    # Normalize path
    if requirements:
        requirements = os.path.abspath(requirements)
        debug(f'  Path: {requirements}')
        if not os.path.exists(requirements):
            debug('  File not found, skipping')
            return
        # Per environment: installed into overlay A says nothing about overlay B.
        if requirements in ctx.processed:
            debug('  Already processed, skipping')
            return

    with FileLock(ctx.paths.lock_file):
        debug(f'  Lock acquired: {ctx.paths.lock_file}')

        # Phase 1: Bootstrap
        bootstrap()

        # Phase 2: Constraints. An overlay's were compiled by ensure_env_scoped from its
        # own requirement set; recompiling the global union here would be work whose
        # result is then discarded, and it would reintroduce the very cross-environment
        # coupling the overlay exists to remove.
        constraints_path = ctx.paths.constraints if ctx.is_overlay else ensure_constraints()

        # Phase 3: Install if requirements provided
        if requirements:
            _install_requirements(requirements, constraints_path)
            ctx.processed.add(requirements)
            debug(f'  Completed: {os.path.basename(requirements)}')

        # Phase 4: Apply platform-specific hacks (after packages may have been installed)
        _apply_pywin32_hack()


def load_depends(current_file: str, requirements_file: str = 'requirements.txt') -> None:
    """Install a requirements file located alongside the calling module.

    Saves callers the os.path boilerplate of resolving a requirements file next
    to their own module. Equivalent to ``depends(<dir of current_file>/<requirements_file>)``.

    Args:
        current_file: The caller's ``__file__``.
        requirements_file: Requirements filename in that module's directory (default 'requirements.txt').

    Returns:
        None.
    """
    requirements = os.path.join(os.path.dirname(os.path.realpath(current_file)), requirements_file)
    depends(requirements)


# ---------------------------------------------------------------------------
# Per-environment scoped install
# ---------------------------------------------------------------------------


def _overlay_index() -> int:
    """Where the overlay goes on ``sys.path``: ahead of the base runtime, behind shims.

    ``ai/node.py`` puts ``ROCKETRIDE_MOCK`` at ``sys.path[0]`` so node tests import stub
    SDKs instead of the real ones. Inserting the overlay at 0 would put the real library
    in front of every stub, and the node would then reach the live service with a mock
    credential, so land immediately after that entry when it is present.
    """
    mock_path = os.environ.get('ROCKETRIDE_MOCK')
    if mock_path:
        target = os.path.normcase(os.path.abspath(mock_path))
        for index, entry in enumerate(sys.path):
            if entry and os.path.normcase(os.path.abspath(entry)) == target:
                return index + 1
    return 0


def _apply_overlay_path(site: str) -> None:
    """Put ``site`` on ``sys.path`` as **the** overlay, displacing any previous one.

    A swap, not an insert: applying a second environment while the first is still on the
    path leaves everything the first has and the second lacks importable, which is the
    cross-environment leak overlays exist to prevent — and it arrives as a wrong version
    rather than as a missing import.

    The swap only governs **future** imports. Whatever the process already imported from
    the previous overlay stays in ``sys.modules``, so this does not make one interpreter
    safely multi-environment; that is why each environment gets its own child process
    (design §4.10).
    """
    global _inserted_overlay
    import importlib

    with _state_lock:
        previous = _inserted_overlay
        if previous == site and site in sys.path:
            return
        if previous and previous != site:
            while previous in sys.path:
                sys.path.remove(previous)
            sys.path_importer_cache.pop(previous, None)
        if site not in sys.path:
            sys.path.insert(_overlay_index(), site)
        sys.path_importer_cache.pop(site, None)
        _inserted_overlay = site
    importlib.invalidate_caches()


def _compile_constraints_at(combined_path: str, constraints_path: str) -> None:
    """Compile ``combined_path`` -> ``constraints_path``, then align any family present.

    The single compile path for both the global union and one environment's scoped set.
    Compile-then-align rather than a single pass: which families the environment contains is
    only knowable *from* a resolution, so detection reads the compile output and the
    alignment goes back in as requirements for a second pass.
    """
    _run_uv_compile(combined_path, constraints_path)
    _align_families(combined_path, constraints_path)


def _align_families(combined_path: str, constraints_path: str) -> None:
    """Hold every derived family's namespace at one version, and prove it still resolves.

    Appends ``<member>==V`` for the members the environment will hold, under a marked block
    in the already-generated ``combined.txt``, and compiles again. Those lines must be
    *requirements* rather than ``-c`` entries: a constraint on a distribution nothing
    requests is a no-op, so it would never check that V exists for a member no consumer
    names — and checking exactly that is the point of the second pass.

    **Skipped whenever the block would change nothing**, which is the common case and not a
    micro-optimisation: the global compile resolves the whole tree at engine startup, so an
    unconditional second pass would double it on every requirements edit. A family whose
    version is **declared** never reaches here at all — there is no derivation to check, and
    a block naming a member nothing resolves would make the second pass permanent.
    """
    resolved = _read_resolution(constraints_path)
    if not resolved:
        return
    try:
        with open(constraints_path, 'r', encoding='utf-8') as fh:
            annotations = pkg_families.parse_annotations(fh.read())
    except OSError:
        annotations = {}

    facts = _facts()
    blocks: list[str] = []
    aligned: list[tuple[pkg_families.Family, str, tuple[pkg_families.Member, ...]]] = []
    for family in pkg_families.members_in(resolved):
        if family.namespace_version:
            continue
        version = pkg_families.align(family, resolved)
        if not version:
            continue
        members = pkg_families.install_set(family, resolved, facts)
        if pkg_families.redundant(version, members, resolved):
            debug(f'{family.name}: already at {version}, second compile skipped')
            continue
        blocks.append(pkg_families.derived_block(family, version, members))
        aligned.append((family, version, members))

    if not blocks:
        return

    with open(combined_path, 'a', encoding='utf-8') as fh:
        fh.write('\n')
        for block in blocks:
            fh.write(block)

    try:
        _run_uv_compile(combined_path, constraints_path)
    except CompileFailed as failure:
        raise _alignment_failure(aligned, resolved, annotations, failure) from None

    _log_alignment_moves(aligned, resolved, annotations)


def _alignment_failure(aligned, resolved, annotations, failure: CompileFailed) -> RuntimeError:
    """Turn a failed second compile into the right error, which is not always a conflict.

    Pass 1 succeeded on the same inputs minus the derived block, so a pass-2-only failure is
    attributable to the alignment — and uv's own explanation already names the chain
    (*"because X depends on Y==… and you require Y==…"*), which is better than any range this
    repository could enumerate: ``surya-ocr``'s ``opencv-python-headless==4.11.0.86`` lives in
    its wheel metadata, not in any file here.

    But it **checks before claiming**. If uv's failure mentions no family member, this is an
    ordinary compile failure that happened to surface in pass 2, and it is reported as one.
    """
    detail = failure.detail
    lowered = detail.lower()
    for family, version, _members in aligned:
        if not any(pkg_families.normalize(m.dist) in lowered for m in family.members):
            continue
        lowest = _who_set_the_version(family, version, resolved, annotations)
        return RuntimeError(
            f'{family.name}: this environment cannot hold one version of the "{family.import_name}" '
            f'namespace. Alignment derived {version}{lowest}, and re-resolving against it failed.\n'
            f'{detail[:800]}\n'
            'These consumers cannot share an environment. Put one of them in a Virtual '
            'Environment container so each gets its own resolution.'
        )
    return failure


def _who_set_the_version(family, version, resolved, annotations) -> str:
    """Read off the annotation of the member that produced the minimum: `` from X (via Y)``."""
    for member in family.members:
        name = pkg_families.normalize(member.dist)
        if resolved.get(name) == version:
            sources = annotations.get(name, ())
            if sources:
                return f' from {member.dist} (via {", ".join(sources)})'
            return f' from {member.dist}'
    return ''


def _log_alignment_moves(aligned, resolved, annotations) -> None:
    """Say when alignment succeeded but moved someone — not a failure, but not invisible.

    A user whose doctr quietly rides Surya's version should be able to see why and decide to
    split the pipeline. The attribution comes from the ``# via`` annotation of the member that
    produced the minimum, which is the consumer rather than any file of ours.
    """
    for family, version, members in aligned:
        imposed = _who_set_the_version(family, version, resolved, annotations)
        for member in members:
            was = resolved.get(pkg_families.normalize(member.dist))
            if was and was != version:
                monitorStatus(
                    f'{family.name}: {member.dist} moved {was} -> {version}{imposed}; '
                    f'the "{family.import_name}" namespace is shared, so one version has to win.'
                )


def _install_target(requirements_path: str, constraints_path: str, target_site: str) -> Optional[RestartRequired]:
    """Install ``requirements_path`` into the overlay ``target_site`` (uv ``--target``).

    Returns a :class:`RestartRequired` rather than raising it, so the caller can record the
    environment before refusing the run.
    """
    with open(requirements_path, 'r', encoding='utf-8') as f:
        has_deps = any(line.strip() and not line.strip().startswith('#') for line in f)
    if not has_deps:
        debug(f'  Empty scoped requirements, nothing to install: {requirements_path}')
        return None

    exe_dir = _get_executable_dir()
    # No dry-run on this path — it installs the environment's whole combined file in one go,
    # so a family present in the resolution *is* by construction a family being installed.
    # An implementer looking for the dry-run the base path uses will not find one here.
    family_work = _family_work(constraints_path, target_site)
    excludes_rel = os.path.relpath(_write_excludes_file(_family_exclusions(family_work)), exe_dir)
    argv = venv_env.build_install_argv(
        uv_path=_uv_abs_path(),
        python_exe=sys.executable,
        requirements_path=requirements_path,
        target_site=target_site,
        excludes_path=excludes_rel,
    )
    argv.extend(_constraints_args(constraints_path, exe_dir))

    _start_heartbeat()
    try:
        debug(f'Scoped install: {argv}')
        proc = subprocess.Popen(
            argv,
            cwd=exe_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
        )
        lines: list[str] = []
        for line in proc.stdout:
            line = line.rstrip()
            lines.append(line)
            updateProgress(line)
        proc.wait()
        if proc.returncode != 0:
            tail = '\n'.join(lines[-10:])
            error(f'Scoped install failed: {tail}')
            raise RuntimeError(f'Failed scoped install into {target_site}\n{tail}')
        # Inside the heartbeat window on purpose: this half can be several hundred megabytes,
        # and appending it after the `finally` would put it outside the keep-alive and turn a
        # working install into a task-startup timeout.
        deferred = _handle_families(family_work, constraints_path, target_site)
    finally:
        _stop_heartbeat()

    import importlib

    importlib.invalidate_caches()
    sys.path_importer_cache.pop(target_site, None)
    debug(f'Scoped install complete: {target_site}')
    return deferred


def ensure_env_scoped(
    project_id: Optional[str],
    env_id: Optional[str],
    providers,
    has_isolated_group: bool = False,
) -> Optional[str]:
    """Install only what a pipeline environment uses, into its own overlay.

    Discovers the requirement files reachable from ``providers``, compiles and installs
    them into ``venvs/<project_id>/<env_id>/site-packages`` when they drifted, and puts
    that overlay ahead of the base runtime on ``sys.path``.

    Args:
        project_id: Pipe id, or ``None`` for the shared default env.
        env_id: ``'main'`` or a group id. An inherited ``ROCKETRIDE_VENV_ENV_ID`` wins over
            it -- the C++ hook passes the literal ``'main'`` for every process.
        providers: The environment's node ``provider`` strings.
        has_isolated_group: Whether the pipeline has an isolated group (auto mode).

    Returns:
        The overlay ``site-packages`` path, or ``None`` when scoping does not apply —
        the caller then keeps the global-glob behavior.
    """
    exe_dir = _get_executable_dir()

    def _discover(provs):
        # In the deployed engine both the nodes and ai packages sit under the exe dir.
        # Workspace-local nodes live wherever --node_path points, so the scoped resolver
        # has to be told: the startup glob is rooted at the exe dir and cannot reach them.
        # Read from argv rather than sys.path so the condition matches the C++ one exactly.
        local_root = ast_deps.local_nodes_root(engine_args())
        return ast_deps.discover_for_providers(provs, exe_dir, exe_dir, local_root).requirement_files

    def _compile_and_install(plan):
        # Returns the restart-required condition instead of raising it: mark_installed lives
        # on run_scoped_install's side, and recording has to happen before the refusal or the
        # restart repeats the whole build.
        with FileLock(plan.paths.lock_file):  # one lock per overlay, not the global one
            bootstrap()
            _compile_constraints_at(plan.paths.combined, plan.paths.constraints)
            return _install_target(plan.paths.combined, plan.paths.constraints, plan.paths.site_packages)

    def _overlay(paths):
        # The one door that does both halves of a switch: where installs go, and where
        # imports come from. use_env() deliberately does only the first.
        activate_env(register_env(paths.env_dir))
        _apply_overlay_path(paths.site_packages)

    # Resolved at the one production door, not inside run_scoped_install: the resolvers pop
    # os.environ, and that side effect must not hide in a planning function. Unconditional --
    # run_scoped_install early-returns under =0, but the consume must still happen.
    env_id = venv_env.resolve_env_id(env_id)
    # OR, not replace: the parameter side is always False today (the sole caller is C++ passing
    # three positional arguments), so the variable is in practice the only live input. The OR keeps
    # the signature honest for a future non-C++ caller rather than guarding a real second source.
    has_isolated_group = has_isolated_group or venv_env.isolated_from_env()

    site = venv_env.run_scoped_install(
        exe_dir,
        project_id,
        env_id,
        providers,
        discover=_discover,
        compile_and_install=_compile_and_install,
        has_isolated_group=has_isolated_group,
        on_overlay=_overlay,
    )

    # Outside the drift gate, which is the point: an overlay whose hash matched was never
    # rebuilt, so nothing on the install path ran either to re-prove a build a previous run
    # measured as broken, or to notice that this process already holds the namespace at
    # another version. Both are the *common* shape, not a corner.
    if site is not None:
        paths = active_env().paths
        _reprove_unproved(paths.constraints, paths.env_dir, site)
        shadowed = _shadowing_check(paths.constraints)
        if shadowed is not None:
            raise shadowed
    return site


# ---------------------------------------------------------------------------
# Main Mode
# ---------------------------------------------------------------------------


def main():
    """
    Run the main command-line entry point.

    Usage: engine depends.py [uv pip arguments]

    After bootstrapping and ensuring constraints, passes all arguments
    through to 'uv pip'. Falls back to standard pip if uv can't build
    source distributions due to virtualenv creation issues.

    A CLI invocation has no pipeline, so the active environment here is the base runtime.
    """
    ctx = active_env()

    with FileLock(ctx.paths.lock_file):
        # Bootstrap environment
        bootstrap()

        # Ensure constraints are ready
        ensure_constraints()

        # Pass through to uv pip
        if len(sys.argv) > 1:
            if not _uv_available():
                sys.exit(1)

            exe_dir = _get_executable_dir()

            # Build uv args
            uv_args = [_uv_abs_path(), 'pip'] + sys.argv[1:] + ['--python', sys.executable]

            # --index-strategy is only valid for install, compile, sync commands
            is_install_cmd = sys.argv[1] in ('install', 'compile', 'sync')
            if is_install_cmd:
                uv_args += ['--index-strategy', 'unsafe-best-match']

            # For install/sync commands, add constraints file if available
            if sys.argv[1] in ('install', 'sync'):
                uv_args.extend(_constraints_args(ctx.paths.constraints, exe_dir))

            # Run uv
            result = subprocess.run(uv_args, cwd=exe_dir)
            sys.exit(result.returncode)


if __name__ == '__main__':
    main()
