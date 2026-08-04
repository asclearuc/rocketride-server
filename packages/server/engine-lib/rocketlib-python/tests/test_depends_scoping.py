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

import venv_env as V

try:
    import depends as D

    _HAVE_ENGLIB = True
except ImportError:  # engLib is built into engine.exe
    _HAVE_ENGLIB = False

pytestmark = pytest.mark.skipif(not _HAVE_ENGLIB, reason='depends needs engLib (engine interpreter)')


@pytest.fixture
def restore_active_env():
    """Leave the process on whatever environment it started on."""
    previous = D.active_env() if _HAVE_ENGLIB else None
    yield
    D.activate_env(None if previous is None or not previous.is_overlay else previous)


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


# One predicate no longer expresses the rule: under =1 the node tree contributes exactly one file
# and no others, so "is it under nodes/" cannot tell the kept case from the dropped one.


def _node_root():
    return os.path.abspath(os.path.join(D._get_executable_dir(), 'nodes'))


def _has_tree_baseline(paths):
    """``<exe>/nodes/requirements.txt`` — the Python-backend floor, kept in every mode."""
    want = os.path.join(_node_root(), 'requirements.txt')
    return any(os.path.abspath(p) == want for p in paths)


def _has_per_node_file(paths):
    """A requirement file owned by an individual node, i.e. *below* ``<exe>/nodes/``."""
    root = _node_root()
    return any(
        os.path.abspath(p).startswith(root + os.sep) and os.path.dirname(os.path.abspath(p)) != root for p in paths
    )


def _ships_node_files():
    """The assertions about the node tree only mean something where one is installed."""
    return os.path.isdir(_node_root())


# The cases below measure the requirement-file glob, not the switch. venv_env freezes the mode
# at first read, so each one relies on conftest's autouse reset to get one.


def test_forced_scoping_drops_per_node_files_but_keeps_the_tree_baseline(monkeypatch):
    # Per-node deps come from each env's scoped install; folding them into the startup compile
    # would make two conflicting nodes unable to coexist at all. The baseline is not a node
    # dependency -- it is the floor the engine's own Python runs on -- so it stays, and the
    # positive half is what stops the drop from quietly widening back into it.
    monkeypatch.setenv('ROCKETRIDE_SERVER_USE_VENV', '1')
    found = D._find_requirement_files()
    assert not _has_per_node_file(found)
    if _ships_node_files():
        assert _has_tree_baseline(found)


def test_the_glob_does_not_follow_a_mid_process_flip(monkeypatch):
    # A node flipping the switch must not put the per-node globs back into the base compile.
    monkeypatch.setenv('ROCKETRIDE_SERVER_USE_VENV', '1')
    assert not _has_per_node_file(D._find_requirement_files())
    monkeypatch.setenv('ROCKETRIDE_SERVER_USE_VENV', '0')
    assert not _has_per_node_file(D._find_requirement_files())


@pytest.mark.parametrize('value', ['0', None])
def test_legacy_and_auto_keep_node_requirements(monkeypatch, value):
    if value is None:
        monkeypatch.delenv('ROCKETRIDE_SERVER_USE_VENV', raising=False)
    else:
        monkeypatch.setenv('ROCKETRIDE_SERVER_USE_VENV', value)
    unscoped = D._find_requirement_files()
    monkeypatch.setenv('ROCKETRIDE_SERVER_USE_VENV', '1')
    V._reset_venv_env_cache()  # without this the frozen set comes back and the case passes vacuously
    assert set(D._find_requirement_files()) <= set(unscoped)
    # Only meaningful while the installation actually ships node requirement files.
    if _ships_node_files():
        assert _has_per_node_file(unscoped)


def test_ai_and_root_requirements_survive_forced_scoping(monkeypatch):
    monkeypatch.setenv('ROCKETRIDE_SERVER_USE_VENV', '1')
    exe_dir = os.path.abspath(D._get_executable_dir())
    found = [os.path.abspath(p) for p in D._find_requirement_files()]
    ai_root = os.path.join(exe_dir, 'ai')
    if os.path.isdir(ai_root):
        assert any(p.startswith(ai_root + os.sep) for p in found), 'base runtime must keep ai deps'


# --- the active environment -------------------------------------------------


