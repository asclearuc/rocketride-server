"""Local venv-child spawn helpers (virtual-environments feature, §7 step 7).

A venv child is a per-run sibling ``engine`` subprocess that runs one isolated pipeline
group. It is kept alive by a resident ``venv_source_stub`` source hosting a loopback
``/venv/pipe`` WebServer; the main engine's ``venv`` client nodes dial it. These are the
side-effect-light, unit-testable pieces (env construction, url injection, the two-phase
kill); the ``Task``-bound orchestration (assign ports, write task files, spawn, readiness,
teardown) lives in ``task_engine.py`` and reuses these.

Reliability is the main engine's, extended to N children: children are spawned with
``--autoterm`` (a C++ stdin monitor exits the child if the server process dies) and torn
down explicitly by the owning ``Task`` (they are resident and never self-stop); the child
processes are reaped via ``wait()`` so no zombies are left.
"""

import asyncio
import os
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

# Per-run shared bridge secret. Delivered to the main engine and every child via an
# inherited env var (never argv, never the on-disk task file); the child's /venv/pipe
# route verifies it and the main-side venv clients present it. (§4.5)
VENV_TOKEN_ENV = 'ROCKETRIDE_VENV_TOKEN'
# Points a child at its per-environment ``sys.path`` overlay (§4.11); consumed by the
# engine bootstrap in ``depends.py``. Unset -> the child runs on the base runtime.
VENV_SITE_ENV = 'ROCKETRIDE_VENV_SITE'


@dataclass
class VenvChild:
    """A spawned venv-child subprocess and the run resources it owns."""

    env_id: str
    name: str
    process: 'asyncio.subprocess.Process'
    port: int
    tmpfile: str
    drains: List['asyncio.Task'] = field(default_factory=list)
    # Ring of the child's most recent stdout/stderr lines, so a startup failure can report
    # the child's own error (e.g. a dependency conflict) instead of only its exit code.
    tail: Deque[str] = field(default_factory=lambda: deque(maxlen=25))


def overlay_site(exe_dir: str, project_id: Optional[str], env_id: str) -> Optional[str]:
    """The child's ``ROCKETRIDE_VENV_SITE`` overlay path, or ``None`` to use the base runtime.

    Computed through ``venv_env`` (ids are shortened for MAX_PATH; never hand-assemble the
    path). Returns ``None`` when ``venv_env`` is unavailable (non-engine context) or the
    overlay has not been installed yet -- step 7 only points at the overlay; installing it
    (uv via ``depends.py``) is a separate phase, and an absent overlay degrades to base.
    """
    try:
        import venv_env  # engine sys.path only
    except ImportError:
        return None
    site = venv_env.env_paths(venv_env.env_dir(exe_dir, project_id, env_id)).site_packages
    return site if os.path.isdir(site) else None


def build_child_env(
    base_env: Dict[str, str],
    client_id: str,
    run_token: str,
    overlay: Optional[str],
    avoid_mocks: bool,
) -> Dict[str, str]:
    """Build a venv child's subprocess environment, mirroring the main-engine spawn.

    Copies the parent env, sets the account context (``ROCKETRIDE_CLIENT_ID``), the per-run
    bridge token, and (when present) the overlay path; strips ``ROCKETRIDE_MOCK`` under
    ``avoidMocks`` exactly like the main engine so the child loads real libraries.
    """
    env = dict(base_env)
    env['ROCKETRIDE_CLIENT_ID'] = client_id
    env[VENV_TOKEN_ENV] = run_token
    if overlay:
        env[VENV_SITE_ENV] = overlay
    if avoid_mocks:
        env.pop('ROCKETRIDE_MOCK', None)
    return env


def inject_venv_urls(main_components: List[Dict[str, Any]], port_by_env: Dict[str, int]) -> None:
    """Fill each main-side round-trip ``venv`` node's live loopback ``urlProcess`` (in place).

    The partitioner leaves the bridge config as ``{channelId, lane, sourceEnv, targetEnv}``
    (plus ``returnChannelId``/``returnLane`` when the venv returns data); at spawn the
    orchestrator adds the child URL. The node dials the boundary's child (the ``main -> env``
    forward channel's target) on one socket that carries both directions: ``?channel=`` is the
    forward channel; ``&return=`` (when present) is the return channel the route binds the
    child egress to. The Bearer token rides the env, not this config.
    """
    for component in main_components:
        if component.get('provider') != 'venv':
            continue
        config = component.get('config') or {}
        channel_id = config.get('channelId')
        if not channel_id:
            continue
        source_env, target_env = config.get('sourceEnv'), config.get('targetEnv')
        child_env = target_env if target_env != 'main' else source_env
        port = port_by_env.get(child_env)
        if port is None:
            raise RuntimeError(f'no spawned venv child for env "{child_env}" (channel "{channel_id}")')
        url = f'ws://127.0.0.1:{port}/venv/pipe?channel={channel_id}'
        return_channel_id = config.get('returnChannelId')
        if return_channel_id:
            url += f'&return={return_channel_id}'
        config['urlProcess'] = url
        component['config'] = config


async def probe_ready(
    host: str,
    port: int,
    process: 'asyncio.subprocess.Process',
    attempts: int = 120,
    interval: float = 0.25,
) -> None:
    """Wait until the child's WebServer accepts TCP connections (default ~30s).

    A transport-level probe, not a ``/venv/pipe`` WS connect: that route rejects
    unauthenticated peers before accepting, so a bare connect cannot confirm readiness. A
    completed TCP handshake means the resident source's server is bound. The window must
    cover the whole child startup (engine init -> task setup -> resident source binds its
    WebServer), so it is generous. Bails immediately if the child has already exited.
    """
    last: Optional[BaseException] = None
    for _ in range(attempts):
        if process.returncode is not None:
            raise RuntimeError(f'venv child exited during startup with code {process.returncode}')
        try:
            _reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return
        except OSError as e:
            last = e
            await asyncio.sleep(interval)
    raise RuntimeError(f'venv child on port {port} did not become ready: {last}')


async def kill_process(process: 'asyncio.subprocess.Process', timeout: float) -> None:
    """Two-phase terminate -> (timeout) kill of a subprocess, then reap via ``wait()``.

    Mirrors the main engine's ``stop_task`` shutdown. ``wait()`` reaps the child so no
    zombie is left. Safe to call on an already-exited process.
    """
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
        return
    except asyncio.TimeoutError:
        pass
    try:
        process.kill()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
