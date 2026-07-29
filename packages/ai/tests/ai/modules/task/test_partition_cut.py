"""
Unit tests for the partitioner's cut (``partition_pipeline(..., scoped=True)``).

Increment 1 (flattening, ``scoped=False``) is covered by ``test_partition.py`` and its
19 cases are left untouched. This file covers increment 2: cutting isolated groups into a
per-venv sub-document each, wired by ``venv``/``venv_server`` bridge pairs at every boundary
data-lane edge, with a routing table keyed by ``channelId`` and env-cycle detection over the
env-quotient graph. See ``packages/server/design/virtual-environments.md`` §4.3/§4.6.
"""

import copy

import pytest

from ai.modules.task.pipeline import PartitionResult, has_isolated_group, partition_pipeline


# ---------------------------------------------------------------------------
# Helpers (same document shape as test_partition.py)
# ---------------------------------------------------------------------------


def _node(component_id, **extra):
    """A plain processing component."""
    return {'id': component_id, 'provider': 'default', 'config': {}, **extra}


def _container(container_id, members=None, environment=None, **extra):
    """A group (no environment) or a virtual environment (with one)."""
    config = {}
    if environment is not None:
        config['environment'] = environment
    if members is not None:
        config['pipeline'] = {'components': members}
    return {'id': container_id, 'provider': 'default', 'config': config, **extra}


def _venv(container_id, members, name='v'):
    """An isolated virtual-environment container."""
    return _container(container_id, members=members, environment={'name': name, 'isolated': True})


def _ids(document):
    return [component['id'] for component in document['components']]


def _routes(result):
    return {entry['channelId']: entry for entry in result.routing}


# ---------------------------------------------------------------------------
# has_isolated_group
# ---------------------------------------------------------------------------


def test_has_isolated_group_detects_only_isolated_containers():
    assert has_isolated_group({'components': [_venv('v', [_node('a')])]}) is True
    assert has_isolated_group({'components': [_container('g', members=[_node('a')])]}) is False
    assert has_isolated_group({'components': [_node('a')]}) is False


# ---------------------------------------------------------------------------
# Output shape
# ---------------------------------------------------------------------------


def test_scoped_without_isolated_groups_wraps_the_flattened_main():
    pipeline = {'components': [_node('a'), _container('g', members=[_node('b')])]}

    result = partition_pipeline(pipeline, scoped=True)

    assert isinstance(result, PartitionResult)
    assert list(result.environments) == ['main']
    assert _ids(result.environments['main']) == ['a', 'b']
    assert result.routing == []
    assert result.groups == {}


def test_scoped_false_still_returns_a_flat_document():
    pipeline = {'components': [_node('a'), _venv('v', [_node('b')])]}

    flat = partition_pipeline(pipeline, scoped=False)

    assert isinstance(flat, dict)
    assert _ids(flat) == ['a', 'b']


def test_environments_are_main_first_then_groups_in_document_order():
    pipeline = {
        'source': 'src',
        'components': [
            _node('src'),
            _node('parse', input=[{'lane': 'text', 'from': 'src'}]),
            _venv('v1', [_node('a', input=[{'lane': 'text', 'from': 'parse'}])], name='v1'),
            _venv('v2', [_node('b', input=[{'lane': 'text', 'from': 'parse'}])], name='v2'),
        ],
    }

    result = partition_pipeline(pipeline, scoped=True)

    assert list(result.environments) == ['main', 'v1', 'v2']


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_main_to_venv_inserts_client_in_main_and_server_in_child():
    parse = _node('parse')
    detect = _node('detect', input=[{'lane': 'image', 'from': 'parse'}])
    pipeline = {'project_id': 'p1', 'source': 'parse', 'components': [parse, _venv('vision', [detect])]}

    result = partition_pipeline(pipeline, scoped=True)
    main = result.environments['main']
    child = result.environments['vision']

    assert main['project_id'] == 'p1' and main['source'] == 'parse'
    egress = [c for c in main['components'] if c['provider'] == 'venv']
    assert len(egress) == 1
    assert egress[0]['input'] == [{'lane': 'image', 'from': 'parse'}]
    assert egress[0]['config'] == {
        'channelId': 'main->vision',
        'sourceEnv': 'main',
        'targetEnv': 'vision',
        'lanes': ['image'],
    }

    assert child['source'] == 'venv_source_stub'
    assert child['components'][0] == {'id': 'venv_source_stub', 'provider': 'venv_source_stub', 'config': {}}
    server = [c for c in child['components'] if c['provider'] == 'venv_server']
    assert len(server) == 1
    assert server[0]['input'] == [{'lane': 'image', 'from': 'venv_source_stub'}]
    detect_out = next(c for c in child['components'] if c['id'] == 'detect')
    assert detect_out['input'] == [{'lane': 'image', 'from': server[0]['id']}]

    assert len(result.routing) == 1
    entry = result.routing[0]
    assert entry == {
        'channelId': 'main->vision',
        'sourceEnv': 'main',
        'targetEnv': 'vision',
        'lanes': ['image'],
        'producers': {'image': 'parse'},
        'consumers': {'image': ['detect']},
        'egressNode': egress[0]['id'],
        'ingressNode': server[0]['id'],
    }
    assert result.groups == {'vision': {'name': 'v', 'isolated': True}}


