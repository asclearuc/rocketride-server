# =============================================================================
# MIT License — Copyright (c) 2026 Aparavi Software AG
# (full text in depends.py)
# =============================================================================

"""Per-environment overlay layout, scoped-install planning, and reclamation.

Every environment (``main`` or an isolated group) gets its own overlay under
``<exe>/venvs/<proj>/<env>/`` holding ``site-packages/`` plus its own
``combined.txt`` / ``constraints.txt`` / ``requirements.hash`` and an install lock.
Ids are shortened to stay under the Windows ``MAX_PATH`` limit.

:class:`EnvContext` bundles those paths with the set of requirement files already
installed into that environment, so switching environments is one act rather than four
independent ones.

The module also **destroys** environments (``purge_env`` / ``delete_env`` /
``delete_project``) and enumerates them (``list_envs``), and takes OS file locks to do
it safely. Say so here rather than advertising planning alone: the next person otherwise
concludes this file is read-only and puts the destructive half somewhere else.

Stdlib only — no engine, ``uv`` or subprocess, and no ``sys.path`` mutation — so it is
testable in isolation; ``depends.py`` layers the actual compile/install on top.
The hash/combine helpers mirror ``depends.py`` to avoid importing it (that would
pull in ``engLib``); keep them in sync.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from typing import Optional

# Load-bearing, not style: a bare ``import msvcrt`` makes this module UNIMPORTABLE on Linux,
# and this is precisely the module the POSIX test half loads -- the mistake would surface as
# the whole suite erroring at collection, not as a failing lock test. Mirrors depends.py.
if os.name == 'nt':
    import msvcrt
else:
    import fcntl

# Stdlib-only sibling, so importing it keeps this module free of engLib (the reason the
# hash/combine helpers below are mirrored from depends rather than imported).
import pkg_families

# env_id for the always-present base-of-the-pipeline environment.
MAIN_ENV = 'main'
# Used when there is no project_id (engtest, CLI, ad-hoc runs).
DEFAULT_ID = 'default'

_ID_MAX = 8  # readable prefix kept from a long id segment
_HASH_LEN = 8  # hex chars of sha1 over the full id, appended whenever the prefix is lossy


# ---------------------------------------------------------------------------
# the ROCKETRIDE_SERVER_USE_VENV switch
# ---------------------------------------------------------------------------

# Resolved states of the switch.
USE_AUTO = 'auto'  # unset: scope per-env only if the pipeline has an isolated group
USE_OFF = 'off'  # '0': never partition/scope — today's global-glob behavior (legacy)
USE_ON = 'on'  # '1': force the scoped/venv machinery on


# First real-environment resolution, kept for the life of the process. See use_venv_mode.
_MODE_CACHE: Optional[str] = None


def _resolve_mode(env) -> str:
    raw = env.get('ROCKETRIDE_SERVER_USE_VENV')
    if raw is None:
        return USE_AUTO
    raw = raw.strip()
    if raw == '0':
        return USE_OFF
    if raw == '1':
        return USE_ON
    return USE_AUTO


def use_venv_mode(env: Optional[dict] = None) -> str:
    """Resolve the ``ROCKETRIDE_SERVER_USE_VENV`` switch to ``auto`` / ``off`` / ``on``.

    A process-init input, not a live control channel: the first resolution over the real
    environment is cached and every later call returns it. Without that, node code running
    inside the engine could rewrite ``os.environ`` before another node's ``depends()`` and
    move the startup glob -- reaching the base runtime, which outlives the run.

    An explicitly passed ``env`` bypasses the cache and is resolved fresh every time.

    Args:
        env: Environment mapping to read (defaults to ``os.environ``); injectable
            for tests.

    Returns:
        ``USE_OFF`` for ``'0'``, ``USE_ON`` for ``'1'``, ``USE_AUTO`` otherwise
        (unset / any other value).
    """
    global _MODE_CACHE
    if env is not None:
        return _resolve_mode(env)
    if _MODE_CACHE is None:
        _MODE_CACHE = _resolve_mode(os.environ)
    return _MODE_CACHE


def scoping_enabled(mode: str, has_isolated_group: bool) -> bool:
    """Whether per-environment scoping applies for this run.

    ``off`` -> never (legacy global-glob); ``on`` -> always; ``auto`` -> only when
    the pipeline opts in via an isolated group.
    """
    if mode == USE_OFF:
        return False
    if mode == USE_ON:
        return True
    return has_isolated_group


# ---------------------------------------------------------------------------
# which environment this process installs into
# ---------------------------------------------------------------------------

# Mirrored in ai/modules/task/venv_spawn.py (which cannot import this module) -- keep in sync.
VENV_ENV_ID_ENV = 'ROCKETRIDE_VENV_ENV_ID'

_UNREAD = object()
# The INHERITED value, frozen at first read -- not the resolved result, which depends on
# ``passed`` and must stay a fresh resolution on every call.
_ENV_ID_CACHE = _UNREAD


def resolve_env_id(passed: Optional[str], env: Optional[dict] = None) -> Optional[str]:
    """Resolve which environment this process installs into.

    The inherited value **wins** over ``passed``: the only production caller is the C++
    endpoint hook, which hard-codes ``'main'`` for every process. Empty is absent.

    Consumed on first read -- frozen, then popped from ``os.environ`` -- so neither node code
    nor anything a node spawns can observe or change it. An explicitly passed ``env`` is
    resolved fresh and never popped.
    """
    global _ENV_ID_CACHE
    if env is not None:
        return (env.get(VENV_ENV_ID_ENV) or '').strip() or passed
    if _ENV_ID_CACHE is _UNREAD:
        _ENV_ID_CACHE = (os.environ.pop(VENV_ENV_ID_ENV, '') or '').strip()
    return _ENV_ID_CACHE or passed


# Mirrored in ai/modules/task/venv_spawn.py -- keep in sync.
VENV_ISOLATED_ENV = 'ROCKETRIDE_VENV_ISOLATED'

_ISOLATED_CACHE = _UNREAD


def _truthy(raw) -> bool:
    return (raw or '').strip().lower() in ('1', 'true', 'yes')


def isolated_from_env(env: Optional[dict] = None) -> bool:
    """Whether this process's document contains an isolated group.

    The raw document **fact**, not a resolved decision: :func:`scoping_enabled` still decides, so
    however stale this value gets it cannot switch scoping on under ``=0``. Consumed on first read
    -- frozen, then popped -- like the environment id.

    Absent, empty and ``'0'`` all mean False; there is no third state to distinguish, unlike the
    mode switch where unset genuinely differs from ``'0'``.
    """
    global _ISOLATED_CACHE
    if env is not None:
        return _truthy(env.get(VENV_ISOLATED_ENV))
    if _ISOLATED_CACHE is _UNREAD:
        _ISOLATED_CACHE = _truthy(os.environ.pop(VENV_ISOLATED_ENV, ''))
    return _ISOLATED_CACHE


def _reset_venv_env_cache() -> None:
    """Drop the process-init caches so the next call reads the environment again.

    Tests only -- production resolves once by design.
    """
    global _MODE_CACHE, _ENV_ID_CACHE, _ISOLATED_CACHE
    _MODE_CACHE = None
    _ENV_ID_CACHE = _UNREAD
    _ISOLATED_CACHE = _UNREAD


# ---------------------------------------------------------------------------
# id shortening + directory layout
# ---------------------------------------------------------------------------


def short_id(identifier: Optional[str], length: int = _ID_MAX) -> str:
    """Shorten a stable id to a filesystem-safe, MAX_PATH-friendly segment.

    An id that survives cleaning unchanged and fits in ``length`` (``main``) is returned
    as-is. Everything else becomes ``<prefix>-<sha1[:8]>``, the digest taken over the
    **full** id so that ids sharing a prefix stay distinct.

    Truncating alone is only safe for high-entropy ids. Readable ones spend the whole
    prefix on their common part — ``test-tool_daytona`` and ``test-tool_tavily`` both
    truncate to ``testtool`` — and the two projects then share an overlay and overwrite
    each other's ``constraints.txt``. ``None`` / empty -> :data:`DEFAULT_ID`.
    """
    if not identifier:
        return DEFAULT_ID
    text = str(identifier)
    cleaned = re.sub(r'[^A-Za-z0-9]', '', text).lower()
    if not cleaned:
        return DEFAULT_ID
    if len(cleaned) <= length and cleaned == text.lower():
        return cleaned
    digest = hashlib.sha1(text.encode('utf-8')).hexdigest()[:_HASH_LEN]
    return f'{cleaned[:length]}-{digest}'


def venv_root(exe_dir: str) -> str:
    """The top-level ``venvs/`` directory (sibling of ``lib/`` and ``cache/``)."""
    return os.path.join(exe_dir, 'venvs')


def env_dir(exe_dir: str, project_id: Optional[str], env_id: Optional[str]) -> str:
    """Overlay directory for one environment: ``<exe>/venvs/<proj>/<env>``.

    ``project_id`` is the pipe id (``config.pipeline.project_id``); ``env_id`` is
    ``main`` or a group node id. Both are shortened. Missing ``project_id`` falls
    back to a shared ``default`` project.
    """
    return os.path.join(venv_root(exe_dir), short_id(project_id), short_id(env_id or MAIN_ENV))


@dataclass
class EnvPaths:
    """Resolved on-disk paths for one environment overlay."""

    env_dir: str
    site_packages: str
    combined: str
    constraints: str
    hash_file: str
    lock_file: str


def env_paths(directory: str) -> EnvPaths:
    """Return the standard file layout inside an environment overlay ``directory``."""
    return EnvPaths(
        env_dir=directory,
        site_packages=os.path.join(directory, 'site-packages'),
        combined=os.path.join(directory, 'combined.txt'),
        constraints=os.path.join(directory, 'constraints.txt'),
        hash_file=os.path.join(directory, 'requirements.hash'),
        lock_file=os.path.join(directory, 'install.lock'),
    )


def base_paths(cache_dir: str, site_packages: str) -> EnvPaths:
    """Return the same layout for the **base runtime**, whose files are not co-located.

    An overlay keeps everything under one directory; the base keeps its metadata in
    ``<exe>/cache`` but installs into ``<exe>/lib/site-packages``, so :func:`env_paths`
    cannot express it and the two paths are passed separately.
    """
    return EnvPaths(
        env_dir=cache_dir,
        site_packages=site_packages,
        combined=os.path.join(cache_dir, 'combined.txt'),
        constraints=os.path.join(cache_dir, 'constraints.txt'),
        hash_file=os.path.join(cache_dir, 'requirements.hash'),
        lock_file=os.path.join(cache_dir, 'install.lock'),
    )


# env_dir of the base runtime context; overlays are keyed by their directory.
BASE_KEY = 'base'


@dataclass
class EnvContext:
    """Everything that must change together when the active environment changes.

    The install lock, the constraints file, the ``uv --target`` destination and the
    record of what is already installed are one decision, not four: applying an
    environment means switching all of them, and holding them in separate module
    globals is what made a second environment in one process unsafe.

    ``processed`` is per environment on purpose — the same requirements file installed
    into overlay A says nothing about overlay B.
    """

    key: str
    paths: EnvPaths
    is_overlay: bool
    processed: set = field(default_factory=set)


# ---------------------------------------------------------------------------
# hash-drift planning (per-env; mirrors depends.py helpers)
# ---------------------------------------------------------------------------


def requirements_hash(req_files: list[str]) -> str:
    """Fast content hash of a requirement-file set (path + size + mtime_ns).

    Mirrors ``depends._compute_hash`` so a per-env overlay reuses the same drift
    semantics without importing ``depends`` (which needs ``engLib``).
    """
    hasher = hashlib.md5()
    for path in sorted(req_files):
        info = os.stat(path)  # not `stat`: that name is the stdlib module here
        hasher.update(f'{path}:{info.st_size}:{info.st_mtime_ns}\n'.encode())
    return hasher.hexdigest()


def _include_target(line: str) -> Optional[str]:
    """The path a ``-r`` / ``--requirement`` line refers to, or ``None``.

    (``-c`` / ``--constraint`` includes have the same relative-path problem but do not
    occur in this tree; add them here if they ever do.)
    """
    text = line.strip()
    for prefix in ('--requirement=', '--requirement ', '-r ', '-r'):
        if text.startswith(prefix):
            target = text[len(prefix) :].strip()
            return target or None
    return None


def resolve_includes(req_files: list[str]) -> list[str]:
    """Expand ``-r`` includes transitively: every file whose content ends up resolved.

    A requirement file may pull in another with ``-r other.txt``. Those files shape the
    resolution just as much as the listed ones, so they must be part of the drift hash —
    otherwise editing an included file never triggers a rebuild.

    Args:
        req_files: The discovered requirement files.

    Returns:
        ``req_files`` plus every transitively included file, de-duplicated, in
        first-seen order.

    Raises:
        FileNotFoundError: An include points at a file that does not exist. Better here,
            naming the referring file, than later as a ``uv`` error naming ``combined.txt``.
    """
    seen: list[str] = []
    known: set[str] = set()
    queue = list(req_files)
    while queue:
        path = os.path.abspath(queue.pop(0))
        if path in known:
            continue
        known.add(path)
        seen.append(path)
        for included in _includes_of(path):
            queue.append(included)
    return seen


def _includes_of(path: str) -> list[str]:
    """Absolute paths of the ``-r`` includes in ``path`` (resolved against its directory)."""
    out: list[str] = []
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            lines = fh.readlines()
    except OSError:
        return out
    base = os.path.dirname(os.path.abspath(path))
    for line in lines:
        target = _include_target(line)
        if target is None:
            continue
        resolved = os.path.abspath(os.path.join(base, target))
        if not os.path.isfile(resolved):
            raise FileNotFoundError(f'{path}: included requirements file not found: {target}')
        out.append(resolved)
    return out


def write_combined(req_files: list[str], combined_path: str) -> None:
    r"""Concatenate ``req_files`` into ``combined_path`` (mirrors depends helper).

    ``-r`` includes are rewritten to absolute paths. ``uv`` resolves them relative to the
    file holding the line, and this file holds the bytes of requirement files from
    elsewhere in the tree — a relative include would be looked for next to
    ``combined_path`` and the compile would fail there instead of at the node.

    The rewritten path uses forward slashes even on Windows: a requirement file treats
    ``\`` as an escape character, so ``-r C:\x\y.txt`` reaches uv as ``C:xy.txt``
    (verified against the shipped uv). Forward slashes are accepted on every platform.
    """
    with open(combined_path, 'w', encoding='utf-8') as out:
        for path in req_files:
            out.write(f'# Source: {path}\n')
            source_dir = os.path.dirname(os.path.abspath(path))
            with open(path, 'r', encoding='utf-8') as inp:
                for line in inp:
                    target = _include_target(line)
                    if target is not None:
                        resolved = os.path.abspath(os.path.join(source_dir, target))
                        line = f'-r {resolved.replace(os.sep, "/")}\n'
                    out.write(line)
            out.write('\n')


@dataclass
class InstallPlan:
    """Result of planning a scoped install for one environment."""

    paths: EnvPaths
    current_hash: str
    needs_rebuild: bool


def plan_install(
    exe_dir: str,
    project_id: Optional[str],
    env_id: Optional[str],
    req_files: list[str],
    create: bool = True,
) -> InstallPlan:
    """Plan a scoped install: resolve the overlay, compute drift, write the combined file.

    Prepares what the caller needs to run ``uv``, or to skip when already up to date.

    Args:
        exe_dir: The engine executable directory (``dirname(sys.executable)``).
        project_id: Pipe id, or ``None`` for the shared ``default`` project.
        env_id: ``main`` or a group id.
        req_files: The environment's requirement-file set (from ``ast_deps``).
        create: Create the overlay directory tree when ``True``.

    Returns:
        An :class:`InstallPlan`. ``needs_rebuild`` is ``True`` when the stored hash
        differs (or the constraints file is missing); write the combined file only
        then, run ``uv``, and finally :func:`mark_installed`.
    """
    paths = env_paths(env_dir(exe_dir, project_id, env_id))
    if create:
        os.makedirs(paths.site_packages, exist_ok=True)

    # Hash over the includes too: a `-r`-referenced file shapes the resolution and must
    # therefore be able to invalidate it.
    current = requirements_hash(resolve_includes(req_files)) if req_files else ''
    # And over the declarations of the families this environment holds, which live in
    # lib/pkg_families/*.py where the file walk above never looks. Read from the environment's
    # *previous* resolution, the only thing that knows which families it contains before the
    # compile that would tell us again. An environment holding none keeps its bytes unchanged.
    if current:
        current = pkg_families.combine_hash(current, pkg_families.hash_contribution(paths.constraints))
    stored = _read_text(paths.hash_file)
    needs = (current != stored) or not os.path.exists(paths.constraints)

    if needs and create and req_files:
        write_combined(req_files, paths.combined)

    return InstallPlan(paths=paths, current_hash=current, needs_rebuild=needs)


def mark_installed(plan: InstallPlan) -> None:
    """Persist the plan's hash after a successful install (drift baseline)."""
    with open(plan.paths.hash_file, 'w', encoding='utf-8') as fh:
        fh.write(plan.current_hash)


