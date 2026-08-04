# =============================================================================
# MIT License — Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""Tests for ``ast_deps`` — provider resolution and AST dependency discovery.

Tests needing the real node/ai source tree are skipped when it is not reachable.
"""

from __future__ import annotations

import json
import os

import pytest

import ast_deps as A

# tests/ -> rocketlib-python -> engine-lib -> server -> packages -> repo root
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), *([os.pardir] * 5)))
_NODES_SRC = os.path.join(_REPO, 'nodes', 'src')
_AI_SRC = os.path.join(_REPO, 'packages', 'ai', 'src')
_FIXTURES = os.path.join(_REPO, 'nodes', 'test', 'fixtures')

_HAVE_TREE = os.path.isdir(os.path.join(_NODES_SRC, 'nodes')) and os.path.isdir(os.path.join(_AI_SRC, 'ai'))
_needs_tree = pytest.mark.skipif(not _HAVE_TREE, reason='node/ai source tree not reachable')
_needs_fixtures = pytest.mark.skipif(
    not os.path.isdir(os.path.join(_FIXTURES, 'local_nodes', 'vtest_alpha')), reason='vtest fixtures not present'
)


# --- JSONC parsing ----------------------------------------------------------


def test_strip_jsonc_preserves_scheme_in_strings():
    import json

    src = '{ // a comment\n  "protocol": "webhook://", /* blk */ "path": "nodes.webhook" }'
    data = json.loads(A.strip_jsonc(src))
    assert data['protocol'] == 'webhook://'  # the // inside the string must survive
    assert data['path'] == 'nodes.webhook'


def test_strip_jsonc_trailing_commas():
    import json

    assert json.loads(A.strip_jsonc('{"a": 1, "b": [1, 2,], }')) == {'a': 1, 'b': [1, 2]}


def test_string_consts_flatten():
    import ast

    node = ast.parse("X = ['a.txt', ('b.txt', 'c.txt')]").body[0].value
    assert A._string_consts(node) == ['a.txt', 'b.txt', 'c.txt']


# --- provider -> module resolution ------------------------------------------


@_needs_tree
def test_provider_aliases_share_one_dir():
    idx = A.ProviderIndex(_NODES_SRC)
    for prov in ('webhook', 'chat', 'dropper'):
        entry = idx.resolve(prov)
        assert entry is not None and entry.node_path == 'nodes.webhook'
        assert any(f.endswith('IInstance.py') for f in entry.entry_files)


@_needs_tree
def test_provider_subpackage_path():
    idx = A.ProviderIndex(_NODES_SRC)
    assert idx.resolve('remote').node_path == 'nodes.remote.client'
    assert idx.resolve('remote_server').node_path == 'nodes.remote.server'


@_needs_tree
def test_provider_name_not_equal_dir():
    idx = A.ProviderIndex(_NODES_SRC)
    assert idx.resolve('text-output').node_path == 'nodes.text_output'
    assert idx.resolve('anonymize_text').node_path == 'nodes.anonymize'


@_needs_tree
def test_provider_native_and_unknown():
    idx = A.ProviderIndex(_NODES_SRC)
    for native in ('parse', 'filesys'):
        entry = idx.resolve(native)
        assert entry is not None and entry.native and entry.entry_files == []
    assert idx.resolve('does_not_exist') is None


# --- transitive walk --------------------------------------------------------


def _rel(res):
    """Discovered files as repo-relative forward-slash paths."""
    return {os.path.relpath(p, _REPO).replace(os.sep, '/') for p in res.requirement_files}


@_needs_tree
@pytest.mark.parametrize(
    'provider, must_include',
    [
        ('detect', {'requirements_detection.txt', 'requirements_vision.txt'}),
        ('audio_transcribe', {'requirements_whisper.txt'}),
        ('anonymize_text', {'requirements_gliner.txt'}),
    ],
)
def test_golden_requirement_sets(provider, must_include):
    res = A.discover_for_providers([provider], _NODES_SRC, _AI_SRC)
    assert not res.unresolved_providers
    basenames = {os.path.basename(p) for p in res.requirement_files}
    assert must_include <= basenames, f'{provider} missing {must_include - basenames}'
    # torch is reached through each heavy node's local (non-model-server) branch
    assert any('torch' in os.path.relpath(p, _REPO).replace(os.sep, '/') for p in res.requirement_files)
    assert res.dynamic_imports == []
    # The packages Python executes on the way in. A subset assertion keeps passing when the walk
    # stops finding these, so they are named: the golden set has to cover what the rule adds.
    assert {'nodes/src/nodes/requirements.txt', 'packages/ai/src/ai/requirements.txt'} <= _rel(res)


@_needs_tree
@pytest.mark.parametrize(
    'provider, foreign',
    [
        # detect imports its model submodule by full path and was never at risk -- it is the
        # control. audio_transcribe is one of the four barrel importers Option A converted, so it
        # is where a mis-scoped ancestor rule would re-admit ai.common.models' eager barrel and
        # pull every family back in.
        ('detect', {'requirements_whisper.txt', 'requirements_gliner.txt'}),
        (
            'audio_transcribe',
            {
                'requirements_gliner.txt',
                'requirements_easyocr.txt',
                'requirements_surya.txt',
                'requirements_detection.txt',
                'requirements_pose.txt',
            },
        ),
    ],
)
def test_only_needed_excludes_unrelated_families(provider, foreign):
    res = A.discover_for_providers([provider], _NODES_SRC, _AI_SRC)
    basenames = {os.path.basename(p) for p in res.requirement_files}
    assert not (foreign & basenames), f'{provider} leaked {foreign & basenames}'


@_needs_tree
def test_dynamic_import_is_flagged():
    # preprocessor_code resolves its module from a config-driven dict at runtime
    res = A.discover_for_providers(['preprocessor_code'], _NODES_SRC, _AI_SRC)
    assert res.dynamic_imports


# --- ancestor packages ------------------------------------------------------
# Python runs nodes/__init__.py before any node and ai/__init__.py before any ai module, and each
# installs its own co-located requirements. Those files are nobody's import statement, so a walk
# that only follows imports never opens them.


@_needs_tree
@pytest.mark.parametrize('provider', ['venv', 'venv_server', 'response', 'detect'])
def test_every_nodes_rooted_provider_carries_the_tree_baseline(provider):
    # Guaranteed by construction, not by luck: importing any node runs nodes/__init__.py. Leaf and
    # sub-package entries alike, which is the half the measurement originally missed.
    assert 'nodes/src/nodes/requirements.txt' in _rel(A.discover_for_providers([provider], _NODES_SRC, _AI_SRC))


@_needs_tree
@pytest.mark.parametrize(
    'provider, parent_file',
    [
        ('venv', 'nodes/src/nodes/venv/requirements.txt'),
        ('venv_server', 'nodes/src/nodes/venv/requirements.txt'),
        ('remote_server', 'nodes/src/nodes/remote/requirements.txt'),
    ],
)
def test_sub_package_entry_reaches_its_parent_packages_file(provider, parent_file):
    # These providers' entry modules are sub-packages (nodes.venv.client, ...), so the parent
    # package's file is executed but never imported. Three of them are this feature's own bridges,
    # which is how the hole stayed invisible: they returned an empty set and nothing looked.
    assert parent_file in _rel(A.discover_for_providers([provider], _NODES_SRC, _AI_SRC))


def test_namespace_ancestor_is_skipped_not_stopped(tmp_path):
    # PEP 420: a directory without __init__.py executes nothing, so it contributes no
    # requirements -- but its own ancestors still run and still do. Every directory in the shipped
    # tree has an __init__.py, so only a --node_path tree can tell "skip" from "stop" apart.
    root = tmp_path / 'root'
    pkg = root / 'local_nodes'
    gap = pkg / 'gap'  # namespace package: no __init__.py on purpose
    leaf = gap / 'leaf'
    leaf.mkdir(parents=True)
    (pkg / '__init__.py').write_text('', encoding='utf-8')
    (pkg / 'requirements.txt').write_text('top\n', encoding='utf-8')
    (gap / 'requirements.txt').write_text('namespace\n', encoding='utf-8')
    (leaf / '__init__.py').write_text('', encoding='utf-8')
    (leaf / 'requirements.txt').write_text('leaf\n', encoding='utf-8')

    res = A.discover([str(leaf / '__init__.py')], {'local_nodes': str(root)})
    names = {os.path.basename(os.path.dirname(p)) for p in res.requirement_files}
    assert names == {'leaf', 'local_nodes'}, 'the namespace directory must be skipped, not a stop'


@_needs_tree
def test_the_models_barrel_needs_nothing_beyond_the_baseline_at_import_time():
    """Why ancestors are harvested and not walked, stated as an assertion.

    ``ai/common/models/__init__.py`` eagerly re-exports every family, so queuing it would drag the
    whole model universe into every environment that touches one model -- the precise thing Option
    A removed. Not queuing it is only safe because executing it needs nothing an environment
    lacks: the heavy imports all sit inside functions, behind ``_ensure_dependencies``. That is a
    property of the tree, not a promise, so it is measured. If this fails, harvest-only has become
    an under-inclusion and the barrel has to be made lazy or the rule changed.
    """
    import ast

    roots = {'nodes': _NODES_SRC, 'ai': _AI_SRC}
    start = os.path.join(_AI_SRC, 'ai', 'common', 'models', '__init__.py')
    seen, queue, third = set(), [start], set()
    while queue:
        cur = queue.pop()
        if cur in seen or not os.path.isfile(cur):
            continue
        seen.add(cur)
        try:
            tree = ast.parse(open(cur, encoding='utf-8').read())
        except (OSError, SyntaxError, ValueError):
            continue
        for node in tree.body:  # module level only -- that is what import time executes
            for stmt in ast.walk(node) if isinstance(node, ast.If) else [node]:
                if not isinstance(stmt, (ast.Import, ast.ImportFrom)):
                    continue
                for mod in A._import_targets(stmt, cur, roots):
                    top = mod.split('.', 1)[0]
                    if top in roots:
                        nxt = A._module_to_file(mod, roots)
                        if nxt:
                            queue.append(nxt)
                    elif top and top not in A._NON_REQUIREMENT_TOPS:
                        third.add(top)

    # numpy ships in the tree baseline every environment now carries; wave is stdlib; rocketride is
    # the SDK shipped beside the engine. Anything else would be a package nobody installed.
    assert third <= {'numpy', 'wave', 'rocketride'}, f'barrel needs {third} at import time'


def test_root_matching_prefers_the_longest_base(tmp_path):
    # With --node_path pointing inside the exe dir, `nodes` and `ai` both map to the exe dir; plain
    # iteration order would claim a local_nodes file before its own root was considered, and the
    # walker and _pkg_of would then disagree about which package a file belongs to.
    exe = tmp_path / 'exe'
    local_root = exe / 'workspace'
    pkg = local_root / 'local_nodes' / 'mine'
    pkg.mkdir(parents=True)
    (local_root / 'local_nodes' / '__init__.py').write_text('', encoding='utf-8')
    (pkg / '__init__.py').write_text('', encoding='utf-8')
    roots = {'nodes': str(exe), 'ai': str(exe), 'local_nodes': str(local_root)}

    assert A._root_of(str(pkg / '__init__.py'), roots) == str(local_root)
    assert A._pkg_of(str(pkg / '__init__.py'), roots) == 'local_nodes.mine'


# --- conflict fixture nodes -------------------------------------------------


@_needs_fixtures
def test_fixture_nodes_pin_conflicting_versions_without_ai():
    # The fixtures live under `local_nodes/`, so they resolve through the local root, not
    # `nodes_src` -- which is exactly the arrangement the engine sees under `--node_path=`.
    idx = A.ProviderIndex(_NODES_SRC, local_root=_FIXTURES)
    roots_ai = _AI_SRC if _HAVE_TREE else _FIXTURES
    for prov, pin in (('vtest_alpha', 'tabulate==0.8.10'), ('vtest_beta', 'tabulate==0.9.0')):
        entry = idx.resolve(prov)
        assert entry is not None and not entry.native
        assert entry.node_path == f'local_nodes.{prov}'
        res = A.discover(entry.entry_files, {'local_nodes': _FIXTURES, 'ai': roots_ai})
        contents = ''.join(open(r, encoding='utf-8').read() for r in res.requirement_files)
        assert pin in contents
        assert res.reached_modules == []
        assert 'tabulate' in res.third_party


@_needs_fixtures
def test_discover_for_providers_dedupes_repeated_providers(monkeypatch):
    # a pipeline may hold many nodes of one type; each provider must resolve once
    seen = []
    orig = A.ProviderIndex.resolve

    def counting(self, provider):
        seen.append(provider)
        return orig(self, provider)

    monkeypatch.setattr(A.ProviderIndex, 'resolve', counting)
    res = A.discover_for_providers(
        ['vtest_alpha', 'vtest_alpha', 'vtest_beta', 'vtest_alpha'],
        _NODES_SRC,
        _AI_SRC,
        local_root=_FIXTURES,
    )
    assert seen == ['vtest_alpha', 'vtest_beta']
    assert {os.path.basename(r) for r in res.requirement_files} >= {'requirements.txt'}


# --- the second provider root (--node_path / local_nodes) -------------------


def _make_local_node(tmp_path, name, dotted=None, pin=None):
    """Write a minimal local node under <tmp_path>/local_nodes/<name>/."""
    pkg = tmp_path / 'local_nodes' / name
    pkg.mkdir(parents=True)
    (tmp_path / 'local_nodes' / '__init__.py').write_text('', encoding='utf-8')
    (pkg / '__init__.py').write_text('from .IInstance import IInstance\n', encoding='utf-8')
    (pkg / 'IInstance.py').write_text('class IInstance:\n    pass\n', encoding='utf-8')
    if pin:
        (pkg / 'requirements.txt').write_text(pin + '\n', encoding='utf-8')
    (pkg / 'services.json').write_text(
        json.dumps({'protocol': f'{name}://', 'path': dotted or f'local_nodes.{name}'}),
        encoding='utf-8',
    )
    return pkg


def test_local_root_is_scanned_and_resolved_against_its_own_base(tmp_path):
    _make_local_node(tmp_path, 'probe_node', pin='sixpack==1.2.3')
    idx = A.ProviderIndex(str(tmp_path / 'unused_nodes_src'), local_root=str(tmp_path))

    entry = idx.resolve('probe_node')
    assert entry is not None and not entry.native
    assert entry.node_path == 'local_nodes.probe_node'
    # Resolved under the LOCAL root, not nodes_src -- the dotted path names its own base.
    assert entry.entry_files, 'entry files must resolve under the local root'
    for f in entry.entry_files:
        assert str(tmp_path / 'local_nodes' / 'probe_node') in f


def test_local_root_absent_means_no_local_providers(tmp_path):
    _make_local_node(tmp_path, 'probe_node')
    idx = A.ProviderIndex(str(tmp_path / 'unused_nodes_src'))
    assert idx.resolve('probe_node') is None, 'no local_root -> the provider must be invisible'


def test_builtin_node_wins_a_name_collision(tmp_path):
    """A local node may not shadow a shipped one: built-in roots are scanned first."""
    builtin = tmp_path / 'src' / 'nodes' / 'clash'
    builtin.mkdir(parents=True)
    (builtin / 'services.json').write_text(
        json.dumps({'protocol': 'clash://', 'path': 'nodes.clash'}), encoding='utf-8'
    )
    _make_local_node(tmp_path, 'clash', dotted='local_nodes.clash')

    idx = A.ProviderIndex(str(tmp_path / 'src'), local_root=str(tmp_path))
    assert idx.resolve('clash').node_path == 'nodes.clash'


def test_discover_for_providers_walks_a_local_node(tmp_path):
    _make_local_node(tmp_path, 'probe_node', pin='sixpack==1.2.3')
    res = A.discover_for_providers(
        ['probe_node'], str(tmp_path / 'unused'), str(tmp_path / 'unused'), local_root=str(tmp_path)
    )
    assert res.unresolved_providers == []
    contents = ''.join(open(r, encoding='utf-8').read() for r in res.requirement_files)
    assert 'sixpack==1.2.3' in contents


# --- local_nodes_root(argv) ------------------------------------------------


def test_local_nodes_root_requires_the_local_nodes_directory(tmp_path):
    argv = ['engine.exe', 'ai/node.py', f'--node_path={tmp_path}']
    # The directory does not hold local_nodes/ yet: mirror the C++ condition and refuse.
    assert A.local_nodes_root(argv) is None
    (tmp_path / 'local_nodes').mkdir()
    assert A.local_nodes_root(argv) == str(tmp_path)


def test_local_nodes_root_takes_the_first_flag_and_tolerates_absence(tmp_path):
    (tmp_path / 'local_nodes').mkdir()
    other = tmp_path / 'second'
    (other / 'local_nodes').mkdir(parents=True)
    argv = ['engine.exe', f'--node_path={tmp_path}', f'--node_path={other}']
    assert A.local_nodes_root(argv) == str(tmp_path)
    assert A.local_nodes_root(['engine.exe', '--trace=debugOut']) is None
    assert A.local_nodes_root([]) is None
