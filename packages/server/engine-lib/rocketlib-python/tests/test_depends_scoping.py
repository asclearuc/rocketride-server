# =============================================================================
# MIT License — Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""Tests for the parts of ``depends`` that react to the scoping switch.

``depends`` imports ``engLib``, so these run under the engine interpreter
(``builder server:run-rocketlib-test``) and are skipped under a bare Python.
"""

from __future__ import annotations

import os

import pytest

try:
    import depends as D

    _HAVE_ENGLIB = True
except ImportError:  # engLib is built into engine.exe
    _HAVE_ENGLIB = False

pytestmark = pytest.mark.skipif(not _HAVE_ENGLIB, reason='depends needs engLib (engine interpreter)')


def test_overlay_goes_behind_the_mock_shims(monkeypatch, tmp_path):
    # ai/node.py front-loads ROCKETRIDE_MOCK with stub SDKs; the overlay carries the real
    # ones, so landing at index 0 would send node tests to the live services.
    mocks = str(tmp_path / 'mocks')
    monkeypatch.setenv('ROCKETRIDE_MOCK', mocks)
    monkeypatch.setattr(D.sys, 'path', [mocks, '/base/site-packages'])
    assert D._overlay_index() == 1


def test_overlay_index_is_front_without_mocks(monkeypatch):
    monkeypatch.delenv('ROCKETRIDE_MOCK', raising=False)
    assert D._overlay_index() == 0


def test_overlay_index_tolerates_mock_path_not_on_sys_path(monkeypatch, tmp_path):
    monkeypatch.setenv('ROCKETRIDE_MOCK', str(tmp_path / 'absent'))
    monkeypatch.setattr(D.sys, 'path', ['/base/site-packages'])
    assert D._overlay_index() == 0


def _has_node_path(paths):
    root = os.path.abspath(os.path.join(D._get_executable_dir(), 'nodes'))
    return any(os.path.abspath(p).startswith(root + os.sep) for p in paths)


def test_forced_scoping_drops_node_requirements(monkeypatch):
    # With scoping forced on, node deps come from each env's scoped install; folding them
    # into the startup compile would make two conflicting nodes unable to coexist at all.
    monkeypatch.setenv('ROCKETRIDE_SERVER_USE_VENV', '1')
    assert not _has_node_path(D._find_requirement_files())


@pytest.mark.parametrize('value', ['0', None])
def test_legacy_and_auto_keep_node_requirements(monkeypatch, value):
    if value is None:
        monkeypatch.delenv('ROCKETRIDE_SERVER_USE_VENV', raising=False)
    else:
        monkeypatch.setenv('ROCKETRIDE_SERVER_USE_VENV', value)
    unscoped = D._find_requirement_files()
    monkeypatch.setenv('ROCKETRIDE_SERVER_USE_VENV', '1')
    assert set(D._find_requirement_files()) <= set(unscoped)
    # Only meaningful while the installation actually ships node requirement files.
    if os.path.isdir(os.path.join(D._get_executable_dir(), 'nodes')):
        assert _has_node_path(unscoped)


def test_ai_and_root_requirements_survive_forced_scoping(monkeypatch):
    monkeypatch.setenv('ROCKETRIDE_SERVER_USE_VENV', '1')
    exe_dir = os.path.abspath(D._get_executable_dir())
    found = [os.path.abspath(p) for p in D._find_requirement_files()]
    ai_root = os.path.join(exe_dir, 'ai')
    if os.path.isdir(ai_root):
        assert any(p.startswith(ai_root + os.sep) for p in found), 'base runtime must keep ai deps'