def test_venv_to_main_delivers_the_return_through_the_round_trip_node():
    seed = _node('seed')
    gen = _node('gen', input=[{'lane': 'text', 'from': 'seed'}])
    out = _node('out', input=[{'lane': 'text', 'from': 'gen'}])
    pipeline = {'source': 'seed', 'components': [seed, _venv('w', [gen]), out]}

    result = partition_pipeline(pipeline, scoped=True)
    routes = _routes(result)

    assert 'main->w' in routes
    assert 'w->main' in routes

    ret = routes['w->main']
    fwd = routes['main->w']

    # main holds ONE round-trip node: it reads the forward producer, and both sends forward
    # and delivers the return -- so there is no separate input-less ingress node.
    main = result.environments['main']
    clients = [c for c in main['components'] if c['provider'] == 'venv']
    assert len(clients) == 1
    node = clients[0]
    assert node['id'] == fwd['egressNode']
    assert node['input'] == [{'lane': 'text', 'from': 'seed'}]
    assert node['config'] == {
        'channelId': 'main->w',
        'sourceEnv': 'main',
        'targetEnv': 'w',
        'lanes': ['text'],
        'returnChannelId': 'w->main',
        'returnLanes': ['text'],
    }
    # The return consumer reads from that same round-trip node (the return's deliverNode),
    # and the return routing entry points its ingress there.
    out_out = next(c for c in main['components'] if c['id'] == 'out')
    assert out_out['input'] == [{'lane': 'text', 'from': fwd['egressNode']}]
    assert ret['ingressNode'] == fwd['egressNode']

    # The child holds both the forward ingress (from the stub) and the return egress (from gen).
    child = result.environments['w']
    servers = [c for c in child['components'] if c['provider'] == 'venv_server']
    assert len(servers) == 2
    egress = next(c for c in child['components'] if c['id'] == ret['egressNode'])
    assert egress['input'] == [{'lane': 'text', 'from': 'gen'}]
    ingress = next(c for c in child['components'] if c['id'] == fwd['ingressNode'])
    assert ingress['input'] == [{'lane': 'text', 'from': 'venv_source_stub'}]


