"""Unit tests for ``venv_spawn`` orchestration helpers that are pure (no live child).

Covers ``inject_venv_urls``: turning the partitioner's per-child ``venv`` node into a live
loopback URL, including the ``&return=`` binding when the venv returns data (step 8.1, Arch-1:
one bridge node per child carries all its lanes over one socket).
"""

import pytest

from ai.modules.task.venv_spawn import inject_venv_urls


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
