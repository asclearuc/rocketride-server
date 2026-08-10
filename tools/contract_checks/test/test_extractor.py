"""
Self-tests for the AST extractor.

These tests construct tiny .py files in a tmp dir and verify the extractor
produces the expected :class:`ComponentContract`. They don't require the
engine env.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from contract_checks.extractor import component_id, extract_component, iter_components
from contract_checks.trees import Tree


# All self-tests use the same minimal tree config so we can focus on the
# extractor's behavior, not the trees.py config.
def _make_tree(root: Path, internal: frozenset[str] = frozenset({'rocketlib', 'engLib'})) -> Tree:
    """
    Build a throwaway :class:`Tree` rooted at ``root`` for a single self-test.

    Args:
        root:     Directory holding the fixture .py files for this test.
        internal: Set of package names to treat as internal (filtered out
                  of contracts). Default matches the smallest sensible set.

    Returns:
        A :class:`Tree` named ``self-test`` rooted at the tmp dir.
    """
    return Tree(
        name='self-test',
        root=root,
        internal_packages=internal,
    )


def _write(d: Path, name: str, body: str) -> None:
    """
    Write a dedented fixture file under a tmp dir.

    Args:
        d:    Directory the file goes into (typically pytest's ``tmp_path``).
        name: File name (e.g., ``"a.py"``).
        body: File contents; ``textwrap.dedent``'d and stripped of leading
              newline so callers can use triple-quoted indented strings.
    """
    (d / name).write_text(textwrap.dedent(body).lstrip('\n'), encoding='utf-8')


def test_plain_imports_are_picked_up(tmp_path: Path):
    """Top-level ``import`` and ``from … import …`` produce ImportRequirement entries."""
    _write(
        tmp_path,
        'a.py',
        """
        import requests
        from langchain_core.messages import AIMessage, BaseMessage
    """,
    )
    contract = extract_component(_make_tree(tmp_path), tmp_path)
    modules = {req.module: req for req in contract.imports}
    assert 'requests' in modules
    assert 'langchain_core.messages' in modules
    assert set(modules['langchain_core.messages'].symbols) == {'AIMessage', 'BaseMessage'}


def test_stdlib_is_filtered_out(tmp_path: Path):
    """Stdlib names from ``sys.stdlib_module_names`` are dropped from the contract."""
    _write(
        tmp_path,
        'a.py',
        """
        import os
        import sys
        from typing import Optional
        import requests
    """,
    )
    contract = extract_component(_make_tree(tmp_path), tmp_path)
    modules = {req.module for req in contract.imports}
    assert 'os' not in modules
    assert 'sys' not in modules
    assert 'typing' not in modules
    assert 'requests' in modules


def test_internal_packages_are_filtered(tmp_path: Path):
    """Names in the tree's ``internal_packages`` set are dropped from the contract."""
    _write(
        tmp_path,
        'a.py',
        """
        from rocketlib import debug
        from engLib import Filters
        import requests
    """,
    )
    contract = extract_component(_make_tree(tmp_path), tmp_path)
    modules = {req.module for req in contract.imports}
    assert 'rocketlib' not in modules
    assert 'engLib' not in modules
    assert 'requests' in modules


def test_relative_imports_are_skipped(tmp_path: Path):
    """``from . import …`` and ``from .. import …`` contribute nothing to the contract."""
    _write(
        tmp_path,
        'a.py',
        """
        from .sibling import helper
        from ..parent import utility
        import requests
    """,
    )
    contract = extract_component(_make_tree(tmp_path), tmp_path)
    modules = {req.module for req in contract.imports}
    assert 'requests' in modules
    # Relative imports contribute no third-party requirement.
    assert all(not m.startswith('.') for m in modules)


def test_try_except_importerror_creates_any_of(tmp_path: Path):
    """A ``try / except ImportError`` shim is rerouted to an AnyOf group automatically."""
    _write(
        tmp_path,
        'a.py',
        """
        try:
            from img2table.ocr._types import OCRInstance
        except ImportError:
            from img2table.ocr.base import OCRInstance
    """,
    )
    contract = extract_component(_make_tree(tmp_path), tmp_path)
    # No required import from the try-block — it's routed to any_of.
    modules = {req.module for req in contract.imports}
    assert 'img2table.ocr._types' not in modules
    assert 'img2table.ocr.base' not in modules
    # An any_of group with both alternatives is emitted.
    assert len(contract.any_of_imports) == 1
    alts = {alt.module for alt in contract.any_of_imports[0].alternatives}
    assert alts == {'img2table.ocr._types', 'img2table.ocr.base'}


def test_non_import_guard_handler_excluded_from_any_of(tmp_path: Path):
    """A handler catching an unrelated exception contributes no AnyOf alternatives."""
    _write(
        tmp_path,
        'a.py',
        """
        try:
            import fast_json
        except ImportError:
            import slow_json
        except ValueError:
            import unrelated_pkg
    """,
    )
    contract = extract_component(_make_tree(tmp_path), tmp_path)
    assert len(contract.any_of_imports) == 1
    alts = {alt.module for alt in contract.any_of_imports[0].alternatives}
    # The ImportError handler's import is a real fallback; the ValueError
    # handler's import is not and must be excluded.
    assert alts == {'fast_json', 'slow_json'}
    assert 'unrelated_pkg' not in alts


def test_function_body_imports_are_seen(tmp_path: Path):
    """Imports nested inside a function body still register as contract entries."""
    _write(
        tmp_path,
        'a.py',
        """
        def f():
            from twelvelabs import TwelveLabs
            return TwelveLabs(api_key="x")
    """,
    )
    contract = extract_component(_make_tree(tmp_path), tmp_path)
    modules = {req.module for req in contract.imports}
    assert 'twelvelabs' in modules


def test_intra_procedural_heavy_class_detected(tmp_path: Path):
    """``client = SDK(...); client.foo(...)`` auto-emits a HeavyClass with dummied args."""
    _write(
        tmp_path,
        'a.py',
        """
        from twelvelabs import TwelveLabs

        def run():
            api_key = "k"
            client = TwelveLabs(api_key=api_key)
            client.indexes.create("foo")
            client.tasks.create("bar")
    """,
    )
    contract = extract_component(_make_tree(tmp_path), tmp_path)
    hcs = [hc for hc in contract.heavy_classes if hc.qualname == 'twelvelabs.TwelveLabs']
    assert len(hcs) == 1, contract.heavy_classes
    hc = hcs[0]
    assert hc.construct == 'TwelveLabs(api_key="x")'
    assert 'indexes.create' in hc.attr_chains
    assert 'tasks.create' in hc.attr_chains


def test_contract_check_ignore_comment_drops_import(tmp_path: Path):
    """An inline ``# contract-check: ignore`` comment drops that import from the contract."""
    _write(
        tmp_path,
        'a.py',
        """
        import requests  # contract-check: ignore  loaded only via plugin
        import urllib3
    """,
    )
    contract = extract_component(_make_tree(tmp_path), tmp_path)
    modules = {req.module for req in contract.imports}
    assert 'requests' not in modules
    assert 'urllib3' in modules


def test_manifest_skip_drops_auto_detected_package(tmp_path: Path):
    """``ComponentManifest.skip_packages`` removes auto-extracted entries for that package."""
    _write(
        tmp_path,
        'a.py',
        """
        import requests
        import urllib3
    """,
    )
    _write(
        tmp_path,
        'external_contracts.py',
        """
        from contract_checks.manifest import ComponentManifest

        MANIFEST = ComponentManifest(
            skip_packages=frozenset({"requests"}),
        )
    """,
    )
    contract = extract_component(_make_tree(tmp_path), tmp_path)
    modules = {req.module for req in contract.imports}
    assert 'requests' not in modules
    assert 'urllib3' in modules


def test_skip_packages_strips_alternative_from_mixed_any_of(tmp_path: Path):
    """skip_packages removes only the skip-listed alternative; the group survives."""
    _write(
        tmp_path,
        'a.py',
        """
        try:
            from crypto_a.backend import Cipher
        except ImportError:
            from crypto_b.backend import Cipher
    """,
    )
    _write(
        tmp_path,
        'external_contracts.py',
        """
        from contract_checks.manifest import ComponentManifest

        MANIFEST = ComponentManifest(
            skip_packages=frozenset({"crypto_b"}),
        )
    """,
    )
    contract = extract_component(_make_tree(tmp_path), tmp_path)
    assert len(contract.any_of_imports) == 1
    alts = {alt.module for alt in contract.any_of_imports[0].alternatives}
    assert alts == {'crypto_a.backend'}


def test_skip_packages_drops_fully_skip_listed_any_of(tmp_path: Path):
    """When every alternative is skip-listed, the whole AnyOf group is dropped."""
    _write(
        tmp_path,
        'a.py',
        """
        try:
            from crypto_a.backend import Cipher
        except ImportError:
            from crypto_b.backend import Cipher
    """,
    )
    _write(
        tmp_path,
        'external_contracts.py',
        """
        from contract_checks.manifest import ComponentManifest

        MANIFEST = ComponentManifest(
            skip_packages=frozenset({"crypto_a", "crypto_b"}),
        )
    """,
    )
    contract = extract_component(_make_tree(tmp_path), tmp_path)
    assert contract.any_of_imports == []


def test_skip_packages_preserves_any_of_applies_when(tmp_path: Path):
    """Stripping a skip-listed alternative keeps the group's applies_when gate."""
    _write(tmp_path, 'a.py', 'pass\n')
    _write(
        tmp_path,
        'external_contracts.py',
        """
        from contract_checks.manifest import AnyOf, ComponentManifest, ImportRequirement

        MANIFEST = ComponentManifest(
            any_of_imports=(
                AnyOf(
                    alternatives=(
                        ImportRequirement(module='keep_pkg.x'),
                        ImportRequirement(module='drop_pkg.x'),
                    ),
                    applies_when='>=1.0',
                ),
            ),
            skip_packages=frozenset({'drop_pkg'}),
        )
    """,
    )
    contract = extract_component(_make_tree(tmp_path), tmp_path)
    assert len(contract.any_of_imports) == 1
    group = contract.any_of_imports[0]
    assert {alt.module for alt in group.alternatives} == {'keep_pkg.x'}
    assert group.applies_when == '>=1.0'


def test_manifest_adds_heavy_class(tmp_path: Path):
    """A manifest-declared HeavyClass appears in the contract even with no AST evidence."""
    _write(
        tmp_path,
        'a.py',
        """
        from somesdk import Client
        # No instantiation in source, so no auto-extracted heavy class.
    """,
    )
    _write(
        tmp_path,
        'external_contracts.py',
        """
        from contract_checks.manifest import ComponentManifest, HeavyClass

        MANIFEST = ComponentManifest(
            heavy_classes=(
                HeavyClass(
                    qualname='somesdk.Client',
                    construct='Client(token="x")',
                    attr_chains=('users.list', 'projects.get'),
                ),
            ),
        )
    """,
    )
    contract = extract_component(_make_tree(tmp_path), tmp_path)
    qualnames = {hc.qualname for hc in contract.heavy_classes}
    assert 'somesdk.Client' in qualnames


def test_packages_property_returns_distinct_top_levels(tmp_path: Path):
    """``ComponentContract.packages`` deduplicates to top-level package names only."""
    _write(
        tmp_path,
        'a.py',
        """
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatResult
        import requests
    """,
    )
    contract = extract_component(_make_tree(tmp_path), tmp_path)
    assert set(contract.packages) == {'langchain_core', 'requests'}


# --------------------------------------------------------------------------- #
# Manifest replace-on-match semantics (version-aware manifests)
# --------------------------------------------------------------------------- #


def test_manifest_import_replaces_auto_extracted_with_same_module(tmp_path: Path):
    """A manifest ImportRequirement with the same module wins over the AST entry."""
    _write(
        tmp_path,
        'a.py',
        """
        from img2table.ocr.data import OCRDataframe
    """,
    )
    _write(
        tmp_path,
        'external_contracts.py',
        """
        from contract_checks.manifest import ComponentManifest, ImportRequirement

        MANIFEST = ComponentManifest(
            imports=(
                # Version-gated: applies only on img2table <2.0
                ImportRequirement(
                    module='img2table.ocr.data',
                    symbols=('OCRDataframe',),
                    applies_when='<2.0',
                ),
            ),
        )
    """,
    )
    contract = extract_component(_make_tree(tmp_path), tmp_path)
    # Exactly one entry for img2table.ocr.data — the manifest's version-gated one.
    matches = [r for r in contract.imports if r.module == 'img2table.ocr.data']
    assert len(matches) == 1
    assert matches[0].applies_when == '<2.0'


def test_manifest_heavy_class_replaces_auto_extracted_with_same_qualname(tmp_path: Path):
    """A manifest HeavyClass with the same qualname replaces the AST entry."""
    _write(
        tmp_path,
        'a.py',
        """
        from somesdk import Client

        def f():
            c = Client(api_key="real_key", base_url="real_url")
            c.do_thing()
    """,
    )
    _write(
        tmp_path,
        'external_contracts.py',
        """
        from contract_checks.manifest import ComponentManifest, HeavyClass

        MANIFEST = ComponentManifest(
            heavy_classes=(
                HeavyClass(
                    qualname='somesdk.Client',
                    construct='Client(token="x")',
                    attr_chains=('admin.list_users',),
                ),
            ),
        )
    """,
    )
    contract = extract_component(_make_tree(tmp_path), tmp_path)
    matches = [hc for hc in contract.heavy_classes if hc.qualname == 'somesdk.Client']
    assert len(matches) == 1
    # Manifest entry wins — different construct + different chain.
    assert matches[0].construct == 'Client(token="x")'
    assert matches[0].attr_chains == ('admin.list_users',)


def test_applies_when_field_is_preserved_through_extraction(tmp_path: Path):
    """The applies_when field survives the AST → contract pipeline intact."""
    _write(tmp_path, 'a.py', 'pass\n')
    _write(
        tmp_path,
        'external_contracts.py',
        """
        from contract_checks.manifest import (
            AnyOf, ComponentManifest, HeavyClass, ImportRequirement,
        )

        MANIFEST = ComponentManifest(
            imports=(
                ImportRequirement(module='lib_a', applies_when='<2.0'),
            ),
            any_of_imports=(
                AnyOf(
                    alternatives=(
                        ImportRequirement(module='lib_b.new'),
                        ImportRequirement(module='lib_b.old'),
                    ),
                    applies_when='>=1.0',
                ),
            ),
            heavy_classes=(
                HeavyClass(
                    qualname='lib_c.Client',
                    construct='Client()',
                    attr_chains=('foo',),
                    applies_when='~=3.0',
                ),
            ),
        )
    """,
    )
    contract = extract_component(_make_tree(tmp_path), tmp_path)
    assert contract.imports[0].applies_when == '<2.0'
    assert contract.any_of_imports[0].applies_when == '>=1.0'
    assert contract.heavy_classes[0].applies_when == '~=3.0'


def test_malformed_applies_when_fails_at_manifest_load_time(tmp_path: Path):
    """Bad PEP 440 specs raise at manifest load, not silently treated as always-applies."""
    _write(tmp_path, 'a.py', 'pass\n')
    _write(
        tmp_path,
        'external_contracts.py',
        """
        from contract_checks.manifest import ComponentManifest, ImportRequirement

        MANIFEST = ComponentManifest(
            imports=(
                ImportRequirement(module='lib_a', applies_when='<= not a real spec'),
            ),
        )
    """,
    )
    with pytest.raises(ValueError, match='malformed applies_when'):
        extract_component(_make_tree(tmp_path), tmp_path)


def test_empty_applies_when_string_raises(tmp_path: Path):
    """Empty string is rejected — use None for 'always applies' instead."""
    _write(tmp_path, 'a.py', 'pass\n')
    _write(
        tmp_path,
        'external_contracts.py',
        """
        from contract_checks.manifest import ComponentManifest, ImportRequirement

        MANIFEST = ComponentManifest(
            imports=(
                ImportRequirement(module='lib_a', applies_when='   '),
            ),
        )
    """,
    )
    with pytest.raises(ValueError, match='applies_when must be None'):
        extract_component(_make_tree(tmp_path), tmp_path)


def test_internal_package_does_not_leak_into_heavy_class_detection(tmp_path: Path):
    """
    Regression: ``rocketlib`` is in this tree's `internal_packages`, so even
    if local code does ``from rocketlib import X; X(...).method()``, the
    framework must NOT emit a HeavyClass for ``rocketlib.X``. Previously the
    import was filtered out of ``self.imports`` but still landed in the
    visitor's alias map, leaking through to heavy-class auto-detection.
    """
    _write(
        tmp_path,
        'a.py',
        """
        from rocketlib import getServiceDefinition

        def f():
            cfg = getServiceDefinition("x")
            cfg.get('key')

        # Also test the bare-module form
        import rocketlib

        def g():
            client = rocketlib.SomeClient(host="h")
            client.users.list()
    """,
    )
    tree = _make_tree(tmp_path, internal=frozenset({'rocketlib', 'engLib'}))
    contract = extract_component(tree, tmp_path)
    # No third-party imports were used.
    assert contract.imports == []
    # And critically: no heavy_class was emitted for the internal package.
    assert not any(hc.qualname.startswith('rocketlib') for hc in contract.heavy_classes), contract.heavy_classes


# --------------------------------------------------------------------------- #
# Component discovery
#
# `iter_components` is what decides *what* gets contracted at all, so a rule
# that quietly stops finding a directory removes coverage without turning any
# lane red — the report simply gets shorter. These pin the rule itself.
# --------------------------------------------------------------------------- #


def _mkreq(d: Path, body: str = 'requests\n') -> Path:
    """Create ``d`` and give it a ``requirements.txt``, making it a component."""
    d.mkdir(parents=True, exist_ok=True)
    (d / 'requirements.txt').write_text(body, encoding='utf-8')
    return d


def test_a_nested_requirements_file_yields_a_nested_component(tmp_path: Path):
    """A component may sit inside another directory, not only at depth 1."""
    _mkreq(tmp_path / 'ocr' / 'standard')
    tree = _make_tree(tmp_path)
    found = {component_id(tree, c) for c in iter_components(tree)}
    assert found == {'ocr/standard'}


def test_a_nested_component_is_identified_by_its_tree_relative_path(tmp_path: Path):
    """
    The id is the whole relative path, not the directory name: once nesting is
    legal two nodes can each hold a ``standard/``, and the id feeds
    ``triple_id`` in every report line and every ``--pattern`` match.
    """
    _mkreq(tmp_path / 'ocr' / 'standard')
    _mkreq(tmp_path / 'pdf' / 'standard')
    tree = _make_tree(tmp_path)
    found = {component_id(tree, c) for c in iter_components(tree)}
    assert found == {'ocr/standard', 'pdf/standard'}


def test_a_parents_file_set_excludes_a_nested_components_subtree(tmp_path: Path):
    """
    No .py file may be contracted twice under two ids. The child's files
    belong to the child; the parent keeps only what no child claims.
    """
    parent = _mkreq(tmp_path / 'account')
    child = _mkreq(tmp_path / 'account' / 'oss')
    _write(parent, 'parent_only.py', 'import requests\n')
    _write(child, 'child_only.py', 'import boto3\n')

    tree = _make_tree(tmp_path)
    by_id = {component_id(tree, c): extract_component(tree, c) for c in iter_components(tree)}
    assert set(by_id) == {'account', 'account/oss'}
    assert {r.module for r in by_id['account'].imports} == {'requests'}
    assert {r.module for r in by_id['account/oss'].imports} == {'boto3'}


def test_a_tree_root_carrying_requirements_is_not_itself_a_component(tmp_path: Path):
    """
    Both the ``nodes`` and ``ai`` roots carry a ``requirements.txt``: it is the
    baseline every descendant harvests through the AST walker's ancestor rule,
    not a component boundary. Treating it as one would sweep every unclaimed
    file in the tree into one giant component.
    """
    _mkreq(tmp_path)  # root's own requirements.txt — the baseline
    _mkreq(tmp_path / 'ocr')
    _write(tmp_path, 'stray.py', 'import requests\n')

    tree = _make_tree(tmp_path)
    found = {component_id(tree, c) for c in iter_components(tree)}
    assert found == {'ocr'}


def test_a_tree_with_no_requirements_anywhere_collapses_to_its_root(tmp_path: Path):
    """
    The measured state of both ``rocketlib/lib`` and
    ``client-python/src/rocketride``: no ``requirement*.txt`` at any depth, so
    the whole tree is one component keyed by the root's directory name. This
    is the regression that would be easiest to cause while adding nesting.
    """
    (tmp_path / 'sub').mkdir()
    _write(tmp_path, 'a.py', 'import requests\n')
    _write(tmp_path / 'sub', 'b.py', 'import boto3\n')

    tree = _make_tree(tmp_path)
    components = iter_components(tree)
    assert components == [tmp_path]
    assert component_id(tree, components[0]) == tmp_path.name
    contract = extract_component(tree, components[0])
    assert {r.module for r in contract.imports} == {'requests', 'boto3'}