def test_venv_to_venv_becomes_an_edge_between_bridge_nodes():
    # Graph serialization (§4.6): a venv->venv lane is cut at BOTH boundaries and reaches its
    # consumer as an ordinary main-graph edge between the two bridge nodes. v2 is a sink here —
    # fed only by another venv, producing nothing — so it gets a forward channel and no return.
    seed = _node('seed')
    parse = _node('parse', input=[{'lane': 'text', 'from': 'seed'}])
    detect = _node('detect', input=[{'lane': 'image', 'from': 'parse'}])
    pipeline = {
        'source': 'seed',
        'components': [seed, _venv('v1', [parse], name='v1'), _venv('v2', [detect], name='v2')],
    }

    result = partition_pipeline(pipeline, scoped=True)

    routes = _routes(result)
    assert set(routes) == {'main->v1', 'v1->main', 'main->v2'}, 'v2 is a sink: no return channel'

    # v1's image leaves through its own bridge, and that bridge is what v2's bridge reads.
    main = result.environments['main']
    v1_node = next(c for c in main['components'] if c['id'] == routes['main->v1']['egressNode'])
    v2_node = next(c for c in main['components'] if c['id'] == routes['main->v2']['egressNode'])
    assert v1_node['input'] == [{'lane': 'text', 'from': 'seed'}]
    assert v2_node['input'] == [{'lane': 'image', 'from': v1_node['id']}]

    # A sink venv is dialled without a return, so no `&return=` at spawn.
    assert 'returnChannelId' not in v2_node['config']
    assert v1_node['config']['returnChannelId'] == 'v1->main'

    # The routing entry keeps the AUTHORED producer, not the bridge that relays it.
    assert routes['main->v2']['producers'] == {'image': 'parse'}

    # Each child is untouched by the chain: the linear step-7 shape, twice.
    detect_out = next(c for c in result.environments['v2']['components'] if c['id'] == 'detect')
    assert detect_out['input'] == [{'lane': 'image', 'from': routes['main->v2']['ingressNode']}]


# ---------------------------------------------------------------------------
# Fan-out / fan-in / multi-lane
# ---------------------------------------------------------------------------


def test_fan_out_to_two_envs_makes_two_channels():
    pipeline = {
        'source': 'src',
        'components': [
            _node('src'),
            _node('parse', input=[{'lane': 'text', 'from': 'src'}]),
            _venv('v1', [_node('a', input=[{'lane': 'text', 'from': 'parse'}])], name='v1'),
            _venv('v2', [_node('b', input=[{'lane': 'text', 'from': 'parse'}])], name='v2'),
        ],
    }

    result = partition_pipeline(pipeline, scoped=True)
    channels = [entry for entry in result.routing if 'parse' in entry['producers'].values()]

    assert len(channels) == 2
    assert {entry['targetEnv'] for entry in channels} == {'v1', 'v2'}


def test_fan_in_within_one_env_shares_a_single_channel():
    pipeline = {
        'source': 'parse',
        'components': [
            _node('parse'),
            _venv(
                'v',
                [
                    _node('a', input=[{'lane': 'text', 'from': 'parse'}]),
                    _node('b', input=[{'lane': 'text', 'from': 'parse'}]),
                ],
            ),
        ],
    }

    result = partition_pipeline(pipeline, scoped=True)
    channels = [entry for entry in result.routing if 'parse' in entry['producers'].values()]

    assert len(channels) == 1
    assert sorted(channels[0]['consumers']['text']) == ['a', 'b']


def test_multiple_lanes_into_one_venv_makes_one_channel_carrying_both():
    # Multi-lane fan-in: ONE bridge node carries both lanes over one socket; the frame lane
    # header demuxes them (Arch-1). Node identity is not needed because the lanes differ.
    pipeline = {
        'source': 'parse',
        'components': [
            _node('parse'),
            _venv('v', [_node('a', input=[{'lane': 'text', 'from': 'parse'}, {'lane': 'image', 'from': 'parse'}])]),
        ],
    }

    result = partition_pipeline(pipeline, scoped=True)
    routes = _routes(result)

    assert set(routes) == {'main->v'}
    assert sorted(routes['main->v']['lanes']) == ['image', 'text']
    main = result.environments['main']
    clients = [c for c in main['components'] if c['provider'] == 'venv']
    assert len(clients) == 1
    # The one bridge node reads BOTH lanes from parse; the child member reads both from the ingress.
    assert sorted((e['lane'], e['from']) for e in clients[0]['input']) == [('image', 'parse'), ('text', 'parse')]
    child = result.environments['v']
    a_out = next(c for c in child['components'] if c['id'] == 'a')
    ingress = routes['main->v']['ingressNode']
    assert sorted((e['lane'], e['from']) for e in a_out['input']) == [('image', ingress), ('text', ingress)]


