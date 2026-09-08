"""Unit tests for ``venv_spawn`` helpers that are pure (no live child).

Covers ``inject_venv_urls`` -- turning the partitioner's per-child ``venv`` node into a live
loopback URL, including the ``&return=`` binding when the venv returns data (step 8.1, Arch-1:
one bridge node per child carries all its lanes over one socket) -- ``classify_child_event``,
the routing table the 8.4 fan-in acts on, ``await_child_ready`` (the 8.5A readiness wait) and
``ProcessGuard`` (the 8.5B OS-level process-tree binding).

Two kinds of case here deliberately avoid mocks, because a mock would assert away the only thing
worth testing. The readiness cases use a real loopback listener: the whole point of the two-phase
wait is how it behaves against an accepting vs. a refusing port. The guard cases bind a real
throwaway subprocess and check it actually dies -- "orphan safety" mocked is not orphan safety.
Both pass tiny timeouts instead of sleeping out the real ~30 s budget.
"""

import asyncio
import os
import socket
import subprocess
import sys
import time

import pytest

from ai.modules.task.venv_spawn import (
    CH_DETAIL,
    CH_FLOW,
    CH_NONE,
    CH_OUTPUT,
    CH_SSE,
    READY_CONFIRMED,
    READY_DEGRADED,
    SE_ERROR,
    SE_EXIT,
    SE_METRICS,
    SE_READY,
    SE_STATUS_TRACE,
    SE_STATUS_WINDOW,
    SE_TAIL,
    SE_WARNING,
    VENV_READY_STATUS,
    VENV_ENV_ID_ENV,
    VENV_ISOLATED_ENV,
    VENV_TOKEN_ENV,
    VENV_TRACE_EVENT,
    ProcessGuard,
    VenvChild,
    await_child_ready,
    VENV_FORCED_SHA_ENV,
    build_child_env,
    classify_child_event,
    forced_digest,
    inject_venv_urls,
    normalize_forced_text,
    write_forced_file,
)

_POSIX_ONLY = pytest.mark.skipif(os.name == 'nt', reason='process groups are POSIX-only')
_WINDOWS_ONLY = pytest.mark.skipif(os.name != 'nt', reason='Job Objects are Windows-only')


def _venv_node(node_id, config):
    return {'id': node_id, 'provider': 'venv', 'config': config}


def test_forward_only_channel_gets_a_bare_channel_url():
    node = _venv_node(
        'egress',
        {'channelId': 'main->v', 'sourceEnv': 'main', 'targetEnv': 'v', 'lanes': ['text', 'image']},
    )

    inject_venv_urls([node], {'v': 5601})

    assert node['config']['urlProcess'] == 'ws://127.0.0.1:5601/venv/pipe?channel=main->v'


def test_round_trip_channel_appends_the_return_binding():
    node = _venv_node(
        'egress',
        {
            'channelId': 'main->v',
            'sourceEnv': 'main',
            'targetEnv': 'v',
            'lanes': ['text'],
            'returnChannelId': 'v->main',
            'returnLanes': ['text', 'json'],
        },
    )

    inject_venv_urls([node], {'v': 5602})

    assert node['config']['urlProcess'] == 'ws://127.0.0.1:5602/venv/pipe?channel=main->v&return=v->main'


def test_non_venv_nodes_are_left_untouched():
    plain = {'id': 'parse', 'provider': 'default', 'config': {}}

    inject_venv_urls([plain], {'v': 5604})

    assert 'urlProcess' not in plain['config']


def test_missing_child_port_raises():
    node = _venv_node('egress', {'channelId': 'main->v', 'sourceEnv': 'main', 'targetEnv': 'v', 'lanes': ['text']})

    with pytest.raises(RuntimeError, match='no spawned venv child'):
        inject_venv_urls([node], {})


