# =============================================================================
# MIT License — Copyright (c) 2026 Aparavi Software AG
# (full text in depends.py)
# =============================================================================

"""Per-environment overlay layout and scoped-install planning.

Every environment (``main`` or an isolated group) gets its own overlay under
``<exe>/venvs/<proj>/<env>/`` holding ``site-packages/`` plus its own
``combined.txt`` / ``constraints.txt`` / ``requirements.hash`` and an install lock.
Ids are shortened to stay under the Windows ``MAX_PATH`` limit.

:class:`EnvContext` bundles those paths with the set of requirement files already
installed into that environment, so switching environments is one act rather than four
independent ones.

Stdlib only — no engine, ``uv``, subprocess or ``sys.path`` mutation — so it is
testable in isolation; ``depends.py`` layers the actual compile/install on top.
The hash/combine helpers mirror ``depends.py`` to avoid importing it (that would
pull in ``engLib``); keep them in sync.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Optional

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


def _reset_venv_env_cache() -> None:
    """Drop the process-init caches so the next call reads the environment again.

    Tests only -- production resolves once by design.
    """
    global _MODE_CACHE, _ENV_ID_CACHE
    _MODE_CACHE = None
    _ENV_ID_CACHE = _UNREAD


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
        stat = os.stat(path)
        hasher.update(f'{path}:{stat.st_size}:{stat.st_mtime_ns}\n'.encode())
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
    requirements_path: str,
    target_site: Optional[str] = None,
    constraints_path: Optional[str] = None,
    excludes_path: Optional[str] = None,
) -> list[str]:
    """Construct the ``uv pip install`` argv, optionally targeting an overlay.

    The single builder for both install paths: ``target_site=None`` installs into the
    base runtime, a path installs into that environment's overlay. Keeping them one
    function is the point — while there were two, a flag added to one silently diverged
    from the other.

    Pure (returns the list; the caller runs it) so it is unit-testable. ``uv`` splits
    ``-c`` and ``--excludes`` values on whitespace, so pass those already relative to the
    directory the command will run in.
    """
    argv = [
        uv_path,
        'pip',
        'install',
        '--python',
        python_exe,
        '-r',
        requirements_path,
        '--index-strategy',
        'unsafe-best-match',
        '--no-build-isolation',
    ]
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
        compile_and_install(plan)
        mark_installed(plan)
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