def test_same_lane_from_two_producers_into_one_venv_is_rejected():
    # Two producers on the SAME lane into one venv: a single bridge node cannot tell them apart
    # (write* carries no producer identity), so the shape is rejected (Arch-1). Workaround: a
    # merge/router node inside the venv.
    pipeline = {
        'source': 'src',
        'components': [
            _node('src'),
            _node('parse_a', input=[{'lane': 'text', 'from': 'src'}]),
            _node('parse_b', input=[{'lane': 'text', 'from': 'src'}]),
            _venv(
                'v',
                [
                    _node('merge_a', input=[{'lane': 'text', 'from': 'parse_a'}]),
                    _node('merge_b', input=[{'lane': 'text', 'from': 'parse_b'}]),
                ],
            ),
        ],
    }

    with pytest.raises(ValueError, match='more than one producer'):
        partition_pipeline(pipeline, scoped=True)


def test_multi_lane_return_carries_all_lanes_on_one_channel():
    # A venv returns two DIFFERENT lanes to main: one return channel carries both, delivered
    # through the round-trip node, which records both in returnLanes.
    pipeline = {
        'source': 'seed',
        'components': [
            _node('seed'),
            _venv(
                'v',
                [
                    _node('g', input=[{'lane': 'text', 'from': 'seed'}]),
                    _node('h', input=[{'lane': 'text', 'from': 'seed'}]),
                ],
            ),
            _node('out_text', input=[{'lane': 'text', 'from': 'g'}]),
            _node('out_json', input=[{'lane': 'json', 'from': 'h'}]),
        ],
    }

    result = partition_pipeline(pipeline, scoped=True)
    routes = _routes(result)

    assert set(routes) == {'main->v', 'v->main'}
    assert sorted(routes['v->main']['lanes']) == ['json', 'text']
    main = result.environments['main']
    node = next(c for c in main['components'] if c['provider'] == 'venv')
    assert node['config']['returnChannelId'] == 'v->main'
    assert sorted(node['config']['returnLanes']) == ['json', 'text']
    # Both return consumers read from that one round-trip node.
    out_text = next(c for c in main['components'] if c['id'] == 'out_text')
    out_json = next(c for c in main['components'] if c['id'] == 'out_json')
    assert out_text['input'][0]['from'] == node['id']
    assert out_json['input'][0]['from'] == node['id']


def test_same_lane_return_from_two_producers_is_rejected():
    # Two venv producers return the SAME lane to main: one egress node cannot tell them apart,
    # so it is rejected (the return-side mirror of the fan-in rule).
    pipeline = {
        'source': 'seed',
        'components': [
            _node('seed'),
            _venv(
                'v',
                [
                    _node('g', input=[{'lane': 'text', 'from': 'seed'}]),
                    _node('h', input=[{'lane': 'text', 'from': 'seed'}]),
                ],
            ),
            _node('out_g', input=[{'lane': 'text', 'from': 'g'}]),
            _node('out_h', input=[{'lane': 'text', 'from': 'h'}]),
        ],
    }

    with pytest.raises(ValueError, match='more than one producer'):
        partition_pipeline(pipeline, scoped=True)


def test_two_independent_round_trips_each_get_their_own_node():
    # Two separate linear main->venv->main splices: each venv has its own round-trip node.
    pipeline = {
        'source': 'src',
        'components': [
            _node('src'),
            _venv('v1', [_node('a', input=[{'lane': 'text', 'from': 'src'}])], name='v1'),
            _node('mid', input=[{'lane': 'text', 'from': 'a'}]),
            _venv('v2', [_node('b', input=[{'lane': 'text', 'from': 'mid'}])], name='v2'),
            _node('out', input=[{'lane': 'text', 'from': 'b'}]),
        ],
    }

    result = partition_pipeline(pipeline, scoped=True)
    routes = _routes(result)
    assert set(routes) == {'main->v1', 'v1->main', 'main->v2', 'v2->main'}
    # Two round-trip nodes in main, one per venv; each carries its own return channel.
    main = result.environments['main']
    clients = [c for c in main['components'] if c['provider'] == 'venv']
    assert len(clients) == 2
    assert {c['config']['returnChannelId'] for c in clients} == {'v1->main', 'v2->main'}
    # mid consumes v1's return through v1's round-trip node; out consumes v2's the same way.
    mid_out = next(c for c in main['components'] if c['id'] == 'mid')
    assert mid_out['input'] == [{'lane': 'text', 'from': routes['main->v1']['egressNode']}]
    out_out = next(c for c in main['components'] if c['id'] == 'out')
    assert out_out['input'] == [{'lane': 'text', 'from': routes['main->v2']['egressNode']}]


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------