# ---------------------------------------------------------------------------
# classify_child_event -- the 8.4 routing table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'event_name,channel,effects',
    [
        ('apaevt_sse', CH_SSE, set()),
        ('output', CH_OUTPUT, {SE_STATUS_TRACE, SE_TAIL}),
        ('apaevt_status_error', CH_DETAIL, {SE_ERROR, SE_TAIL}),
        ('apaevt_status_warning', CH_DETAIL, {SE_WARNING, SE_TAIL}),
        ('apaevt_status_metrics', CH_DETAIL, {SE_METRICS}),
        ('apaevt_status_message', CH_DETAIL, {SE_STATUS_WINDOW, SE_TAIL}),
        ('apaevt_exit', CH_NONE, {SE_EXIT}),
    ],
)
def test_routes_each_event_family(event_name, channel, effects):
    route = classify_child_event({'event': event_name})

    assert route.channel == channel
    assert set(route.side_effects) == effects


def test_child_service_state_never_reaches_the_billing_gate():
    """>SVC is the trap: Task.on_event handles it BEFORE the apaevt_status_ prefix branch,
    where it sets serviceUp and lifts _billing_gated. "Never _update_status" does not cover
    it, so a child could otherwise start billing a run that has not started.
    """
    route = classify_child_event({'event': 'apaevt_status_state'})

    assert route.channel == CH_DETAIL
    assert not set(route.side_effects)


def test_child_current_object_does_not_reach_the_run_status():
    """>OBJ sets currentObject/currentSize; letting a child through makes the displayed
    object flicker between two processes.
    """
    route = classify_child_event({'event': 'apaevt_status_object'})

    assert route.channel == CH_DETAIL
    assert not set(route.side_effects)


def test_child_traces_are_renamed_rather_than_derived_into_flow():
    """Emitting them as apaevt_flow would corrupt the CLIENT's state machine: the TS log
    codec keys open-flow stacks by body.id, and a child's pipe indices collide with main's.
    """
    route = classify_child_event({'event': 'apaevt_trace'})

    assert route.channel == CH_FLOW
    assert route.rename_to == VENV_TRACE_EVENT


def test_unknown_status_family_is_detail_only():
    route = classify_child_event({'event': 'apaevt_status_counts'})

    assert route.channel == CH_DETAIL
    assert not set(route.side_effects)


def test_unknown_event_is_logged_not_forwarded():
    """Main's on_event sends unmatched events to the DEBUGGER channel, but a child is not the
    debug target -- an unknown family from a child belongs in the log.
    """
    route = classify_child_event({'event': 'something_the_engine_grew_later'})

    assert route.channel is CH_NONE
    assert set(route.side_effects) == {SE_TAIL}


def test_event_without_a_name_is_not_forwarded():
    assert classify_child_event({}).channel is CH_NONE


def test_the_ready_line_adds_readiness_to_the_status_message_route():
    """Readiness rides the existing >JOB route rather than a parallel path: the line still feeds
    the tail and the pre-main-engine status window, and additionally resolves the spawn's wait.
    """
    route = classify_child_event({'event': 'apaevt_status_message', 'body': {'message': VENV_READY_STATUS}})

    assert route.channel == CH_DETAIL
    assert set(route.side_effects) == {SE_STATUS_WINDOW, SE_TAIL, SE_READY}


def test_another_job_message_does_not_resolve_readiness():
    """The keying is on the body, not the family -- a child emits hundreds of >JOB lines and only
    one of them means "the route is mounted".
    """
    route = classify_child_event({'event': 'apaevt_status_message', 'body': {'message': 'Downloading torch (2.7GiB)'}})

    assert SE_READY not in route.side_effects


# ---------------------------------------------------------------------------
# await_child_ready -- the 8.5A readiness wait
# ---------------------------------------------------------------------------


class _FakeProcess:
    """The only attribute the wait reads: whether the child is still alive."""

    def __init__(self, returncode=None):
        self.returncode = returncode


def _child(process=None):
    return VenvChild(env_id='v1', name='v1', process=process or _FakeProcess(), port=0, tmpfile='t.json')


async def _listener():
    """A real accepting loopback socket; returns (server, port)."""
    server = await asyncio.start_server(lambda _r, w: w.close(), '127.0.0.1', 0)
    return server, server.sockets[0].getsockname()[1]


def _closed_port():
    """A port nothing is listening on (bound to learn the number, then released)."""
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