# ---------------------------------------------------------------------------
# uv install-argv builder (scoped to the overlay via --target)
# ---------------------------------------------------------------------------


def build_install_argv(
    uv_path: str,
    python_exe: str,
    requirements_path: Optional[str] = None,
    target_site: Optional[str] = None,
    constraints_path: Optional[str] = None,
    excludes_path: Optional[str] = None,
    specs: Optional[list[str]] = None,
    reinstall_packages: tuple[str, ...] = (),
) -> list[str]:
    """Construct the ``uv pip install`` argv, optionally targeting an overlay.

    The single builder for **every** install path: ``target_site=None`` installs into the
    base runtime, a path installs into that environment's overlay. Keeping them one
    function is the point — while there were two, a flag added to one silently diverged
    from the other.

    Takes **either** a requirements file (``-r``) or explicit ``specs``. The ordered
    package-family passes install named members rather than a file, and giving them a
    second builder — or a throwaway temp requirements file whose only purpose is to satisfy
    this signature — is how a third divergence would start.

    ``reinstall_packages`` maps to ``--reinstall-package``. Order alone does not make the
    widest member of a family win: the *write* does, and uv skips the write for a
    distribution it already considers satisfied.

    Pure (returns the list; the caller runs it) so it is unit-testable. ``uv`` splits
    ``-c`` and ``--excludes`` values on whitespace, so pass those already relative to the
    directory the command will run in.
    """
    if bool(requirements_path) == bool(specs):
        raise ValueError('build_install_argv takes exactly one of requirements_path or specs')

    argv = [
        uv_path,
        'pip',
        'install',
        '--python',
        python_exe,
    ]
    if requirements_path:
        argv += ['-r', requirements_path]
    else:
        argv += list(specs or ())
    argv += [
        '--index-strategy',
        'unsafe-best-match',
        '--no-build-isolation',
    ]
    for name in reinstall_packages:
        argv += ['--reinstall-package', name]
    if target_site:
        argv += ['--target', target_site]
    if constraints_path:
        argv += ['-c', constraints_path]
    if excludes_path:
        argv += ['--excludes', excludes_path]
    return argv