def test_a_to_b_to_main_chain_is_wired_bridge_to_bridge():
    # main->v1->v2->main. In main the chain is dropper -> MV1 -> MV2 -> ret, with the lane
    # changing across the boundary (text in, image out) to prove each channel carries its own.
    pipeline = {
        'source': 'dropper',
        'components': [
            _node('dropper'),
            _venv('v1', [_node('parse', input=[{'lane': 'text', 'from': 'dropper'}])], name='v1'),
            _venv('v2', [_node('detect', input=[{'lane': 'image', 'from': 'parse'}])], name='v2'),
            _node('ret', input=[{'lane': 'image', 'from': 'detect'}]),
        ],
    }

    result = partition_pipeline(pipeline, scoped=True)

    routes = _routes(result)
    assert set(routes) == {'main->v1', 'v1->main', 'main->v2', 'v2->main'}
    mv1, mv2 = routes['main->v1']['egressNode'], routes['main->v2']['egressNode']

    main = result.environments['main']
    assert [c for c in main['components'] if c['provider'] == 'venv'].__len__() == 2, 'one bridge per venv'
    assert next(c for c in main['components'] if c['id'] == mv1)['input'] == [{'lane': 'text', 'from': 'dropper'}]
    assert next(c for c in main['components'] if c['id'] == mv2)['input'] == [{'lane': 'image', 'from': mv1}]
    # The main-side consumer reads the last venv through its round-trip node.
    assert next(c for c in main['components'] if c['id'] == 'ret')['input'] == [{'lane': 'image', 'from': mv2}]

    # Each child keeps the linear step-7 shape: stub + ingress + egress + its own node.
    for env, node_id, lane_in, lane_out in (('v1', 'parse', 'text', 'image'), ('v2', 'detect', 'image', 'image')):
        child = result.environments[env]
        assert sum(1 for c in child['components'] if c['provider'] == 'venv_server') == 2
        assert next(c for c in child['components'] if c['id'] == node_id)['input'] == [
            {'lane': lane_in, 'from': routes[f'main->{env}']['ingressNode']}
        ]
        egress = next(c for c in child['components'] if c['id'] == routes[f'{env}->main']['egressNode'])
        assert egress['input'] == [{'lane': lane_out, 'from': node_id}]


def test_venv_feeding_two_venvs_fans_out_from_one_bridge():
    # One producer, many consumers is legal — even when the consumers live in different venvs.
    # v1 returns `text` once; both v2 and v3 read that single delivered lane off MV1.
    a = _node('a', input=[{'lane': 'text', 'from': 'seed'}])
    pipeline = {
        'source': 'seed',
        'components': [
            _node('seed'),
            _venv('v1', [a], name='v1'),
            _venv('v2', [_node('b', input=[{'lane': 'text', 'from': 'a'}])], name='v2'),
            _venv('v3', [_node('c', input=[{'lane': 'text', 'from': 'a'}])], name='v3'),
        ],
    }

    result = partition_pipeline(pipeline, scoped=True)

    routes = _routes(result)
    mv1 = routes['main->v1']['egressNode']
    main = result.environments['main']
    for env in ('v2', 'v3'):
        node = next(c for c in main['components'] if c['id'] == routes[f'main->{env}']['egressNode'])
        assert node['input'] == [{'lane': 'text', 'from': mv1}]
    # One return channel on v1, whose consumers live in two different child documents.
    assert routes['v1->main']['consumers'] == {'text': ['b', 'c']}


