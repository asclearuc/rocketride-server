"""Unit tests for ``venv_spawn`` helpers that are pure (no live child).

Covers ``inject_venv_urls`` -- turning the partitioner's per-child ``venv`` node into a live
loopback URL, including the ``&return=`` binding when the venv returns data (step 8.1, Arch-1:
one bridge node per child carries all its lanes over one socket) -- ``classify_child_event``,
the routing table the 8.4 fan-in acts on, and ``await_child_ready``, the 8.5A readiness wait.

The readiness cases use a real loopback listener rather than a mocked socket: the whole point of
the two-phase wait is how it behaves against an accepting vs. a refusing port, which a mock would
simply assert away. They pass tiny ``silence_ceiling``/``interval`` values instead of sleeping out
the real ~30 s budget.
"""

import asyncio
import socket
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
    VENV_TRACE_EVENT,
    VenvChild,
    await_child_ready,
    classify_child_event,
    inject_venv_urls,
)


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
    ceiling = 0.1

    async def chatter():
        for _ in range(10):
            await asyncio.sleep(ceiling / 4)
            child.last_event_at = time.monotonic()
        child.ready.set()

    task = asyncio.create_task(chatter())
    started = time.monotonic()
    try:
        outcome = await await_child_ready(
            child, '127.0.0.1', port, child.process, silence_ceiling=ceiling, interval=0.01
        )
        assert outcome == READY_CONFIRMED
        assert time.monotonic() - started > ceiling, 'the old fixed deadline would have killed it here'
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
