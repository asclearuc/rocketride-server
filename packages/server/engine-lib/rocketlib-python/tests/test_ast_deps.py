# =============================================================================
# MIT License — Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""Tests for ``ast_deps`` — provider resolution and AST dependency discovery.

Tests needing the real node/ai source tree are skipped when it is not reachable.
"""

from __future__ import annotations

import ast
import json
import os
import re

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
        # detect imports its model submodule by full path and was never at risk across families --
        # it is the control there. audio_transcribe is one of the four barrel importers Option A
        # converted, so it is where a mis-scoped ancestor rule would re-admit ai.common.models'
        # eager barrel and pull every family back in.
        #
        # The WITHIN-family names (pose for detect, kokoro for audio_transcribe, detection for
        # embedding_image) were impossible to assert before the self-describing rule: blanket
        # co-location handed every walked file its whole directory, so a vision node got rtmlib
        # and an audio node got a TTS engine.
        (
            'detect',
            {'requirements_whisper.txt', 'requirements_gliner.txt', 'requirements_pose.txt'},
        ),
        (
            'audio_transcribe',
            {
                'requirements_gliner.txt',
                'requirements_easyocr.txt',
                'requirements_surya.txt',
                'requirements_detection.txt',
                'requirements_pose.txt',
                'requirements_kokoro.txt',
            },
        ),
        (
            'embedding_image',
            {'requirements_detection.txt', 'requirements_pose.txt', 'requirements_segmentation.txt'},
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


# --- self-describing directories --------------------------------------------
# A directory in which some walked file declares a `_REQUIREMENTS_FILE` resolving to a file that
# EXISTS is self-describing: there, only the declared files are collected. Everywhere else the
# blanket co-location stands, which is what a walked file used to get unconditionally -- and why
# a vision node came away with rtmlib and an audio node with a TTS engine.

# import top -> distribution name, for the handful the tree spells differently. Everything else
# is handled by casefolding and `-`/`_` normalisation (that is what makes faster_whisper match
# faster-whisper), so this table stays short on purpose.
_DIST_ALIASES = {'pil': 'pillow', 'surya': 'surya_ocr', 'doctr': 'python_doctr'}


def _dist_names(req_file):
    """Distribution names a requirement file declares, normalised for comparison."""
    out = set()
    with open(req_file, encoding='utf-8') as fh:
        for line in fh:
            line = line.split('#')[0].split(';')[0].strip()  # comment, then environment marker
            if not line or line.startswith('-'):
                continue
            name = re.split(r'[<>=!~\[\s]', line, maxsplit=1)[0].strip()
            if name:
                out.add(name.lower().replace('-', '_'))
    return out


def _as_dist(import_top):
    top = import_top.lower().replace('-', '_')
    return _DIST_ALIASES.get(top, top)


def _baseline_dists():
    """Packages every environment carries anyway: the files on the path from each root down.

    Nothing in here can go missing from an environment, so a module needing one of them is never
    under-included no matter which sibling happens to name it too -- numpy is the live case, sitting
    in nodes/requirements.txt and also in requirements_whisper.txt.
    """
    out = set()
    for path in (
        os.path.join(_AI_SRC, 'ai', 'requirements.txt'),
        os.path.join(_AI_SRC, 'ai', 'common', 'requirements.txt'),
        os.path.join(_AI_SRC, 'ai', 'web', 'requirements.txt'),
        os.path.join(_NODES_SRC, 'nodes', 'requirements.txt'),
    ):
        if os.path.isfile(path):
            out |= _dist_names(path)
    return out


def _declared_and_imported(path, directory):
    """(_REQUIREMENTS_FILE basenames that exist, third-party tops, same-directory modules)."""
    declared, third, siblings = set(), set(), set()
    try:
        tree = ast.parse(open(path, encoding='utf-8').read())
    except (OSError, SyntaxError, ValueError):
        return declared, third, siblings
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == '_REQUIREMENTS_FILE':
                    for base in A._requirement_basenames(node.value):
                        if os.path.isfile(os.path.join(directory, base)):
                            declared.add(base)
        if isinstance(node, ast.Import):
            for alias in node.names:
                third.add(alias.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            (siblings if node.level == 1 else third).add(node.module.split('.')[0])
    return declared, third, siblings


def test_requirement_basenames_reads_the_shapes_the_tree_actually_uses():
    def names(src):
        return A._requirement_basenames(ast.parse(src).body[0].value)

    joined = "os.path.join(os.path.dirname(__file__), 'requirements_x.txt')"
    assert names(joined) == ['requirements_x.txt']
    assert names(f"[{joined}, os.path.join(d, 'requirements_y.txt')]") == [
        'requirements_x.txt',
        'requirements_y.txt',
    ]
    assert names("os.path.dirname(f) + '/requirements.txt'") == ['requirements.txt']
    assert names("'requirements_plain.txt'") == ['requirements_plain.txt']
    # `= None` has to resolve to nothing: base.py declares it that way, and a subclass doing the
    # same in a requirements-bearing directory would otherwise empty it.
    assert names('None') == []
    assert names("os.path.join(d, 'model.bin')") == []


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


_DECLARES = 'import os\n\n\nclass L:\n    _REQUIREMENTS_FILE = os.path.join(os.path.dirname(__file__), {0!r})\n'


def _self_describing_tree(tmp_path):
    """Three directories, one per behaviour the rule has to show. Returns the local root.

    Self-describing is a property of a directory given the seed set, so the first
    decline-to-fire case is `fam/` walked from a narrower seed; the other two differ in what the
    directory CONTAINS and need their own.
    """
    root = tmp_path / 'root'
    base = root / 'local_nodes'
    _write(base / '__init__.py', '')

    # fam/ -- two declarers, one non-declaring helper, and a stray nobody names
    _write(base / 'fam' / '__init__.py', '')
    _write(base / 'fam' / 'alpha.py', _DECLARES.format('requirements_alpha.txt'))
    _write(base / 'fam' / 'beta.py', _DECLARES.format('requirements_beta.txt'))
    _write(base / 'fam' / 'helper.py', 'X = 1\n')
    _write(base / 'fam' / 'requirements_alpha.txt', 'alpha\n')
    _write(base / 'fam' / 'requirements_beta.txt', 'beta\n')
    _write(base / 'fam' / 'requirements_extra.txt', 'extra\n')

    # typo/ -- the only declarer names a file that is not there
    _write(base / 'typo' / '__init__.py', '')
    _write(base / 'typo' / 'mod.py', _DECLARES.format('requirements_missing.txt'))
    _write(base / 'typo' / 'requirements_real.txt', 'real\n')

    # loose/ -- a lowercase `requirements` local, which is what the legacy substring branch matches
    _write(base / 'loose' / '__init__.py', '')
    _write(
        base / 'loose' / 'mod.py',
        "import os\n\nrequirements = os.path.dirname(__file__) + '/requirements.txt'\n",
    )
    _write(base / 'loose' / 'requirements.txt', 'named\n')
    _write(base / 'loose' / 'requirements_other.txt', 'unnamed\n')
    return root


def test_a_self_describing_directory_yields_only_declared_files(tmp_path):
    root = _self_describing_tree(tmp_path)
    roots = {'local_nodes': str(root)}
    local = root / 'local_nodes'

    def collected(*seeds):
        res = A.discover([str(s) for s in seeds], roots)
        return {os.path.basename(p) for p in res.requirement_files}

    # The rule firing: the stray nobody declares stays out, and one declarer alone takes only its
    # own -- which is the whole point, `detect` not inheriting `pose`.
    assert collected(local / 'fam' / 'alpha.py', local / 'fam' / 'beta.py') == {
        'requirements_alpha.txt',
        'requirements_beta.txt',
    }
    assert collected(local / 'fam' / 'alpha.py') == {'requirements_alpha.txt'}

    # Three ways it must DECLINE to fire; together they are its entire safety margin, because
    # every one of them leaves the directory over-including rather than under-including.
    #
    # 1. no declarer walked -> the same directory blanket-globs, stray included
    assert collected(local / 'fam' / 'helper.py') == {
        'requirements_alpha.txt',
        'requirements_beta.txt',
        'requirements_extra.txt',
    }
    # 2. the only declaration resolves to nothing -> globbed, not emptied. Keyed on the assignment
    #    instead of the resolved path this would be set(), turning a typo into a silent under-install.
    assert collected(local / 'typo' / 'mod.py') == {'requirements_real.txt'}
    # 3. a lowercase `requirements` local is not the convention -> globbed. Were the legacy
    #    substring branch taught to resolve BinOp, this would be {'requirements.txt'} alone.
    assert collected(local / 'loose' / 'mod.py') == {'requirements.txt', 'requirements_other.txt'}


@_needs_tree
def test_an_ocr_engine_module_scopes_to_its_own_requirements():
    """The precondition 2A-4 rests on, and the reason this item is not cosmetic.

    A 2A-4 Surya component would seed its walk here. Today an `ocr` environment gets all three
    engine files, so the resolver holds opencv at 4.13 and backtracks surya-ocr to 0.16.1 while
    surya.py targets the 0.17 API: splitting the node buys nothing until the walk stops handing
    the environment every engine.
    """
    roots = {'nodes': _NODES_SRC, 'ai': _AI_SRC}
    seed = os.path.join(_AI_SRC, 'ai', 'common', 'models', 'ocr', 'surya.py')
    names = {os.path.basename(p) for p in A.discover([seed], roots).requirement_files}
    assert 'requirements_surya.txt' in names
    assert not names & {
        'requirements_easyocr.txt',
        'requirements_doctr.txt',
    }


@_needs_tree
def test_a_node_imports_a_model_module_never_a_model_package():
    """Stated as package-vs-module rather than a list of banned family names.

    One assertion then covers both `from ai.common.models import EasyOCR` (the whole model
    universe, which Option A removed) and `from ai.common.models.ocr import Surya` (the family),
    while leaving `ai.common.models.base` alone -- a module, carrying no requirement file.
    """
    roots = {'nodes': _NODES_SRC, 'ai': _AI_SRC}
    offenders = []
    for dirpath, _dirs, files in os.walk(os.path.join(_NODES_SRC, 'nodes')):
        if '__pycache__' in dirpath:
            continue
        for name in sorted(f for f in files if f.endswith('.py')):
            path = os.path.join(dirpath, name)
            try:
                tree = ast.parse(open(path, encoding='utf-8').read())
            except (OSError, SyntaxError, ValueError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                for mod in A._import_targets(node, path, roots):
                    if mod != 'ai.common.models' and not mod.startswith('ai.common.models.'):
                        continue
                    resolved = A._module_to_file(mod, roots)
                    if resolved and os.path.basename(resolved) == '__init__.py':
                        rel = os.path.relpath(path, _REPO).replace(os.sep, '/')
                        offenders.append(f'{rel} imports {mod}')
    assert not offenders, (
        'these node files import a model PACKAGE, not a model module: '
        + '; '.join(offenders)
        + '. Importing a package runs its __init__, which re-exports every module beneath it, and '
        'each of those declares its own requirements into this node environment. Import the '
        'specific submodule instead.'
    )


@_needs_tree
def test_a_declaring_module_names_every_sibling_it_needs():
    """The rule makes `_REQUIREMENTS_FILE` load-bearing, so an incomplete declaration stops being
    harmless: what the module needs from an unnamed sibling silently leaves the environment.

    This is the check that found Pillow declared only in requirements_trocr.txt while three of the
    four engines import PIL themselves. Scoped to SIBLINGS on purpose -- torch legitimately
    arrives from ai/common/torch/ and numpy from the tree baseline, so asserting over every
    third-party top would fail on every loader. A declared file that does not exist contributes no
    coverage, which is what also makes this the guard for a typo beside a valid declarer.
    """
    problems = []
    baseline = _baseline_dists()
    for dirpath, _dirs, files in os.walk(os.path.join(_AI_SRC, 'ai', 'common', 'models')):
        if '__pycache__' in dirpath:
            continue
        req_files = {f for f in files if f.startswith('requirement') and f.endswith('.txt')}
        if not req_files:
            continue
        modules = {
            name: _declared_and_imported(os.path.join(dirpath, name), dirpath)
            for name in sorted(f for f in files if f.endswith('.py'))
        }
        for name, (declared, third, siblings) in modules.items():
            if not declared:
                continue
            # Fold in a non-declaring same-directory helper's imports: its coverage rides entirely
            # on whoever imports it. Empty class today, and cheap while we are here.
            needed = set(third)
            for sib in siblings:
                helper = modules.get(f'{sib}.py')
                if helper and not helper[0]:
                    needed |= helper[1]
            covered = set().union(*(_dist_names(os.path.join(dirpath, b)) for b in declared))
            covered |= baseline  # the floor is in every environment, whoever else names it
            unnamed = set()
            for base in req_files - declared:
                unnamed |= _dist_names(os.path.join(dirpath, base))
            for top in sorted(needed):
                dist = _as_dist(top)
                if dist in unnamed and dist not in covered:
                    rel = os.path.relpath(os.path.join(dirpath, name), _REPO).replace(os.sep, '/')
                    problems.append(f'{rel} imports {top}, declared only in a sibling it does not name')
    assert not problems, '; '.join(problems)


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


@_needs_tree
def test_a_harvested_ancestor_never_hides_an_uncovered_package():
    """The residual item 1 handed over, closed by measurement rather than by hope.

    Ancestors are harvested and never walked, so an ancestor ``__init__`` that imports a FOREIGN
    first-party subtree keeps that subtree's requirement files out of the compile --
    ``ai/web/__init__`` does exactly that with ``from ai.account import AccountInfo``. It is not a
    hole only because everything such an import would add is already in the tree baseline every
    environment carries. That is a property of the tree, so it is asserted, not asserted-in-prose:
    the day one of these brings an uncovered package, harvest-only has become an under-inclusion
    and the fix is to queue ancestors behind a genuinely lazy barrel.
    """
    roots = {'nodes': _NODES_SRC, 'ai': _AI_SRC}
    baseline = _baseline_dists()
    uncovered = set()
    for base, top in ((_AI_SRC, 'ai'), (_NODES_SRC, 'nodes')):
        for dirpath, _dirs, files in os.walk(os.path.join(base, top)):
            if '__pycache__' in dirpath or '__init__.py' not in files:
                continue
            init = os.path.join(dirpath, '__init__.py')
            own = A._pkg_of(init, roots)
            try:
                tree = ast.parse(open(init, encoding='utf-8').read())
            except (OSError, SyntaxError, ValueError):
                continue
            for node in tree.body:  # module level only -- that is what the import machinery runs
                if isinstance(node, ast.ImportFrom) and node.level:
                    continue  # relative, so inside its own subtree and harvested anyway
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                for mod in A._import_targets(node, init, roots):
                    if mod.split('.', 1)[0] not in roots:
                        continue
                    if not own or mod == own or mod.startswith(own + '.'):
                        continue
                    target = A._module_to_file(mod, roots)
                    if not target:
                        continue
                    for directory in A._package_dirs_to(target, roots) + [os.path.dirname(target)]:
                        for name in sorted(os.listdir(directory)) if os.path.isdir(directory) else []:
                            if not (name.startswith('requirement') and name.endswith('.txt')):
                                continue
                            extra = _dist_names(os.path.join(directory, name)) - baseline
                            if extra:
                                uncovered.add(f'{own} -> {mod}: {sorted(extra)}')

    assert not uncovered, (
        'a harvested ancestor imports a foreign first-party subtree whose packages are not in the '
        'tree baseline, so nothing puts them in the compile: ' + '; '.join(sorted(uncovered))
    )


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