@pytest.mark.asyncio
async def test_the_ready_line_resolves_the_wait():
    server, port = await _listener()
    child = _child()

    async def announce():
        await asyncio.sleep(0.02)
        child.last_event_at = time.monotonic()
        child.ready.set()

    task = asyncio.create_task(announce())
    try:
        outcome = await await_child_ready(child, '127.0.0.1', port, child.process, silence_ceiling=2.0, interval=0.01)
        assert outcome == READY_CONFIRMED
    finally:
        await task
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_a_line_that_arrived_before_the_wait_began_still_resolves_it():
    """The sticky property, and the one case a callback-based implementation fails.

    The stdio pump attaches BEFORE the readiness wait starts, so a fast child announces into the
    void. An already-set Event returns immediately; a one-shot callback or a future resolved with
    nobody listening would hang the spawn until the ceiling. Deliberately run against a CLOSED
    port: the announcement cannot precede its own listener, so proving the mount by TCP as well
    could only add a failure mode.
    """
    child = _child()
    child.ready.set()

    outcome = await await_child_ready(
        child, '127.0.0.1', _closed_port(), child.process, silence_ceiling=0.05, interval=0.01
    )

    assert outcome == READY_CONFIRMED


@pytest.mark.asyncio
async def test_a_chatty_child_is_waited_past_the_silence_ceiling():
    """The budget is a ceiling on SILENCE, not on total time -- this is the whole increment.

    A child compiling and installing dependencies talks throughout (``depends`` re-emits its last
    status every 5 s even during a silent uv run), and under the old fixed deadline it died anyway.
    """
    server, port = await _listener()
    child = _child()
    ceiling = 0.4

    # Simulated time, advanced only by the chatter. Widening a real-clock ceiling was tried
    # (commit 83aa7cda took it from 0.1 s to 0.4 s) and did not hold: the test was racing the
    # scheduler, so a loaded machine could always manufacture a gap wider than whatever number
    # was chosen, and a healthy child came back degraded. With the clock in the test's hands a
    # stall delays the chatter and the waiter together and cannot invent silence.
    now = [0.0]

    def clock() -> float:
        return now[0]

    child.last_event_at = clock()

    async def chatter():
        for _ in range(15):
            await asyncio.sleep(0.005)  # let the waiter poll between ticks; real duration is irrelevant
            now[0] += ceiling / 8  # ...because only this advances the clock the waiter reads
            child.last_event_at = clock()
        child.ready.set()

    task = asyncio.create_task(chatter())
    started = clock()
    try:
        outcome = await await_child_ready(
            child, '127.0.0.1', port, child.process, silence_ceiling=ceiling, interval=0.01, clock=clock
        )
        assert outcome == READY_CONFIRMED
        assert clock() - started > ceiling, 'the old fixed deadline would have killed it here'
    finally:
        await task
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_a_silent_child_with_tcp_up_degrades_rather_than_failing():
    """A reworded status line on the node side must cost latency, not the run: the wait falls
    back to exactly the evidence the pre-8.5 probe accepted.
    """
    server, port = await _listener()
    child = _child()
    try:
        outcome = await await_child_ready(child, '127.0.0.1', port, child.process, silence_ceiling=0.05, interval=0.01)
        assert outcome == READY_DEGRADED
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_tcp_never_accepting_fails_by_name():
    """Today's failure, unchanged -- and distinct from the degraded case above, which is why the
    two must never share a name.
    """
    child = _child()

    with pytest.raises(RuntimeError, match='did not become ready'):
        await await_child_ready(child, '127.0.0.1', _closed_port(), child.process, silence_ceiling=0.05, interval=0.01)


@pytest.mark.asyncio
async def test_an_exited_child_bails_immediately():
    """A dead child is not waited out: the step-7 startup diagnostic quotes the child's own error,
    and it can only do that if the wait returns as soon as the process is gone.
    """
    child = _child(_FakeProcess(returncode=3))
    started = time.monotonic()

    with pytest.raises(RuntimeError, match='exited during startup with code 3'):
        await await_child_ready(child, '127.0.0.1', _closed_port(), child.process, silence_ceiling=5.0, interval=0.01)

    assert time.monotonic() - started < 1.0, 'bailed on the exit, not on the ceiling'