def test_venv_to_venv_cycle_is_rejected():
    # seed(main) -> a(v1); a<->b across v1/v2 forms a venv cycle. The two edges into v1 must
    # use DISTINCT lanes: the merged-boundary conflict check sits upstream of cycle detection,
    # so same-lane entries would be rejected as a conflict and never reach it.
    a = _node('a', input=[{'lane': 'text', 'from': 'seed'}, {'lane': 'json', 'from': 'b'}])
    b = _node('b', input=[{'lane': 'text', 'from': 'a'}])
    pipeline = {
        'source': 'seed',
        'components': [_node('seed'), _venv('v1', [a], name='v1'), _venv('v2', [b], name='v2')],
    }

    with pytest.raises(ValueError, match='cycle'):
        partition_pipeline(pipeline, scoped=True)


def test_venv_entered_twice_around_a_main_node_is_rejected():
    # Authored as a DAG (seed -> a(v1) -> m(main) -> b(v1) -> out), but v1 collapses to ONE
    # bridge node, so it rewrites to MV1 -> m -> MV1. Distinct lanes per direction, again so
    # the conflict check does not pre-empt the cycle check.
    a = _node('a', input=[{'lane': 'text', 'from': 'seed'}])
    b = _node('b', input=[{'lane': 'json', 'from': 'm'}])
    pipeline = {
        'source': 'seed',
        'components': [
            _node('seed'),
            _venv('v1', [a, b], name='v1'),
            _node('m', input=[{'lane': 'text', 'from': 'a'}]),
            _node('out', input=[{'lane': 'json', 'from': 'b'}]),
        ],
    }

    with pytest.raises(ValueError, match='entered more than once'):
        partition_pipeline(pipeline, scoped=True)


def test_a_user_cycle_without_a_bridge_is_left_to_the_engine():
    # The collapsed-cycle check is deliberately narrow: a cycle that does not pass through a
    # bridge node is not the cut's doing, so the partitioner must not become stricter than the
    # engine on shapes that have nothing to do with venvs.
    pipeline = {
        'source': 'seed',
        'components': [
            _node('seed'),
            _node('x', input=[{'lane': 'text', 'from': 'seed'}, {'lane': 'text', 'from': 'y'}]),
            _node('y', input=[{'lane': 'text', 'from': 'x'}]),
            _venv('v1', [_node('a', input=[{'lane': 'text', 'from': 'seed'}])], name='v1'),
        ],
    }

    result = partition_pipeline(pipeline, scoped=True)
    assert 'v1' in result.environments


def test_venv_that_only_emits_is_rejected():
    # Nothing dials the child, so its egress would have no socket to ship the return on. This
    # is also what keeps the forward-source lookup from dying on a KeyError instead.
    pipeline = {
        'source': 'seed',
        'components': [
            _node('seed'),
            _venv('v1', [_node('a')], name='v1'),
            _node('out', input=[{'lane': 'text', 'from': 'a'}]),
        ],
    }

    with pytest.raises(ValueError, match='nothing is routed into it'):
        partition_pipeline(pipeline, scoped=True)


# ---------------------------------------------------------------------------
# Rejections specific to the cut
# ---------------------------------------------------------------------------


def test_boundary_edge_on_words_is_rejected():
    pipeline = {
        'source': 'parse',
        'components': [_node('parse'), _venv('v', [_node('a', input=[{'lane': 'words', 'from': 'parse'}])])],
    }

    with pytest.raises(ValueError, match='not bridgeable'):
        partition_pipeline(pipeline, scoped=True)


def test_group_named_main_is_rejected():
    pipeline = {'components': [_node('x'), _venv('main', [_node('a')])]}

    with pytest.raises(ValueError, match='reserved'):
        partition_pipeline(pipeline, scoped=True)


def test_implied_source_inside_a_venv_is_rejected():
    src = _node('src')
    src['config'] = {'mode': 'Source'}
    pipeline = {'components': [_node('outside'), _venv('v', [src])]}

    with pytest.raises(ValueError, match='source must stay outside'):
        partition_pipeline(pipeline, scoped=True)


def test_source_field_naming_a_venv_member_is_rejected():
    pipeline = {'source': 'a', 'components': [_node('outside'), _venv('v', [_node('a')])]}

    with pytest.raises(ValueError, match='source must stay outside'):
        partition_pipeline(pipeline, scoped=True)