# ---------------------------------------------------------------------------
# scoped-install orchestration (side effects injected -> unit-testable)
# ---------------------------------------------------------------------------


def run_scoped_install(
    exe_dir: str,
    project_id: Optional[str],
    env_id: Optional[str],
    providers,
    *,
    discover,
    compile_and_install,
    mode: Optional[str] = None,
    has_isolated_group: bool = False,
    on_overlay=None,
) -> Optional[str]:
    """Orchestrate a scoped per-environment install; return the overlay site-packages.

    Control flow only — the side effects are injected, so this is testable without an
    engine or ``uv``: ``discover(providers)`` yields the env's requirement files,
    ``compile_and_install(plan)`` runs uv, and the optional ``on_overlay(paths)`` applies
    the overlay (it receives the whole :class:`EnvPaths`, not just the site-packages
    directory, so the caller does not have to re-derive the constraints path from it).

    ``compile_and_install`` may **return** an exception instead of raising one, for the case
    where the environment is correct but this process cannot use it (a shared namespace
    already imported here). Recording has to come first there: raise before
    :func:`mark_installed` and the restart the message asks for repeats the whole build, and
    the operator watches the fix appear not to take. The exception is constructed by the
    caller and merely re-raised here, so this module stays free of engine error types.

    Installs only when the requirement set drifted. Returns ``None`` when scoping does
    not apply, leaving the caller on the base runtime.
    """
    if mode is None:
        mode = use_venv_mode()
    if not scoping_enabled(mode, has_isolated_group):
        return None
    req_files = list(discover(providers))
    if not req_files:
        # Nothing to scope (source-only endpoint, or all-native nodes): must not try to
        # compile an absent combined file — leave the base runtime in place.
        return None
    plan = plan_install(exe_dir, project_id, env_id, req_files)
    if plan.needs_rebuild:
        deferred = compile_and_install(plan)
        mark_installed(plan)
        if isinstance(deferred, BaseException):
            raise deferred
    if on_overlay is not None:
        on_overlay(plan.paths)
    return plan.paths.site_packages