# ---------------------------------------------------------------------------
# ProcessGuard -- the 8.5B OS-level binding
# ---------------------------------------------------------------------------

_SLEEPER = 'import time; time.sleep(60)'
# A grandchild that outlives its parent's own exit -- the class --autoterm cannot reach and the
# only reason 8.5B exists (killing the direct child was already handled).
_SPAWNS_A_GRANDCHILD = (
    'import subprocess, sys, time; '
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
    'print(p.pid, flush=True); time.sleep(60)'
)


def _throwaway(guard, code=_SLEEPER, **kwargs):
    """Spawn a real process THROUGH the guard's own spawn_kwargs.

    Through them, not beside them: on POSIX the kwargs are what make ``assign`` valid at all, so a
    test that spawned a bare ``Popen`` would exercise a pairing the production wiring never uses.
    ``sys.executable`` rather than a hard-coded name, so this same test runs under the Windows
    engine here and a Linux engine elsewhere.
    """
    return subprocess.Popen([sys.executable, '-c', code], **guard.spawn_kwargs(), **kwargs)


def _dead_within(process, seconds=10.0):
    try:
        process.wait(timeout=seconds)
        return True
    except subprocess.TimeoutExpired:
        return False


def test_assign_then_terminate_all_kills_the_process():
    guard = ProcessGuard.create()
    process = _throwaway(guard)
    try:
        assert guard.assign(process)
        guard.terminate_all()
        assert _dead_within(process)
    finally:
        guard.close()
        if process.poll() is None:
            process.kill()


@_WINDOWS_ONLY
def test_closing_the_job_kills_what_is_left():
    """The orphan-safety property itself: the server holds this handle, so however the server
    dies the OS closes it and KILL_ON_JOB_CLOSE takes the tree. Asserted here rather than only
    live, because live it is indistinguishable from --autoterm doing the work.
    """
    guard = ProcessGuard.create()
    process = _throwaway(guard)
    try:
        assert guard.assign(process)
        guard.close()
        assert _dead_within(process)
    finally:
        if process.poll() is None:
            process.kill()


def test_a_degraded_guard_still_yields_working_kwargs_and_a_no_op_terminate():
    """A guard whose OS calls failed must never break the spawn path -- it is on the critical
    path of every scoped run. Built directly rather than via create(), which is exactly the state
    a failed CreateJobObjectW/OpenProcess leaves behind.
    """
    guard = ProcessGuard()

    assert guard.spawn_kwargs() == ({} if os.name == 'nt' else {'start_new_session': True})
    assert guard.terminate_all() == 0
    guard.close()  # must not raise
    guard.close()  # idempotent


@_POSIX_ONLY
def test_assign_refuses_a_process_in_the_servers_own_group():
    """The bug this prevents destroys the server, and nobody would attribute that to teardown.

    Without start_new_session a child inherits the SERVER's process group, so recording its pgid
    would arm terminate_all() to killpg the server itself along with the run.
    """
    guard = ProcessGuard.create()
    # Deliberately NOT through spawn_kwargs: this is the mistake being guarded against.
    process = subprocess.Popen([sys.executable, '-c', _SLEEPER])
    try:
        assert guard.assign(process) is False
        assert os.getpgrp() not in guard._pgids
    finally:
        process.kill()
        process.wait(timeout=10)
        guard.close()


@_POSIX_ONLY
def test_terminate_all_reaches_a_grandchild():
    """Killing only the direct child is the failure mode process groups exist to prevent -- and it
    passes any test that checks one process, which is why the grandchild is asserted explicitly.
    """
    guard = ProcessGuard.create()
    process = _throwaway(guard, _SPAWNS_A_GRANDCHILD, stdout=subprocess.PIPE, text=True)
    try:
        grandchild_pid = int(process.stdout.readline().strip())
        assert guard.assign(process)

        guard.terminate_all()

        assert _dead_within(process)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            pytest.fail(f'grandchild {grandchild_pid} survived terminate_all')
    finally:
        if process.poll() is None:
            process.kill()
        guard.close()


# ---------------------------------------------------------------------------
# the child's subprocess environment (step 8.7A)
# ---------------------------------------------------------------------------