def test_base_context_installs_into_the_runtime(restore_active_env):
    base = D.active_env()
    assert base.is_overlay is False
    assert base.paths.site_packages == D._get_site_packages()
    assert base.paths.constraints == D._get_constraints_path()
    assert D._target_args() == [], 'the base install must never carry --target'


def test_use_env_switches_all_four_things_together(tmp_path, restore_active_env):
    # Lock, constraints, --target and the installed record are one decision; holding
    # them in separate globals is what made a second environment unsafe.
    ctx = D.register_env(str(tmp_path / 'venvs' / 'p' / 'main'))
    base = D.active_env()

    with D.use_env(ctx):
        assert D.active_env() is ctx
        assert D._target_args() == ['--target', ctx.paths.site_packages]
        assert ctx.paths.constraints != base.paths.constraints
        assert ctx.paths.lock_file != base.paths.lock_file

    assert D.active_env() is base


def test_use_env_restores_after_an_exception(tmp_path, restore_active_env):
    ctx = D.register_env(str(tmp_path / 'venvs' / 'p' / 'main'))
    base = D.active_env()
    with pytest.raises(RuntimeError):
        with D.use_env(ctx):
            raise RuntimeError('install failed')
    assert D.active_env() is base


def test_register_env_reuses_the_context(tmp_path):
    directory = str(tmp_path / 'venvs' / 'p' / 'main')
    first = D.register_env(directory)
    first.processed.add('r.txt')
    # Same environment later in the process must not reinstall what it already has.
    assert D.register_env(directory) is first
    assert 'r.txt' in D.register_env(directory).processed


def test_processed_is_per_environment(tmp_path, monkeypatch, restore_active_env):
    req = tmp_path / 'requirements.txt'
    req.write_text('tabulate==0.9.0\n', encoding='utf-8')
    installed = []
    monkeypatch.setattr(D, '_install_requirements', lambda path, constraints: installed.append(path))
    monkeypatch.setattr(D, 'bootstrap', lambda: None)
    monkeypatch.setattr(D, '_apply_pywin32_hack', lambda: None)

    a = D.register_env(str(tmp_path / 'venvs' / 'p' / 'a'))
    b = D.register_env(str(tmp_path / 'venvs' / 'p' / 'b'))
    os.makedirs(a.paths.env_dir, exist_ok=True)
    os.makedirs(b.paths.env_dir, exist_ok=True)

    with D.use_env(a):
        D.depends(str(req))
        D.depends(str(req))  # second call in the same env is the skip
    with D.use_env(b):
        D.depends(str(req))  # different overlay: must install again

    assert len(installed) == 2, 'a per-process set would leave overlay B empty'


def test_overlay_skips_the_global_constraints_compile(tmp_path, monkeypatch, restore_active_env):
    req = tmp_path / 'requirements.txt'
    req.write_text('tabulate==0.9.0\n', encoding='utf-8')
    seen = []
    monkeypatch.setattr(D, '_install_requirements', lambda path, constraints: seen.append(constraints))
    monkeypatch.setattr(D, 'bootstrap', lambda: None)
    monkeypatch.setattr(D, '_apply_pywin32_hack', lambda: None)

    def _fail():
        raise AssertionError('the global union must not be recompiled under an overlay')

    monkeypatch.setattr(D, 'ensure_constraints', _fail)

    ctx = D.register_env(str(tmp_path / 'venvs' / 'p' / 'main'))
    os.makedirs(ctx.paths.env_dir, exist_ok=True)
    with D.use_env(ctx):
        D.depends(str(req))

    assert seen == [ctx.paths.constraints]


# --- FileLock reentrancy ----------------------------------------------------


@pytest.mark.timeout(30)
def test_file_lock_is_reentrant_in_process(tmp_path):
    # Byte-range locks are per file description, so re-opening a path we hold is refused
    # like a foreign holder and the wait loop would poll against ourselves forever.
    lock = str(tmp_path / 'cache' / 'install.lock')
    with D.FileLock(lock, poll_interval=0.05):
        depth = len(D._progress_stack)
        with D.FileLock(lock, poll_interval=0.05):
            assert len(D._progress_stack) == depth, 'reentry must not push a second progress entry'
        assert os.path.exists(lock), 'the outer holder still owns the lock'
    assert len(D._progress_stack) == 1


