# =============================================================================
# MIT License
# Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""
Invariants of the two OCR services files.

Most of what the component split gets right here is **declaration-only** and no
Python touches it: a component advertising a ``table`` lane it cannot serve, or
shipping without the ``title`` the docs generator needs, fails at deploy or at
runtime, never in a suite. Measured, nothing else in the tree validates a
services file's key set either.

So the key set is asserted as a **set**, not only per value -- that is what
catches an omission on a hand-authored file (``classType``, ``description`` and
``documentation`` are present in every sibling services file in the tree;
``tile``, ``test`` and ``fulltest`` in none).

``json5`` rather than ``json``: these files are JSONC, with ``//`` comments
*and* a ``//`` inside every protocol value, so a naive comment-stripper corrupts
the one field several assertions below turn on. ``ai.common.config`` parses them
the same way.

Usage:
    ./builder.cmd nodes:test --pytest-pattern=ocr --verbose
"""

from pathlib import Path

import json5
import pytest

_NODE_DIR = Path(__file__).resolve().parents[2] / 'src' / 'nodes' / 'ocr'
_STANDARD = json5.loads((_NODE_DIR / 'services.json').read_text(encoding='utf-8'))
_SURYA = json5.loads((_NODE_DIR / 'services.surya.json').read_text(encoding='utf-8'))

# The eleven keys that open every precedent sibling file in the tree, plus
# `lanes` and `fields`. No `preconfig`/`shape`: a component with no
# configuration has nothing for them to name.
_SURYA_KEYS = {
    'title',
    'protocol',
    'classType',
    'capabilities',
    'register',
    'node',
    'path',
    'prefix',
    'description',
    'icon',
    'documentation',
    'lanes',
    'fields',
}


class TestSuryaKeySet:
    def test_the_key_set_is_exactly_what_the_split_authored(self) -> None:
        assert set(_SURYA) == _SURYA_KEYS

    @pytest.mark.parametrize('key', ['classType', 'description', 'documentation'])
    def test_keys_every_sibling_file_carries_are_present(self, key: str) -> None:
        assert key in _SURYA

    @pytest.mark.parametrize('key', ['tile', 'test', 'fulltest'])
    def test_keys_no_sibling_file_carries_are_absent(self, key: str) -> None:
        """`test` in particular: `nodes:test` runs in the shared environment and
        this component's engine lives in a scoped overlay by design.
        """
        assert key not in _SURYA


class TestSuryaValues:
    def test_the_protocol_keeps_its_suffix(self) -> None:
        """Every protocol value in the tree is spelled `<name>://`; dropping the
        suffix fails silently rather than loudly.
        """
        assert _SURYA['protocol'] == 'ocr_surya://'

    def test_the_path_points_at_the_component(self) -> None:
        assert _SURYA['path'] == 'nodes.ocr.surya'

    def test_the_prefix_is_shared_with_the_standard_component(self) -> None:
        """Deliberately `ocr`, not `ocr_surya`: `prefix` is a family label, not the
        protocol stem (agent_crewai gives all three of its components one). The
        assertion is here so a later reader does not "fix" it into the stem.
        """
        assert _SURYA['prefix'] == 'ocr'
        assert _SURYA['prefix'] == _STANDARD['prefix']

    def test_the_title_is_present_and_non_empty(self) -> None:
        """With two services files the docs generator builds a section heading
        from each `title`; an empty one degrades to a bare filename.
        """
        assert _SURYA['title'].strip()

    def test_the_description_is_a_list_of_strings(self) -> None:
        """A hand-written scalar is a type error nothing in the tree would catch."""
        assert isinstance(_SURYA['description'], list)
        assert _SURYA['description']
        assert all(isinstance(part, str) for part in _SURYA['description'])

    def test_the_icon_exists_beside_the_services_file(self) -> None:
        assert (_NODE_DIR / _SURYA['icon']).is_file()

    def test_the_class_type_matches_the_standard_component(self) -> None:
        """Both consume images; the docs category and canvas grouping read this."""
        assert _SURYA['classType'] == _STANDARD['classType']


class TestSuryaLanes:
    """Where "no tables" becomes a declared fact rather than a Python one."""

    def test_the_image_lane_produces_text_only(self) -> None:
        assert _SURYA['lanes']['image'] == ['text']

    def test_no_lane_declares_a_table_output(self) -> None:
        assert not any('table' in outputs for outputs in _SURYA['lanes'].values())

    def test_the_documents_lane_is_unchanged(self) -> None:
        assert _SURYA['lanes']['documents'] == ['text']

    def test_the_standard_component_keeps_its_table_lane(self) -> None:
        assert 'table' in _STANDARD['lanes']['image']


class TestSuryaHasNoConfiguration:
    def test_fields_is_present_and_empty(self) -> None:
        """Present, because `gen-node-tables.mjs` skips any services file with no
        `fields` key at all and this component's whole Schema section would
        vanish. Empty, because every field on the standard node is an engine
        picker, an engine-specific option or a table setting.
        """
        assert _SURYA['fields'] == {}

    def test_there_is_no_engine_field(self) -> None:
        """The absence that makes the standard `Reader`'s 'easyocr' default
        dangerous — see test_engine_migration.py.
        """
        assert 'engine' not in _SURYA['fields']
        assert 'ocr.engine' not in _SURYA['fields']


class TestStandardServicesSurgery:
    def test_the_path_moved_down_one_level(self) -> None:
        assert _STANDARD['path'] == 'nodes.ocr.standard'

    def test_the_protocol_is_untouched(self) -> None:
        """Saved `.pipe` documents resolve by protocol; only the path behind it moved."""
        assert _STANDARD['protocol'] == 'ocr://'

    @pytest.mark.parametrize('field', ['ocr.engine', 'ocr.table_engine'])
    @pytest.mark.parametrize('gone', ['surya', 'trocr'])
    def test_the_pickers_no_longer_offer_the_removed_engines(self, field: str, gone: str) -> None:
        values = [entry[0] for entry in _STANDARD['fields'][field]['enum']]
        assert gone not in values

    @pytest.mark.parametrize('field', ['ocr.engine', 'ocr.table_engine'])
    def test_the_defaults_survived_the_removal(self, field: str) -> None:
        values = [entry[0] for entry in _STANDARD['fields'][field]['enum']]
        assert _STANDARD['fields'][field]['default'] in values

    @pytest.mark.parametrize('field', ['ocr.engine', 'ocr.table_engine'])
    @pytest.mark.parametrize('gone', ['Surya', 'TrOCR'])
    def test_the_picker_prose_stops_narrating_removed_engines(self, field: str, gone: str) -> None:
        """These strings resurface in the generated PARAMS table's Description
        column at deploy, where they cannot be hand-fixed. `ocr.engine` may still
        point at the Surya component — as a redirect, not as an offer.
        """
        description = _STANDARD['fields'][field]['description']
        if gone == 'Surya' and field == 'ocr.engine':
            assert 'ocr_surya://' in description, 'the redirect is the only migration pointer here'
            return
        assert gone not in description

    @pytest.mark.parametrize('gone', ['surya', 'trocr'])
    def test_the_preconfig_profiles_are_gone(self, gone: str) -> None:
        assert gone not in _STANDARD['preconfig']['profiles']

    def test_the_profile_picker_needs_no_second_deletion(self) -> None:
        """Its enum is a reference expression resolved from `preconfig`, so the two
        profile deletions above propagate on their own.
        """
        assert _STANDARD['fields']['ocr.profile']['enum'] == ['*>preconfig.profiles.*.title']

    def test_every_fulltest_profile_still_exists(self) -> None:
        """A `fulltest` entry naming a deleted preconfig breaks `nodes:test-full`,
        and nothing in the services surgery itself would have hinted at it.
        """
        profiles = set(_STANDARD['preconfig']['profiles'])
        named = {p for block in _STANDARD['fulltest'] for p in block['profiles']}
        named |= set(_STANDARD['test']['profiles'])
        assert named <= profiles

    def test_the_default_profile_still_exists(self) -> None:
        assert _STANDARD['preconfig']['default'] in _STANDARD['preconfig']['profiles']


class TestTheTwoFilesDoNotCollide:
    def test_the_protocols_differ(self) -> None:
        assert _STANDARD['protocol'] != _SURYA['protocol']

    def test_the_paths_differ(self) -> None:
        assert _STANDARD['path'] != _SURYA['path']

    def test_the_standard_file_still_sorts_first(self) -> None:
        """The docs site takes the node's sidebar label from the alphabetically
        first `services*.json`. 'services.json' < 'services.surya.json' keeps
        /nodes/ocr labelled "OCR" — luck of the alphabet, so pin it: a future
        suffix sorting below 'j' would silently take the node's docs identity.
        """
        names = sorted(p.name for p in _NODE_DIR.glob('services*.json'))
        assert names[0] == 'services.json'