def test_child_env_carries_the_env_id_and_the_token():
    env = build_child_env({'PATH': '/x'}, 'client-1', 'tok-1', 'v1', avoid_mocks=False)
    assert env[VENV_ENV_ID_ENV] == 'v1'
    assert env[VENV_TOKEN_ENV] == 'tok-1'
    assert env['ROCKETRIDE_CLIENT_ID'] == 'client-1'
    assert env['PATH'] == '/x'
    # Unconditional: a venv child IS an isolated group, so there is no false case to test.
    assert env[VENV_ISOLATED_ENV] == '1'


def test_child_env_overwrites_an_inherited_env_id():
    # The assignment IS the child's protection -- a clean base_env would prove only that the
    # key gets written, not that a stale inherited one loses.
    env = build_child_env({VENV_ENV_ID_ENV: 'stale'}, 'c', 't', 'v2', avoid_mocks=False)
    assert env[VENV_ENV_ID_ENV] == 'v2'


def test_child_env_has_no_overlay_path():
    # ROCKETRIDE_VENV_SITE is retired: the child resolves its own overlay from the env id.
    env = build_child_env({}, 'c', 't', 'v1', avoid_mocks=False)
    assert 'ROCKETRIDE_VENV_SITE' not in env


def test_child_env_strips_mocks_only_under_avoid_mocks():
    base = {'ROCKETRIDE_MOCK': '/mocks'}
    assert 'ROCKETRIDE_MOCK' in build_child_env(base, 'c', 't', 'v1', avoid_mocks=False)
    assert 'ROCKETRIDE_MOCK' not in build_child_env(base, 'c', 't', 'v1', avoid_mocks=True)


# --- forced requirements: the parent's half of the handshake (§4.7.1) -------


def test_normalize_folds_line_endings_so_a_textarea_round_trip_does_not_rebuild():
    """A browser textarea turning \\n into \\r\\n changes no requirement and must not rebuild.

    Having argued file mtime out of the environment key for exactly that reason, leaving line
    endings in the digest would reinstate the trap one layer up.
    """
    lf = normalize_forced_text('tabulate==0.9.0\nnumpy\n')
    crlf = normalize_forced_text('tabulate==0.9.0\r\nnumpy\r\n')
    cr = normalize_forced_text('tabulate==0.9.0\rnumpy\r')
    assert lf == crlf == cr
    assert forced_digest(lf) == forced_digest(crlf) == forced_digest(cr)


def test_normalize_strips_trailing_whitespace_and_canonicalises_the_last_newline():
    assert normalize_forced_text('tabulate==0.9.0   \n\n\n') == 'tabulate==0.9.0\n'
    assert normalize_forced_text('tabulate==0.9.0') == 'tabulate==0.9.0\n'


def test_blank_forced_text_is_the_no_forced_value_all_the_way_down():
    # Absent, empty and whitespace-only are one case: empty digest, no file, empty variable.
    for blank in (None, '', '   ', '\n\n', '\r\n'):
        assert normalize_forced_text(blank) == ''
    assert forced_digest('') == ''


def test_digest_is_the_whole_sha256_and_is_also_the_file_name(tmp_path):
    # Two lengths for one identity is how a parent writing sixteen hex and a child opening
    # sixty-four would agree with every word of the design and still never find the file.
    text = normalize_forced_text('tabulate==0.9.0\n')
    digest = forced_digest(text)
    assert len(digest) == 64
    path = write_forced_file(str(tmp_path), text, digest)
    assert os.path.basename(path) == f'{digest}.txt'
    assert os.path.dirname(path).replace('\\', '/').endswith('cache/forced')


def test_write_creates_the_cache_directory_on_the_very_first_run(tmp_path):
    # engine_cache_dir() is create=False by default and cache/forced/ has never existed, so a
    # blind write fails on precisely the run the feature is most likely to be tried on.
    assert not (tmp_path / 'cache').exists()
    text = normalize_forced_text('numpy\n')
    path = write_forced_file(str(tmp_path), text, forced_digest(text))
    assert os.path.isfile(path)
    assert open(path, encoding='utf-8').read() == text


