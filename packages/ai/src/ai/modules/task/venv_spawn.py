"""Local venv-child spawn helpers (virtual-environments feature, §7 step 7).

A venv child is a per-run sibling ``engine`` subprocess that runs one isolated pipeline
group. It is kept alive by a resident ``venv_source_stub`` source hosting a loopback
``/venv/pipe`` WebServer; the main engine's ``venv`` client nodes dial it. This module holds
the pieces that are decidable without a ``Task``: env construction, url injection, the
two-phase kill, and the pure routing table for a child's events. The ``Task``-bound
orchestration (assign ports, write task files, spawn, readiness, teardown, and acting on
that routing table) lives in ``task_engine.py`` and reuses these.

Reliability is the main engine's, extended to N children: children are spawned with
``--autoterm`` (a C++ stdin monitor exits the child if the server process dies) and torn
down explicitly by the owning ``Task`` (they are resident and never self-stop); the child
processes are reaped via ``wait()`` so no zombies are left.
"""

import asyncio
import os
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, FrozenSet, List, NamedTuple, Optional

# Per-run shared bridge secret. Delivered to the main engine and every child via an
# inherited env var (never argv, never the on-disk task file); the child's /venv/pipe
# route verifies it and the main-side venv clients present it. (§4.5)
VENV_TOKEN_ENV = 'ROCKETRIDE_VENV_TOKEN'
# Points a child at its per-environment ``sys.path`` overlay (§4.11); consumed by the
# engine bootstrap in ``depends.py``. Unset -> the child runs on the base runtime.
VENV_SITE_ENV = 'ROCKETRIDE_VENV_SITE'

# ---------------------------------------------------------------------------
# child event routing (pure; the Task acts on the result)
# ---------------------------------------------------------------------------

# A child's traces travel under their own name rather than being derived into
# ``apaevt_flow``. Two reasons, and the second is the decisive one: a child's pipe indices
# are its own, so merging them into main's ``pipeflow`` corrupts its accounting, AND
# emitting them as ``apaevt_flow`` corrupts the *client's* -- the TS log codec keys its
# open-flow stacks by ``body.id``, so two processes' enter/leave pairs interleave under one
# key. Declining to merge fixes only the server; declining to derive fixes both. The name is
# new rather than reused because main's raw ``apaevt_trace`` never reaches the wire (it is
# consumed into ``apaevt_flow``), so reusing it would create a channel that carries child
# traces and silently never main's.
VENV_TRACE_EVENT = 'apaevt_venv_trace'

# Delivery channels, as plain strings: ``EVENT_TYPE`` lives in the ``rocketride`` SDK
# package and this module deliberately imports nothing from it. ``task_engine`` maps these
# onto the real flags at the call site.
CH_SSE = 'sse'
CH_OUTPUT = 'output'
CH_FLOW = 'flow'
CH_DETAIL = 'detail'
CH_NONE = None  # log only -- not forwarded to any subscriber

# Side effects a route may additionally carry. More than one can apply to a single event,
# which is why this is a set: ``output`` both forwards and appends to the run's trace, and a
# status message forwards, feeds the tail and (from 8.5) resolves readiness.
SE_TAIL = 'tail'  # append rendered text to the child's tail ring
SE_STATUS_TRACE = 'status_trace'  # append to the run's _status_trace
SE_ERROR = 'error'  # append to _status.errors, env-prefixed
SE_WARNING = 'warning'  # append to _status.warnings, env-prefixed
SE_METRICS = 'metrics'  # merge into the per-source >MET slot
SE_STATUS_WINDOW = 'status_window'  # may set the run status, but only pre-main-engine
SE_EXIT = 'exit'  # record the child's exit; never terminates the run


class ChildRoute(NamedTuple):
    """Where one child event goes, and what else it touches."""

    channel: Optional[str]
    side_effects: FrozenSet[str]
    # Set when the event is forwarded under a different name than it arrived with.
    rename_to: Optional[str] = None


_STATUS_ROUTES: Dict[str, ChildRoute] = {
    # >SVC. Handled in Task.on_event *before* the apaevt_status_ prefix branch, where it
    # sets serviceUp and lifts the billing gate -- so "never _update_status" does not cover
    # it and it has to be named. A child must never move the run's billing gate.
    'apaevt_status_state': ChildRoute(CH_DETAIL, frozenset()),
    # >OBJ. Sets currentObject/currentSize; letting a child through makes the run's
    # displayed current object flicker between two processes.
    'apaevt_status_object': ChildRoute(CH_DETAIL, frozenset()),
    # >ERR / >WRN. A child's error belongs to the run, so these are the exceptions that do
    # reach _status -- and they feed the tail, since >ERR* is NOT an ``output`` event and a
    # tail built only from ``output`` would lose the startup diagnostic entirely.
    'apaevt_status_error': ChildRoute(CH_DETAIL, frozenset({SE_ERROR, SE_TAIL})),
    'apaevt_status_warning': ChildRoute(CH_DETAIL, frozenset({SE_WARNING, SE_TAIL})),
    # >MET. Routed explicitly to the per-source metric slot rather than through
    # _update_status, which would let a child's snapshot overwrite main's wholesale.
    'apaevt_status_metrics': ChildRoute(CH_DETAIL, frozenset({SE_METRICS})),
    # >JOB, by far the loudest (265 in one measured child-run). Sets the run status only
    # while no main engine exists yet -- that window is child startup, which is what turns a
    # silent 30-second death into visible install progress.
    'apaevt_status_message': ChildRoute(CH_DETAIL, frozenset({SE_STATUS_WINDOW, SE_TAIL})),
}


