"""Local venv-child spawn helpers (virtual-environments feature, §7 step 7).

A venv child is a per-run sibling ``engine`` subprocess that runs one isolated pipeline
group. It is kept alive by a resident ``venv_source_stub`` source hosting a loopback
``/venv/pipe`` WebServer; the main engine's ``venv`` client nodes dial it. This module holds
the pieces that are decidable without a ``Task``: env construction, url injection, the
two-phase kill, the pure routing table for a child's events, the readiness contract (the
status line a child emits once its route is mounted, plus the wait that consumes it), and
:class:`ProcessGuard`, the OS-level binding that bounds the run's whole process tree. The
``Task``-bound orchestration (assign ports, write task files, spawn, teardown, and acting on
that routing table) lives in ``task_engine.py`` and reuses these.

Reliability has two layers, and they cover different things. **Cooperative:** children are
spawned with ``--autoterm`` (a C++ stdin monitor exits the child if the server process dies)
and torn down explicitly by the owning ``Task`` (they are resident and never self-stop); the
child processes are reaped via ``wait()`` so no zombies are left. That reaches every *engine*
— measured: killing the server leaves zero of them behind. **What it does not reach is
grandchildren**: a ``subprocess.Popen``'d ``ffmpeg``, an audio loader, ``uv``, a model server.
Those have neither the stdin monitor nor a pipe from the server, which is what
:class:`ProcessGuard` is for.
"""

import asyncio
import ctypes
import os
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, FrozenSet, List, NamedTuple, Optional

# Per-run shared bridge secret. Delivered to the main engine and every child via an
# inherited env var (never argv, never the on-disk task file); the child's /venv/pipe
# route verifies it and the main-side venv clients present it. (§4.5)
VENV_TOKEN_ENV = 'ROCKETRIDE_VENV_TOKEN'
# Which environment the child installs into and imports from. The child resolves its own
# overlay path from this; it is never told the path. Mirrors venv_env.VENV_ENV_ID_ENV, which
# this module cannot import (engine sys.path only) -- keep in sync.
VENV_ENV_ID_ENV = 'ROCKETRIDE_VENV_ENV_ID'
# The raw document fact "this run has an isolated group", not a resolved scoping decision --
# scoping_enabled still decides, so a stale value cannot switch scoping on under =0. Mirrors
# venv_env.VENV_ISOLATED_ENV -- keep in sync.
VENV_ISOLATED_ENV = 'ROCKETRIDE_VENV_ISOLATED'

# ---------------------------------------------------------------------------
# child event routing (pure; the Task acts on the result)
# ---------------------------------------------------------------------------

# The child's own readiness announcement, emitted by the resident venv source strictly AFTER
# ``server.use('venv')`` mounts ``/venv/pipe`` (nodes/venv/source/IEndpoint.py). That ordering --
# mount, then report -- is the proof the route exists; a TCP handshake against the shared
# subprocess web server cannot give it, because that server binds at bootstrap and answers long
# before the route is added. This literal is therefore a contract between two trees: keep it in
# sync with the ``monitorStatus(...)`` call at the emitting site, which carries a pointer comment
# back here. A reworded line costs latency (readiness degrades to the old TCP-only proof), not
# correctness.
VENV_READY_STATUS = 'Venv child ready - listening for bridged lane data'

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
SE_READY = 'ready'  # the child announced /venv/pipe is mounted; resolves the readiness wait

# Outcomes of await_child_ready. Named rather than bare bools because the difference matters to the
# log: CONFIRMED means the mount was proved, DEGRADED means we fell back to the pre-8.5 TCP-only
# evidence and are proceeding anyway.
READY_CONFIRMED = 'ready'
READY_DEGRADED = 'degraded'


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


def is_ready_line(event: Dict[str, Any]) -> bool:
    """Whether this event is the child's readiness announcement (:data:`VENV_READY_STATUS`).

    Compared after ``strip()`` so trailing whitespace from the monitor channel cannot make a
    correct child look silent; anything else about the text must match exactly, because the whole
    value of the signal is that it is emitted at one specific point in the child's startup.
    """
    body = event.get('body')
    if not isinstance(body, dict):
        return False
    message = body.get('message')
    return isinstance(message, str) and message.strip() == VENV_READY_STATUS


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
        if name == 'apaevt_status_message' and is_ready_line(event):
            # Readiness rides the existing route instead of a parallel path: the line still feeds
            # the tail and the pre-main-engine status window, and additionally resolves the spawn's
            # wait. Keyed on the body, not the event name, which is why it cannot live in the table.
            return route._replace(side_effects=route.side_effects | {SE_READY})
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
    # Set when the child announces VENV_READY_STATUS. An Event because it must be STICKY: the pump
    # attaches before the readiness wait starts, so a fast child can announce into the void -- an
    # already-set Event returns immediately, whereas a one-shot callback or a future resolved with
    # nobody listening would hang the spawn until the ceiling.
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    # monotonic() of the last event received from this child, whatever its family. The readiness
    # wait treats its budget as a ceiling on SILENCE rather than on total time, so a child that is
    # visibly compiling and installing is waited for instead of killed at a fixed deadline.
    last_event_at: float = field(default_factory=time.monotonic)


