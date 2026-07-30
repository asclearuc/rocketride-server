"""Unit tests for ``venv_spawn`` helpers that are pure (no live child).

Covers ``inject_venv_urls`` -- turning the partitioner's per-child ``venv`` node into a live
loopback URL, including the ``&return=`` binding when the venv returns data (step 8.1, Arch-1:
one bridge node per child carries all its lanes over one socket) -- and
``classify_child_event``, the routing table the 8.4 fan-in acts on.
"""

import pytest

from ai.modules.task.venv_spawn import (
    CH_DETAIL,
    CH_FLOW,
    CH_NONE,
    CH_OUTPUT,
    CH_SSE,
    SE_ERROR,
    SE_EXIT,
    SE_METRICS,
    SE_STATUS_TRACE,
    SE_STATUS_WINDOW,
    SE_TAIL,
    SE_WARNING,
    VENV_TRACE_EVENT,
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