def classify_child_event(event: Dict[str, Any]) -> ChildRoute:
    """Decide where a venv child's event goes. Pure -- the caller performs the effects.

    The whole policy lives here so it can be tested without a ``Task``: what a child may
    influence in the run's shared state is a security-of-accounting question more than a
    plumbing one, and the two events that must NOT flow through (``apaevt_status_state``,
    ``apaevt_status_metrics`` via ``_update_status``) are invisible in any happy-path test.

    Args:
        event: A parsed DAP event from the child's stdio pump.

    Returns:
        The route: delivery channel (``None`` = log only), the set of side effects, and an
        optional replacement event name.
    """
    name = event.get('event', '')

    if name == 'apaevt_sse':
        return ChildRoute(CH_SSE, frozenset())

    if name == 'output':
        return ChildRoute(CH_OUTPUT, frozenset({SE_STATUS_TRACE, SE_TAIL}))

    if name == 'apaevt_trace':
        # Gated on the run's trace level by the caller: a child emits >DBG whether or not
        # the run asked for tracing, so forwarding ungated would deliver volume that =0
        # does not.
        return ChildRoute(CH_FLOW, frozenset(), VENV_TRACE_EVENT)

    if name == 'apaevt_exit':
        return ChildRoute(CH_NONE, frozenset({SE_EXIT}))

    route = _STATUS_ROUTES.get(name)
    if route is not None:
        return route

    if name.startswith('apaevt_status_'):
        return ChildRoute(CH_DETAIL, frozenset())

    # Default row. Main's on_event sends unmatched events to the DEBUGGER channel, but a
    # child is not the debug target -- an unknown family from a child belongs in the log.
    return ChildRoute(CH_NONE, frozenset({SE_TAIL}))


@dataclass
class VenvChild:
    """A spawned venv-child subprocess and the run resources it owns."""

    env_id: str
    name: str
    process: 'asyncio.subprocess.Process'
    port: int
    tmpfile: str
    # DAP stdio pump over the child's stdout/stderr (a Task.VenvChildStdio). Replaces the
    # raw readline drains: the transport already parses the engine's '>' protocol, so the
    # tail, the mirror and the event fan-in all feed from parsed events instead of lines.
    pump: Optional[Any] = None
    # Set before a deliberate disconnect. The transport fires on_disconnected from
    # disconnect() as well as from a real exit, so without this every clean run would end by
    # logging N spurious child deaths -- the noise that makes a real one easy to miss.
    stopping: bool = False
    # True once the child's stdio closed or it reported apaevt_exit.
    exited: bool = False
    # Per-live-child log path. Keyed by port as well as env id: env names collide across
    # concurrent runs ('v1' is every test's favourite), and truncate-at-spawn on a shared
    # name would clobber another run's live log. The port is unique per live child and
    # recycled afterwards, so this bounds the file count instead of growing it forever.
    log_path: Optional[str] = None
    # Ring of the child's most recent rendered output, so a startup failure can report the
    # child's own error (e.g. a dependency conflict) instead of only its exit code. Fed from
    # more than 'output' events: '>ERR*' arrives as apaevt_status_error, so a tail built
    # only from output would silently lose the startup diagnostic.
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

    The partitioner leaves the bridge config as ``{channelId, sourceEnv, targetEnv, lanes}``
    (plus ``returnChannelId``/``returnLanes`` when the venv returns data); at spawn the
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
    unauthenticated peers before accepting, and an unmatched path is refused the same way, so
    a handshake cannot tell "route mounted" from "route missing".

    **What it proves, since #912:** the shared subprocess WebServer (bootstrapped by
    ``ai/node.py`` from ``--data_port``, before the engine runs) is listening and the child is
    alive. It no longer proves ``/venv/pipe`` is mounted -- the resident source adds that route
    later, from ``scanObjects``. In practice the child wins that race comfortably: it only has
    to finish its own engine init, while the bridge's first dial waits on the rest of the spawn
    loop, the main task file, and a whole main-engine startup that begins afterwards. If it ever
    loses, the symptom is a refused first dial rather than a hang -- fix it then, with the
    evidence, rather than guessing at a readiness protocol now.

    Bails immediately if the child has already exited.
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