# ---------------------------------------------------------------------------
# internal
# ---------------------------------------------------------------------------


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return fh.read().strip()
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
# reclamation: purge / delete / list
# ---------------------------------------------------------------------------


class EnvBusy(RuntimeError):
    """An environment could not be reclaimed because something still holds it."""


class _EnvLock:
    """Non-blocking exclusive lock on one environment's ``install.lock``.

    **Same primitive family as** ``depends.FileLock`` -- ``msvcrt`` on Windows,
    ``fcntl.flock`` on POSIX -- because ``flock`` and ``lockf`` do not see each other on Linux,
    so the wrong one evaporates the gate on exactly one platform, invisibly.

    **Not** ``depends.FileLock`` itself, for two independent reasons either of which settles it:
    that lock **blocks** (polls forever with a status sidecar), which is what a protocol call
    must never do -- a purge issued behind a live install would hang instead of reporting busy;
    and this module deliberately imports nothing from ``depends``, since that would pull in
    ``engLib``.
    """

    def __init__(self, path: str):
        self._path = path
        self._fh = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        fh = open(self._path, 'a+b')
        try:
            if os.name == 'nt':
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise EnvBusy(f'environment is busy (install in progress): {self._path}') from exc
        self._fh = fh
        return self

    def __exit__(self, *_exc):
        if self._fh is None:
            return False
        try:
            if os.name == 'nt':
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None
        return False