def test_empty_main_with_one_venv_is_rejected():
    pipeline = {'components': [_venv('v', [_node('a')])]}

    with pytest.raises(ValueError, match='base environment'):
        partition_pipeline(pipeline, scoped=True)


def test_empty_main_with_two_venvs_is_rejected():
    pipeline = {'components': [_venv('v1', [_node('a')], name='v1'), _venv('v2', [_node('b')], name='v2')]}

    with pytest.raises(ValueError, match='base environment'):
        partition_pipeline(pipeline, scoped=True)


def test_control_edge_from_a_container_is_rejected():
    inner = _node('agent', control=[{'classType': 'llm', 'from': 'g'}])
    pipeline = {'components': [_container('g', members=[_node('llm')]), _venv('v', [inner])]}

    with pytest.raises(ValueError, match='produces no data'):
        partition_pipeline(pipeline, scoped=True)


# ---------------------------------------------------------------------------
# Transitive env_of (fixes both dual increment-1 bugs)
# ---------------------------------------------------------------------------


def test_transitive_cross_boundary_control_into_nested_member_is_rejected():
    # control from a main node into a member of a plain group nested inside a venv.
    member = _node('agent', control=[{'classType': 'llm', 'from': 'llm_outside'}])
    pipeline = {'components': [_node('llm_outside'), _venv('v', [_container('g', members=[member])])]}

    with pytest.raises(ValueError, match='crosses a virtual environment boundary'):
        partition_pipeline(pipeline, scoped=True)


def test_transitive_intra_env_control_into_nested_member_is_allowed():
    member = _node('agent', control=[{'classType': 'llm', 'from': 'llm_inside'}])
    pipeline = {
        'source': 'seed',
        'components': [
            _node('seed'),
            _venv(
                'v', [_node('llm_inside', input=[{'lane': 'text', 'from': 'seed'}]), _container('g', members=[member])]
            ),
        ],
    }

    result = partition_pipeline(pipeline, scoped=True)

    assert 'v' in result.environments


def test_venv_nested_in_a_plain_group_cuts_to_a_top_level_env():
    detect = _node('detect', input=[{'lane': 'image', 'from': 'parse'}])
    pipeline = {
        'source': 'parse',
        'components': [_node('parse'), _container('layout', members=[_venv('vision', [detect])])],
    }

    result = partition_pipeline(pipeline, scoped=True)

    assert set(result.environments) == {'main', 'vision'}
    assert 'detect' in _ids(result.environments['vision'])


# ---------------------------------------------------------------------------
# Corner cases
# ---------------------------------------------------------------------------


def test_empty_venv_gets_no_environment_entry():
    pipeline = {'source': 'a', 'components': [_node('a'), _venv('empty', [])]}

    result = partition_pipeline(pipeline, scoped=True)

    assert list(result.environments) == ['main']


def test_channel_less_venv_still_gets_an_entry():
    pipeline = {
        'source': 'a',
        'components': [_node('a'), _venv('island', [_node('x'), _node('y', input=[{'lane': 'text', 'from': 'x'}])])],
    }

    result = partition_pipeline(pipeline, scoped=True)

    assert 'island' in result.environments
    assert result.routing == []


def test_unknown_from_reference_is_left_untouched():
    detect = _node('detect', input=[{'lane': 'image', 'from': 'ghost'}])
    pipeline = {'source': 'parse', 'components': [_node('parse'), _venv('vision', [detect])]}

    result = partition_pipeline(pipeline, scoped=True)

    assert result.routing == []
    child = result.environments['vision']
    detect_out = next(c for c in child['components'] if c['id'] == 'detect')
    assert detect_out['input'] == [{'lane': 'image', 'from': 'ghost'}]


def test_synthesized_id_collision_is_suffixed():
    collide = 'venv_ingress--main--vision'
    detect = _node('detect', input=[{'lane': 'image', 'from': 'parse'}])
    pipeline = {'source': 'parse', 'components': [_node('parse'), _venv('vision', [detect, _node(collide)])]}

    result = partition_pipeline(pipeline, scoped=True)

    assert result.routing[0]['ingressNode'] == collide + '-2'