def build_child_env(
    base_env: Dict[str, str],
    client_id: str,
    run_token: str,
    env_id: str,
    avoid_mocks: bool,
) -> Dict[str, str]:
    """Build a venv child's subprocess environment, mirroring the main-engine spawn.

    Copies the parent env, sets the account context (``ROCKETRIDE_CLIENT_ID``), the per-run
    bridge token and the child's environment id; strips ``ROCKETRIDE_MOCK`` under
    ``avoidMocks`` exactly like the main engine so the child loads real libraries.

    The env id is **assigned, never conditionally set**: a child must carry exactly one, so an
    inherited value has to lose. Main's spawn is the mirror image -- it pops the variable,
    because main must carry none.
    """
    env = dict(base_env)
    env['ROCKETRIDE_CLIENT_ID'] = client_id
    env[VENV_TOKEN_ENV] = run_token
    env[VENV_ENV_ID_ENV] = env_id
    # Unconditional, and not a parameter: a venv child IS an isolated group -- one child per group
    # and never otherwise -- so threading the computed value in would add an argument whose only
    # possible value is True. Main's stamp is conditional because main's document may have none.
    env[VENV_ISOLATED_ENV] = '1'
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


async def await_child_ready(
    child: VenvChild,
    host: str,
    port: int,
    process: 'asyncio.subprocess.Process',
    silence_ceiling: float = 30.0,
    interval: float = 0.25,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Wait until the child proves ``/venv/pipe`` is mounted; return how well it was proved.

    Two phases under **one** budget:

    1. **TCP accept** against the shared subprocess WebServer. Since #912 this proves only that
       the child is alive and its bootstrap server is listening -- the resident venv source adds
       ``/venv/pipe`` later -- so it is a liveness pre-check, not the proof.
    2. **The child's own announcement** (:data:`VENV_READY_STATUS`), delivered by the stdio pump
       into ``child.ready``. The emitting site reports it strictly after ``server.use('venv')``,
       so the ordering inside that one function is what makes it proof of mount.

    An already-set ``child.ready`` short-circuits phase 1 outright: the line cannot be emitted
    before the listener exists, so re-proving it by TCP could only add a failure mode.

    **The budget is a ceiling on silence, not on total time.** Every event from the child (any
    family) refreshes ``child.last_event_at``, so a child that is compiling and installing
    dependencies -- talking all the while, via ``depends``' 5-second install heartbeat -- is
    waited for, while a wedged one still fails inside ``silence_ceiling``. That is the whole
    change: the old fixed ~30 s deadline killed slow-but-healthy children.

    Args:
        child: The child being waited for; supplies the sticky ready Event and the liveness clock.
        host: Loopback host to probe.
        port: The child's ``--data_port``.
        process: The child process, so a death is detected instead of waited out.
        silence_ceiling: Seconds of total quiet tolerated in either phase (default matches the
            old 120 x 0.25 s budget). Injectable so tests do not sleep out real ceilings.
        interval: Poll period for both phases.
        clock: Source of monotonic time, injectable for the same reason the ceiling is — and for
            one more. A test that drives the ceiling with real ``asyncio.sleep`` is racing the
            scheduler: on a loaded machine a pause longer than the ceiling reads as silence and
            a healthy child is reported degraded. Feeding a clock the test advances itself makes
            "did the child speak" independent of whether the box was busy.

    Returns:
        ``READY_CONFIRMED`` when the child announced itself; ``READY_DEGRADED`` when the socket
        accepted but the announcement never came and the child then fell silent -- the caller
        proceeds, because that is exactly the pre-8.5 behaviour and a reworded status line must
        cost latency, not the run.

    Raises:
        RuntimeError: the child exited during startup, or nothing ever accepted on ``port``
            while the child stayed silent.
    """
    started = clock()

    def quiet_for() -> float:
        """Seconds since the last sign of life (the wait's own start counts as one)."""
        return clock() - max(child.last_event_at, started)

    def _bail_if_dead() -> None:
        if process.returncode is not None:
            raise RuntimeError(f'venv child exited during startup with code {process.returncode}')

    # Phase 1 -- socket accepts, or the announcement beats us to it.
    last: Optional[BaseException] = None
    while not child.ready.is_set():
        _bail_if_dead()
        try:
            _reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            break
        except OSError as e:
            last = e
        if quiet_for() > silence_ceiling:
            raise RuntimeError(f'venv child on port {port} did not become ready: {last}')
        await asyncio.sleep(interval)

    # Phase 2 -- the mount proof itself.
    while not child.ready.is_set():
        _bail_if_dead()
        if quiet_for() > silence_ceiling:
            return READY_DEGRADED
        try:
            await asyncio.wait_for(asyncio.shield(child.ready.wait()), timeout=interval)
        except asyncio.TimeoutError:
            pass

    return READY_CONFIRMED


# ---------------------------------------------------------------------------
# OS-level process-tree binding (8.5B)
# ---------------------------------------------------------------------------

_IS_WINDOWS = os.name == 'nt'

# Windows Job Object constants. Spelled out rather than imported because ctypes has no header.
_JobObjectExtendedLimitInformation = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ('ReadOperationCount', ctypes.c_ulonglong),
        ('WriteOperationCount', ctypes.c_ulonglong),
        ('OtherOperationCount', ctypes.c_ulonglong),
        ('ReadTransferCount', ctypes.c_ulonglong),
        ('WriteTransferCount', ctypes.c_ulonglong),
        ('OtherTransferCount', ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('PerProcessUserTimeLimit', ctypes.c_int64),
        ('PerJobUserTimeLimit', ctypes.c_int64),
        ('LimitFlags', ctypes.c_uint32),
        ('MinimumWorkingSetSize', ctypes.c_size_t),
        ('MaximumWorkingSetSize', ctypes.c_size_t),
        ('ActiveProcessLimit', ctypes.c_uint32),
        ('Affinity', ctypes.c_size_t),  # ULONG_PTR
        ('PriorityClass', ctypes.c_uint32),
        ('SchedulingClass', ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('BasicLimitInformation', _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ('IoInfo', _IO_COUNTERS),
        ('ProcessMemoryLimit', ctypes.c_size_t),
        ('JobMemoryLimit', ctypes.c_size_t),
        ('PeakProcessMemoryUsed', ctypes.c_size_t),
        ('PeakJobMemoryUsed', ctypes.c_size_t),
    ]


class ProcessGuard:
    """Bind a run's engine processes to the OS so nothing of theirs outlives the run.

    **The two platforms do not deliver the same guarantee, and the difference is the point.**

    - *Windows* is kernel-enforced and unconditional. The server holds a Job Object handle with
      ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``; however the server dies — including ``kill -9`` —
      the OS closes the handle and the kernel takes the **whole tree**, grandchildren included.
      No cooperation required from anyone.
    - *POSIX* has **no equivalent for parent death**. ``killpg`` needs someone alive to call it,
      and a SIGKILLed server calls nothing, so the group survives. What process groups buy is the
      *graceful* path: teardown reaches grandchildren that ``terminate`` → ``kill`` on the direct
      child never touches. Abrupt server death stays covered only by ``--autoterm``, which engines
      have and ``ffmpeg``/``uv`` do not.

    Treat "the POSIX branch is the Windows branch with different calls" as the mistake to avoid:
    it shows up twice in the code below — there is nothing to ``close()``, and the state is a
    *list* of per-child groups rather than one container.

    Every OS call degrades to a documented no-op on failure. This object sits on the spawn path
    of every scoped run; it must never be able to break one.
    """

    def __init__(self):
        """Use :meth:`create` -- the constructor deliberately acquires nothing."""
        self._job = None  # Windows: HANDLE to the job object
        self._pgids: List[int] = []  # POSIX: one process-group id per assigned process
        self._closed = False

    @classmethod
    def create(cls) -> 'ProcessGuard':
        """Build a guard, acquiring the Job Object on Windows (a holder only on POSIX)."""
        guard = cls()
        if not _IS_WINDOWS:
            return guard
        try:
            kernel32 = ctypes.windll.kernel32
            # Anonymous on purpose: a NAME can collide -- CreateJobObjectW returns the EXISTING
            # job for a name already in use, so two concurrent runs (or a restart racing a dying
            # job) would silently share one job, and close() on the first would kill the second's
            # children. Nothing needs to open this job from outside; membership is asserted in the
            # unit tests, where the handle is in hand.
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return guard
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = kernel32.SetInformationJobObject(
                job, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
            )
            if not ok:
                kernel32.CloseHandle(job)
                return guard
            guard._job = job
        except Exception:
            guard._job = None
        return guard

    def spawn_kwargs(self) -> Dict[str, Any]:
        """Extra ``create_subprocess_exec`` kwargs every guarded process MUST be spawned with.

        POSIX: ``start_new_session=True``, so the process leads its own group and ``killpg``
        reaches its grandchildren *without* reaching the server. This is not optional decoration —
        :meth:`assign` is only valid for a process spawned this way (see its refusal below).

        Deliberately **no** ``preexec_fn`` and therefore no ``PR_SET_PDEATHSIG``: forking with
        ``preexec_fn`` from a multi-threaded server is a documented deadlock hazard, and pdeathsig
        keys on the *forking thread* rather than the process, so a future ``asyncio.to_thread``
        spawn would kill live children when that thread ended. Parent death is already covered by
        ``--autoterm`` for engines; the residual gap for grandchildren is recorded in §4.10 rather
        than papered over here.

        One consequence worth knowing on POSIX: a new session detaches the child from the
        controlling terminal, so an interactive Ctrl-C no longer reaches it. Teardown and
        ``--autoterm`` cover it; a developer used to Ctrl-C killing everything will notice.
        """
        return {} if _IS_WINDOWS else {'start_new_session': True}

    def assign(self, process) -> bool:
        """Bind one already-spawned process to the guard. Returns whether it took.

        The process **must** have been spawned with :meth:`spawn_kwargs`. On POSIX that is a
        correctness requirement, not style: without ``start_new_session`` the child inherits the
        *server's* process group, so this would record the server's own pgid and
        :meth:`terminate_all` would take the server down with the run. The check below refuses
        that outright rather than trusting the caller — a defensive check that should never fire
        is the right shape when the failure it prevents is "the server vanished mid-run".

        Windows has a spawn-to-assign race: a grandchild created between ``CreateProcess`` and
        ``AssignProcessToJobObject`` escapes the job. ``CREATE_SUSPENDED`` + assign + resume
        cannot be expressed through ``asyncio.create_subprocess_exec`` (no main-thread handle; it
        would need ``NtResumeProcess``), so the window is accepted — a child's grandchildren
        appear well after engine init.
        """
        pid = getattr(process, 'pid', None)
        if pid is None:
            return False

        if _IS_WINDOWS:
            if not self._job:
                return False
            try:
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
                if not handle:
                    return False
                try:
                    return bool(kernel32.AssignProcessToJobObject(self._job, handle))
                finally:
                    kernel32.CloseHandle(handle)
            except Exception:
                return False

        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        if pgid == os.getpgrp():
            # The process was NOT spawned with spawn_kwargs(); recording this would arm
            # terminate_all() to kill the server itself.
            return False
        # Per child, not one shared value: start_new_session gives each its OWN group, so a
        # single stored pgid would tear down one child and leave the rest.
        if pgid not in self._pgids:
            self._pgids.append(pgid)
        return True

    def terminate_all(self, grace: float = 0.5) -> int:
        """Kill everything bound to the guard. Returns how many POSIX groups were still alive.

        Windows: one ``TerminateJobObject`` call takes the whole job. The return value is 0 there
        — counting survivors would need a variable-length ``QueryInformationJobObject`` buffer,
        and the caller already knows which children the cooperative phase failed to reap, which is
        the number worth logging.

        POSIX: ``SIGTERM`` each recorded group, wait ``grace``, then ``SIGKILL``, tolerating
        ``ProcessLookupError`` for groups the cooperative phase already emptied.
        """
        if _IS_WINDOWS:
            if self._job:
                try:
                    ctypes.windll.kernel32.TerminateJobObject(self._job, 1)
                except Exception:
                    pass
            return 0

        alive = 0
        for pgid in self._pgids:
            try:
                os.killpg(pgid, signal.SIGTERM)
                alive += 1
            except (ProcessLookupError, PermissionError, OSError):
                continue
        if alive:
            time.sleep(grace)
            for pgid in self._pgids:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    continue
        return alive

    def close(self) -> None:
        """Release the guard. On Windows this IS the orphan-safety property.

        Closing the last handle to a ``KILL_ON_JOB_CLOSE`` job makes the kernel kill everything
        still in it — which is exactly why the server holding this handle protects the tree
        however the server itself dies. POSIX has nothing to close, and that asymmetry is the
        whole story in one line: there, :meth:`terminate_all` is the only thing that ever reaps a
        group, so a teardown path that skips it leaks silently instead of being caught by handle
        closure.
        """
        if self._closed:
            return
        self._closed = True
        if _IS_WINDOWS and self._job:
            try:
                ctypes.windll.kernel32.CloseHandle(self._job)
            except Exception:
                pass
            self._job = None
        self._pgids = []


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