def _force_remove(path: str) -> None:
    """Remove one file, retrying once after clearing a read-only bit.

    The chmod retry is cheap insurance, **not** the load-bearing case: measured across 40
    overlays (77,191 files) ``uv`` leaves **zero** read-only files. What actually fails here is a
    resident engine holding an imported ``.pyd``/``.dll`` open, and no chmod frees that -- so the
    second failure is **named**, not retried. A retry loop that hides an open file turns a clear
    "stop the engine first" into a hang, on the one platform where this is the expected outcome.
    """
    try:
        os.unlink(path)
        return
    except FileNotFoundError:
        return
    except PermissionError:
        pass
    try:
        os.chmod(path, stat.S_IWRITE)
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise EnvBusy(f'cannot remove {path} - a process may still hold it open') from exc


def _empty_dir(directory: str, keep=()) -> None:
    """Delete everything inside ``directory``, keeping the named top-level entries.

    Hand-rolled rather than ``shutil.rmtree``: neither ``onexc=`` nor ``onerror=`` is right on
    both interpreters this module must run on -- the engine embeds 3.12 (where ``onerror`` is
    deprecated) and WSL ships 3.10 (where ``onexc`` is a ``TypeError``). An explicit try/except
    is version-neutral.
    """
    if not os.path.isdir(directory):
        return
    for entry in os.scandir(directory):
        if entry.name in keep:
            continue
        if entry.is_dir(follow_symlinks=False):
            _empty_dir(entry.path)
            try:
                os.rmdir(entry.path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise EnvBusy(f'cannot remove {entry.path} - a process may still hold it open') from exc
        else:
            _force_remove(entry.path)


def _resolve_segment(parent: str, given: Optional[str]) -> str:
    """Literal-first: an exact on-disk name wins, otherwise shorten.

    ``list_envs`` can only report what is on disk, and what is on disk is ``short_id`` of both
    segments. Feeding a listed name back into ``purge`` and applying ``short_id`` **again** is not
    idempotent: ``chaindaa-abc12345`` cleans to 16 characters and re-hashes to a *different,
    plausible* directory, which is absent, which is idempotent success -- so the command would
    report success and delete nothing, and a ``list -> purge -> list`` check would not even
    notice, because the entry it re-lists is the one that was never touched.
    """
    name = (given or '').strip()
    if name and os.path.isdir(os.path.join(parent, name)):
        return name
    return short_id(name or None)


def resolve_project_dir(exe_dir: str, project_id: Optional[str]) -> str:
    """``venvs/<project>``, resolved literal-first."""
    root = venv_root(exe_dir)
    return os.path.join(root, _resolve_segment(root, project_id))


def resolve_env_dir(exe_dir: str, project_id: Optional[str], env_id: Optional[str]) -> str:
    """``venvs/<project>/<env>``, both segments resolved literal-first."""
    project = resolve_project_dir(exe_dir, project_id)
    return os.path.join(project, _resolve_segment(project, env_id))


def purge_env(exe_dir: str, project_id: Optional[str], env_id: Optional[str]) -> bool:
    """Empty one environment's ``site-packages``, keeping its compiled inputs.

    Returns ``False`` when the environment does not exist -- an absent target is idempotent
    success, not an error.

    **Drops ``requirements.hash`` FIRST, then wipes.** The reverse order turns any mid-wipe
    failure into an environment that is half-deleted yet still marked installed, which the next
    run happily imports from; hash-first makes the worst case a redundant reinstall.

    ``combined.txt`` and ``constraints.txt` survive (§4.10). That does **not** make the next run
    cheap: with the hash gone ``plan_install`` rebuilds regardless, so the constraints are
    recompiled from scratch and the post-purge run is a full compile-and-install. Never
    "optimise" this by keeping the hash -- that is the inverse of this rule and hands the next
    run an empty ``site-packages`` marked as installed.
    """
    directory = resolve_env_dir(exe_dir, project_id, env_id)
    if not os.path.isdir(directory):
        return False
    paths = env_paths(directory)
    with _EnvLock(paths.lock_file):
        _force_remove(paths.hash_file)
        _empty_dir(paths.site_packages)
    return True


def _delete_env_dir(directory: str) -> bool:
    """Delete one environment overlay **by path**. Shared by both delete entry points.

    Windows cannot remove the lock file while it is held, hence the order: acquire -> delete
    everything except ``install.lock`` -> release -> unlink the lock best-effort -> remove the
    now-empty directory. A failure at either of the last two steps is **not** an error: the
    environment is already gone in every sense that matters.
    """
    if not os.path.isdir(directory):
        return False
    paths = env_paths(directory)
    with _EnvLock(paths.lock_file):
        _empty_dir(directory, keep={os.path.basename(paths.lock_file)})
    try:
        os.unlink(paths.lock_file)
        os.rmdir(directory)
    except OSError:
        pass
    return True


def delete_env(exe_dir: str, project_id: Optional[str], env_id: Optional[str]) -> bool:
    """Delete one environment overlay entirely. Absent target -> ``False``, not an error."""
    return _delete_env_dir(resolve_env_dir(exe_dir, project_id, env_id))


def delete_project(exe_dir: str, project_id: Optional[str]) -> int:
    """Delete every environment of one project **and the project directory itself**.

    §4.10's operation C is "delete the whole ``venvs/<project_id>/`` subtree", not "empty it":
    iterating environments alone would leave a childless project directory behind, and that is
    not cosmetic -- it decides what the closing ``list`` shows. ``list_envs`` skips childless
    project directories for the same reason, so the two answers agree.

    Iterates through the **path-level** helper because ``short_id`` is not idempotent: a
    name-level loop would re-shorten each directory name it just read off disk, land on a
    different plausible name, find it absent, and report success having deleted nothing.

    Returns the number of environments removed.
    """
    root = resolve_project_dir(exe_dir, project_id)
    if not os.path.isdir(root):
        return 0
    removed = 0
    for entry in sorted(os.scandir(root), key=lambda e: e.name):
        if entry.is_dir(follow_symlinks=False) and _delete_env_dir(entry.path):
            removed += 1
    try:
        os.rmdir(root)
    except OSError:
        pass
    return removed


def _dir_size(directory: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(directory):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def list_envs(exe_dir: str, project_id: Optional[str] = None, sizes: bool = False):
    """Enumerate installed environments. Read-only, no lock.

    Keys are the **wire** spelling, since these rows travel to a client as they are.

    ``sizes`` is opt-in, and the numbers rather than taste say why: measured here, **154**
    populated ``site-packages`` directories with ~3520 files in a sampled one, so sizing
    everything is on the order of half a million ``stat`` calls -- on the call a canvas makes
    most often. Default is a cheap ``scandir`` with no recursion; size only when someone is
    actually deciding what to reclaim.
    """
    root = venv_root(exe_dir)
    rows = []
    if not os.path.isdir(root):
        return rows
    wanted = _resolve_segment(root, project_id) if project_id else None
    for project in sorted(os.scandir(root), key=lambda e: e.name):
        if not project.is_dir(follow_symlinks=False):
            continue
        if wanted is not None and project.name != wanted:
            continue
        for env in sorted(os.scandir(project.path), key=lambda e: e.name):
            if not env.is_dir(follow_symlinks=False):
                continue
            row = {
                'projectId': project.name,
                'envId': env.name,
                'installed': os.path.isfile(os.path.join(env.path, 'requirements.hash')),
            }
            if sizes:
                row['bytes'] = _dir_size(os.path.join(env.path, 'site-packages'))
            rows.append(row)
    return rows