def test_write_is_write_if_absent_and_leaves_an_existing_file_alone(tmp_path):
    text = normalize_forced_text('numpy\n')
    digest = forced_digest(text)
    path = write_forced_file(str(tmp_path), text, digest)
    before = os.stat(path).st_mtime_ns
    time.sleep(0.01)
    assert write_forced_file(str(tmp_path), text, digest) == path
    assert os.stat(path).st_mtime_ns == before, 'the name is the digest, so an existing file is the right one'


def test_write_leaves_no_temporary_behind(tmp_path):
    text = normalize_forced_text('numpy\n')
    write_forced_file(str(tmp_path), text, forced_digest(text))
    leftovers = [n for n in os.listdir(tmp_path / 'cache' / 'forced') if n.endswith('.tmp')]
    assert leftovers == []


def test_empty_digest_writes_nothing(tmp_path):
    assert write_forced_file(str(tmp_path), '', '') is None
    assert not (tmp_path / 'cache').exists()


def test_two_documents_for_one_project_each_get_their_own_file(tmp_path):
    """The multi-team deployment case: one project runs at several versions at once.

    ``deployments`` is unique on (team_id, project_id) and each row carries its own version, so
    two teams' documents land on one pod. Under a single fixed filename the second would either
    clobber the first or be refused for doing nothing wrong; the digest naming removes the case.
    """
    a = normalize_forced_text('tabulate==0.9.0\n')
    b = normalize_forced_text('tabulate==0.10.0\n')
    pa = write_forced_file(str(tmp_path), a, forced_digest(a))
    pb = write_forced_file(str(tmp_path), b, forced_digest(b))
    assert pa != pb
    assert os.path.isfile(pa) and os.path.isfile(pb), 'both documents must still run'
    assert open(pa, encoding='utf-8').read() == a
    assert open(pb, encoding='utf-8').read() == b


def test_writing_never_removes_another_documents_file(tmp_path):
    # Sweeping is the plausible wrong instinct and it is how one deployed version would delete
    # another's. Purge and GC clear venvs/, which is not where this lives.
    stale = tmp_path / 'cache' / 'forced' / ('0' * 64 + '.txt')
    stale.parent.mkdir(parents=True)
    stale.write_text('six==1.16.0\n', encoding='utf-8')
    text = normalize_forced_text('numpy\n')
    write_forced_file(str(tmp_path), text, forced_digest(text))
    assert stale.is_file(), "a file this run does not name is not this run's business"


def test_write_succeeds_while_an_environments_install_lock_is_held(tmp_path):
    """The regression guard for the lock question §4.7.1 reversed.

    ``depends.FileLock`` is unreachable from this package (importing ``depends`` pulls in
    ``engLib``) and ``venv_env._EnvLock`` is private and **non-blocking**, so a parent that
    starts taking a lock would raise ``EnvBusy`` on an ordinary concurrent install. This holds a
    real OS lock with the same primitive family and asserts the write is unbothered by it.
    """
    lock_path = tmp_path / 'venvs' / 'p' / 'main' / 'install.lock'
    lock_path.parent.mkdir(parents=True)
    lock_path.touch()
    text = normalize_forced_text('numpy\n')

    with open(lock_path, 'a+b') as held:
        if os.name == 'nt':
            import msvcrt

            held.seek(0)
            msvcrt.locking(held.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            path = write_forced_file(str(tmp_path), text, forced_digest(text))
            assert os.path.isfile(path)
        finally:
            if os.name == 'nt':
                held.seek(0)
                msvcrt.locking(held.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(held.fileno(), fcntl.LOCK_UN)


def test_child_env_carries_the_forced_digest():
    env = build_child_env({'PATH': '/x'}, 'c', 't', 'v1', avoid_mocks=False, forced_sha='abc123')
    assert env[VENV_FORCED_SHA_ENV] == 'abc123'


def test_child_env_overwrites_an_inherited_forced_digest():
    # Assigned, never conditionally set -- like the env id, and for the same reason: a child must
    # carry exactly one answer, so a stale inherited value has to lose.
    env = build_child_env({VENV_FORCED_SHA_ENV: 'stale'}, 'c', 't', 'v1', avoid_mocks=False)
    assert env[VENV_FORCED_SHA_ENV] == ''
