"""
Unit tests for ``ai.modules.task.pipeline.partition_pipeline``.

The canvas nests a container's members under ``config.pipeline.components``, but
the engine reads only the top-level ``components`` list. Without this pass every
grouped component is silently dropped from the run — the failure mode these tests
exist to prevent, since a pipeline that quietly loses nodes still "succeeds".
"""

import pytest

from ai.modules.task.pipeline import is_container, is_isolated, partition_pipeline


# ---------------------------------------------------------------------------
# Helpers
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


def _ids(pipeline):
    return [component['id'] for component in pipeline['components']]


# ---------------------------------------------------------------------------
# Recognising containers
# ---------------------------------------------------------------------------


def test_container_recognised_by_members_or_environment():
    assert is_container(_container('g', members=[_node('a')])) is True
    assert is_container(_container('venv', environment={'name': 'v', 'isolated': True})) is True
    assert is_container(_node('a')) is False


def test_isolation_requires_the_flag():
    assert is_isolated(_container('venv', environment={'name': 'v', 'isolated': True})) is True
    assert is_isolated(_container('venv', environment={'name': 'v', 'isolated': False})) is False
    assert is_isolated(_container('g', members=[_node('a')])) is False


# ---------------------------------------------------------------------------
# Flattening
# ---------------------------------------------------------------------------


def test_pipeline_without_containers_is_returned_unchanged():
    pipeline = {'components': [_node('a'), _node('b')]}
    assert partition_pipeline(pipeline) is pipeline


def test_group_members_are_lifted_in_the_container_s_place():
    pipeline = {'components': [_node('before'), _container('g', members=[_node('inner')]), _node('after')]}

    assert _ids(partition_pipeline(pipeline)) == ['before', 'inner', 'after']


def test_the_container_itself_does_not_survive():
    pipeline = {'components': [_container('g', members=[_node('inner')])]}

    assert 'g' not in _ids(partition_pipeline(pipeline))


def test_nesting_is_flattened_to_one_level():
    pipeline = {'components': [_container('outer', members=[_node('a'), _container('inner', members=[_node('b')])])]}

    assert _ids(partition_pipeline(pipeline)) == ['a', 'b']


def test_an_empty_container_simply_disappears():
    pipeline = {'components': [_node('a'), _container('venv', environment={'name': 'v', 'isolated': True})]}

    assert _ids(partition_pipeline(pipeline)) == ['a']


def test_members_keep_their_connections_across_the_boundary():
    # Membership carries no runtime meaning: ids do not change, so an edge that
    # crossed the container needs no rewriting.
    member = _node('inner', input=[{'lane': 'text', 'from': 'outside'}])
    pipeline = {
        'components': [
            _node('outside'),
            _container('venv', members=[member], environment={'name': 'v', 'isolated': True}),
        ]
    }

    flat = partition_pipeline(pipeline)

    assert flat['components'][1]['input'] == [{'lane': 'text', 'from': 'outside'}]


def test_the_input_pipeline_is_not_modified():
    pipeline = {'components': [_container('g', members=[_node('inner')])]}

    partition_pipeline(pipeline)

    assert _ids(pipeline) == ['g'], 'the caller keeps its own document'


def test_other_pipeline_fields_are_preserved():
    pipeline = {'project_id': 'p1', 'source': 'a', 'components': [_node('a'), _container('g', members=[_node('b')])]}

    flat = partition_pipeline(pipeline)

    assert flat['project_id'] == 'p1'
    assert flat['source'] == 'a'


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_nested_environments_are_rejected():
    inner = _container('venv_inner', members=[_node('a')], environment={'name': 'inner', 'isolated': True})
    pipeline = {
        'components': [_container('venv_outer', members=[inner], environment={'name': 'outer', 'isolated': True})]
    }

    with pytest.raises(ValueError, match='nested'):
        partition_pipeline(pipeline)


def test_a_group_inside_an_environment_is_allowed():
    # Only *isolated* nesting is rejected; an organizational group inside one is
    # just layout and flattens away.
    group = _container('g', members=[_node('a')])
    pipeline = {'components': [_container('venv', members=[group], environment={'name': 'v', 'isolated': True})]}

    assert _ids(partition_pipeline(pipeline)) == ['a']


def test_source_inside_an_environment_is_rejected():
    pipeline = {'components': [_container('venv', members=[_node('src')], environment={'name': 'v', 'isolated': True})]}

    with pytest.raises(ValueError, match='source must stay outside'):
        partition_pipeline(pipeline, source='src')


def test_source_outside_any_environment_is_fine():
    pipeline = {
        'components': [
            _node('src'),
            _container('venv', members=[_node('a')], environment={'name': 'v', 'isolated': True}),
        ]
    }

    assert _ids(partition_pipeline(pipeline, source='src')) == ['src', 'a']


def test_source_inside_a_plain_group_is_fine():
    # A group is layout, not an execution boundary — the source may live in one.
    pipeline = {'components': [_container('g', members=[_node('src')])]}

    assert _ids(partition_pipeline(pipeline, source='src')) == ['src']


def test_invoke_edge_crossing_an_environment_boundary_is_rejected():
    member = _node('agent', control=[{'classType': 'llm', 'from': 'llm_outside'}])
    pipeline = {
        'components': [
            _node('llm_outside'),
            _container('venv', members=[member], environment={'name': 'v', 'isolated': True}),
        ]
    }

    with pytest.raises(ValueError, match='crosses a virtual environment boundary'):
        partition_pipeline(pipeline)


def test_invoke_edge_inside_one_environment_is_fine():
    member = _node('agent', control=[{'classType': 'llm', 'from': 'llm_inside'}])
    pipeline = {
        'components': [
            _container('venv', members=[_node('llm_inside'), member], environment={'name': 'v', 'isolated': True})
        ]
    }

    assert _ids(partition_pipeline(pipeline)) == ['llm_inside', 'agent']


def test_invoke_edge_between_two_environments_is_rejected():
    caller = _node('agent', control=[{'classType': 'llm', 'from': 'llm_a'}])
    pipeline = {
        'components': [
            _container('venv_a', members=[_node('llm_a')], environment={'name': 'a', 'isolated': True}),
            _container('venv_b', members=[caller], environment={'name': 'b', 'isolated': True}),
        ],
    }

    with pytest.raises(ValueError, match='crosses a virtual environment boundary'):
        partition_pipeline(pipeline)


def test_connecting_to_a_container_is_rejected():
    # A container has no lanes; an edge from one would dangle the moment it is
    # flattened away, so say so instead of producing a broken graph.
    pipeline = {
        'components': [_container('g', members=[_node('a')]), _node('b', input=[{'lane': 'text', 'from': 'g'}])]
    }

    with pytest.raises(ValueError, match='produces no data'):
        partition_pipeline(pipeline)
