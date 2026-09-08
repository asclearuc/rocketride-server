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


# ---------------------------------------------------------------------------
# Forced Python requirements: the partitioner's refusals (§4.7.1, 2C-FR step 3)
# ---------------------------------------------------------------------------


def _forced_env(text, isolated=True, name='ocr'):
    return {'name': name, 'isolated': isolated, 'forced': text}


def _forced_pipeline(text, isolated=True):
    return {
        'components': [
            _node('src', config={'mode': 'Source'}),
            _container('venv', members=[_node('a')], environment=_forced_env(text, isolated)),
        ]
    }


def test_forced_on_an_un_isolated_container_is_refused_by_name():
    """The one-click trap: a working container, isolation un-ticked to debug in one process.

    Refusing is the backstop for hand-authored documents and for an older editor; the panel
    greys the field out. Silently ignoring the text would be the exact defect the field exists
    to avoid -- something the user filled in that changes nothing and says nothing.
    """
    with pytest.raises(ValueError) as excinfo:
        partition_pipeline(_forced_pipeline('tabulate==0.9.0\n', isolated=False), 'src', scoped=True)
    message = str(excinfo.value)
    assert 'ocr' in message, 'name the container, so the canvas can point at it'
    assert 'isolated' in message


def test_forced_under_an_unscoped_run_is_refused_rather_than_flattened_away():
    # `scoped` is the fact to test, not the mode behind it: it is already False under the off
    # switch AND under auto when nothing is isolated, so reading the mode would be one branch
    # narrower than the truth.
    with pytest.raises(ValueError) as excinfo:
        partition_pipeline(_forced_pipeline('tabulate==0.9.0\n'), 'src', scoped=False)
    message = str(excinfo.value)
    assert 'ocr' in message
    assert 'not scoped' in message


def test_isolation_is_reported_before_scoping_because_it_is_the_actionable_one():
    # Under `auto` an un-isolated lone container produces both conditions at once. "Tick the box"
    # is something the author can act on; "this run is not scoped" sends them to a server switch.
    with pytest.raises(ValueError) as excinfo:
        partition_pipeline(_forced_pipeline('tabulate==0.9.0\n', isolated=False), 'src', scoped=False)
    assert 'isolated' in str(excinfo.value)


@pytest.mark.parametrize(
    'line',
    [
        '-r other.txt',
        '-c constraints.txt',
        '-e .',
        '--index-url https://evil.example/simple',
        '--extra-index-url https://evil.example/simple',
        './local/pkg-1.0-py3-none-any.whl',
        'C:/tmp/pkg.whl',
        'tabulate==',
    ],
)
def test_inadmissible_forced_lines_are_refused_with_the_line_in_the_message(line):
    """The security boundary. ``-r`` is a file-read primitive over a tenant-supplied string.

    An index flag is worse than it looks: the compile runs ``--index-strategy
    unsafe-best-match``, so uv takes the best version from *every* index, and
    ``--emit-index-url`` writes the index into ``constraints.txt`` where later installs read it.
    """
    with pytest.raises(ValueError) as excinfo:
        partition_pipeline(_forced_pipeline(line + '\n'), 'src', scoped=True)
    message = str(excinfo.value)
    assert line in message, 'carry the offending line, not just the container'
    assert 'ocr' in message


def test_a_direct_url_reference_is_refused_even_though_pep_508_accepts_it():
    # The one shape a real PEP 508 parser accepts and this field must not: `pkg @ url` names a
    # distribution to fetch from anywhere, which is "forced never adds" in reverse.
    with pytest.raises(ValueError) as excinfo:
        partition_pipeline(_forced_pipeline('pkg @ https://example.com/x.tar.gz\n'), 'src', scoped=True)
    assert 'URL' in str(excinfo.value)


@pytest.mark.parametrize(
    'text',
    [
        'numpy\n',
        'tabulate==0.9.0\n',
        'torch[cuda]>=2.1,<3.0\n',
        'torch==2.10.0+cu128 ; sys_platform != "darwin"\n',
        'opencv_contrib_python ~= 4.10\n',
        '# just a comment\n\n  \n',
        'tabulate==0.9.0  # trailing comment\n',
        'torch==2.10.0 ; sys_platform == "darwin"\ntorch==2.10.0+cu128 ; sys_platform != "darwin"\n',
    ],
)
def test_admissible_forced_text_passes(text):
    # Every row of §4.7.1's table has to survive the gate, the marker-scoped pair included --
    # a gate that refused row 5 would refuse the shape the requester asked for by name.
    result = partition_pipeline(_forced_pipeline(text), 'src', scoped=True)
    assert result is not None


def test_a_container_without_forced_text_is_untouched():
    # Nothing carries forced text until someone types it, so the gate must be invisible today.
    plain = {
        'components': [
            _node('src', config={'mode': 'Source'}),
            _container('venv', members=[_node('a')], environment={'name': 'ocr', 'isolated': True}),
        ]
    }
    assert partition_pipeline(plain, 'src', scoped=True) is not None
    assert partition_pipeline(plain, 'src', scoped=False) is not None


def test_blank_forced_text_is_not_forced_text():
    # An emptied box round-trips through the document as '' or whitespace, and must not refuse a
    # run that has nothing to apply.
    for blank in ('', '   ', '\n\n'):
        assert partition_pipeline(_forced_pipeline(blank, isolated=False), 'src', scoped=False) is not None