@pytest.mark.timeout(30)
def test_different_lock_paths_stay_independent(tmp_path):
    with D.FileLock(str(tmp_path / 'a' / 'install.lock'), poll_interval=0.05):
        with D.FileLock(str(tmp_path / 'b' / 'install.lock'), poll_interval=0.05):
            assert len(D._progress_stack) == 3  # fallback + two operations
    assert len(D._progress_stack) == 1


# --- install progress -------------------------------------------------------


def test_nested_stop_leaves_the_outer_heartbeat_running(tmp_path):
    # The heartbeat is what keeps the task startup timeout alive during a long silent uv
    # run; a nested install must not be able to switch it off.
    with D.FileLock(str(tmp_path / 'cache' / 'install.lock'), poll_interval=0.05):
        progress = D._progress()
        D._start_heartbeat()
        D._start_heartbeat()
        D._stop_heartbeat()
        assert progress._thread is not None
        D._stop_heartbeat()
        assert progress._thread is None


def test_download_aggregation_is_per_operation(tmp_path):
    with D.FileLock(str(tmp_path / 'a' / 'install.lock'), poll_interval=0.05):
        D.updateProgress('Downloading torch (2.7GiB)')
        assert [n for n, _ in D._progress().downloading] == ['torch']
        with D.FileLock(str(tmp_path / 'b' / 'install.lock'), poll_interval=0.05):
            assert D._progress().downloading == [], 'a nested operation starts clean'
            D.updateProgress('Downloading numpy (1MiB)')
        assert [n for n, _ in D._progress().downloading] == ['torch']


def test_progress_outside_a_lock_writes_no_sidecar(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    D.updateProgress('Compiling constraints...')  # must not raise
    assert D._progress().sidecar_path is None
    assert list(tmp_path.iterdir()) == []


# --- overlay swap -----------------------------------------------------------


def test_overlay_is_swapped_not_accumulated(tmp_path, monkeypatch):
    # Two overlays in front of base would leave everything A has and B lacks importable
    # from B — the leak overlays exist to prevent, arriving as a wrong version.
    monkeypatch.delenv('ROCKETRIDE_MOCK', raising=False)
    monkeypatch.setattr(D.sys, 'path', ['/base/site-packages'])
    monkeypatch.setattr(D, '_inserted_overlay', None)
    a = str(tmp_path / 'a' / 'site-packages')
    b = str(tmp_path / 'b' / 'site-packages')

    D._apply_overlay_path(a)
    assert D.sys.path[0] == a

    D._apply_overlay_path(a)
    assert D.sys.path.count(a) == 1, 're-applying the same environment is idempotent'

    D._apply_overlay_path(b)
    assert D.sys.path.count(a) == 0, "the previous environment's overlay must be removed"
    assert D.sys.path[0] == b
    assert '/base/site-packages' in D.sys.path, 'base is the floor, never removed'


def test_overlay_swap_respects_the_mock_shims(tmp_path, monkeypatch):
    mocks = str(tmp_path / 'mocks')
    monkeypatch.setenv('ROCKETRIDE_MOCK', mocks)
    monkeypatch.setattr(D.sys, 'path', [mocks, '/base/site-packages'])
    monkeypatch.setattr(D, '_inserted_overlay', None)
    D._apply_overlay_path(str(tmp_path / 'a' / 'site-packages'))
    assert D.sys.path[0] == mocks, 'stub SDKs stay in front of the real ones'
    assert D.sys.path[1].endswith('site-packages')


# --- one install-argv builder ------------------------------------------------


def test_both_install_paths_share_one_argv_builder(tmp_path, restore_active_env):
    # Base is the overlay form minus --target, so a flag can no longer be added to one
    # install path and forgotten in the other.
    ctx = D.register_env(str(tmp_path / 'venvs' / 'p' / 'main'))
    with D.use_env(ctx):
        assert D._target_site() == ctx.paths.site_packages
    assert D._target_site() is None

    overlay = V.build_install_argv('uv', 'py', 'r.txt', ctx.paths.site_packages, 'c.txt', 'ex.txt')
    base = V.build_install_argv('uv', 'py', 'r.txt', None, 'c.txt', 'ex.txt')
    assert base == [a for a in overlay if a not in ('--target', ctx.paths.site_packages)]