# ---------------------------------------------------------------------------
# Determinism / immutability / field propagation
# ---------------------------------------------------------------------------


def test_the_input_pipeline_is_not_modified():
    detect = _node('detect', input=[{'lane': 'image', 'from': 'parse'}])
    pipeline = {'source': 'parse', 'components': [_node('parse'), _venv('vision', [detect])]}

    partition_pipeline(pipeline, scoped=True)

    assert detect['input'] == [{'lane': 'image', 'from': 'parse'}]


def test_the_cut_is_deterministic():
    pipeline = {
        'source': 'dropper',
        'components': [
            _node('dropper'),
            _venv('v1', [_node('parse', input=[{'lane': 'text', 'from': 'dropper'}])], name='v1'),
            _node('ret', input=[{'lane': 'text', 'from': 'parse'}]),
        ],
    }

    first = partition_pipeline(copy.deepcopy(pipeline), scoped=True)
    second = partition_pipeline(copy.deepcopy(pipeline), scoped=True)

    assert [_ids(doc) for doc in first.environments.values()] == [_ids(doc) for doc in second.environments.values()]
    assert first.routing == second.routing


def test_project_id_propagates_to_every_document():
    pipeline = {
        'project_id': 'pid-123',
        'source': 'parse',
        'components': [_node('parse'), _venv('vision', [_node('detect', input=[{'lane': 'image', 'from': 'parse'}])])],
    }

    result = partition_pipeline(pipeline, scoped=True)

    assert all(doc.get('project_id') == 'pid-123' for doc in result.environments.values())


def test_main_document_preserves_the_original_source_field():
    parse = _node('parse')
    detect = _node('detect', input=[{'lane': 'image', 'from': 'parse'}])
    pipeline = {'source': 'parse', 'components': [parse, _venv('vision', [detect])]}

    # A source param that differs from the field must not be written into the main doc.
    result = partition_pipeline(pipeline, source='parse', scoped=True)

    assert result.environments['main']['source'] == 'parse'


# ---------------------------------------------------------------------------
# Golden: a linear main -> venv -> main round-trip, end to end
# ---------------------------------------------------------------------------


def test_golden_linear_round_trip():
    pipeline = {
        'project_id': 'golden',
        'source': 'dropper',
        'components': [
            _node('dropper'),
            _venv('v1', [_node('work', input=[{'lane': 'text', 'from': 'dropper'}])], name='v1'),
            _node('response', input=[{'lane': 'text', 'from': 'work'}]),
        ],
    }

    result = partition_pipeline(pipeline, scoped=True)

    assert list(result.environments) == ['main', 'v1']
    routes = _routes(result)
    assert set(routes) == {'main->v1', 'v1->main'}
    fwd = routes['main->v1']
    ret = routes['v1->main']

    # main: dropper + response + ONE round-trip node (the forward egress that also delivers
    # the return). response reads that node; the return routing entry delivers through it.
    main = result.environments['main']
    assert {'dropper', 'response'}.issubset(set(_ids(main)))
    main_clients = [c for c in main['components'] if c['provider'] == 'venv']
    assert len(main_clients) == 1
    node = main_clients[0]
    assert node['id'] == fwd['egressNode']
    assert node['config']['returnChannelId'] == 'v1->main'
    response_out = next(c for c in main['components'] if c['id'] == 'response')
    assert response_out['input'] == [{'lane': 'text', 'from': fwd['egressNode']}]
    assert ret['ingressNode'] == fwd['egressNode']

    # v1: stub + forward ingress (from stub) + return egress (from work) + work.
    v1 = result.environments['v1']
    assert v1['source'] == 'venv_source_stub'
    assert sum(1 for c in v1['components'] if c['provider'] == 'venv_server') == 2
    work_out = next(c for c in v1['components'] if c['id'] == 'work')
    assert work_out['input'] == [{'lane': 'text', 'from': fwd['ingressNode']}]
    egress = next(c for c in v1['components'] if c['id'] == ret['egressNode'])
    assert egress['input'] == [{'lane': 'text', 'from': 'work'}]
