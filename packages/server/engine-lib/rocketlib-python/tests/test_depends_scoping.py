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


def test_activation_records_last_used(tmp_path, monkeypatch, restore_active_env):
    # The only production call site of touch_last_used is a closure inside ensure_env_scoped, so
    # venv_env's own run_scoped_install tests cannot reach it -- they capture on_overlay with
    # `overlaid.append` and never invoke it. Stubbing the orchestration and calling the callback
    # the real code passed is what actually exercises the wiring.
    paths = V.env_paths(V.env_dir(str(tmp_path), 'proj', 'main'))
    os.makedirs(paths.site_packages, exist_ok=True)
    called = []

    def _run(*_args, **kwargs):
        kwargs['on_overlay'](paths)
        called.append(True)
        return paths.site_packages

    monkeypatch.setattr(D, '_get_executable_dir', lambda: str(tmp_path))
    monkeypatch.setattr(V, 'run_scoped_install', _run)
    monkeypatch.setattr(D, '_reprove_unproved', lambda *a, **k: None)
    monkeypatch.setattr(D, '_shadowing_check', lambda *a, **k: None)

    D.ensure_env_scoped('proj', 'main', [])

    assert called, 'the overlay callback must have run'
    assert os.path.isfile(paths.last_used_file), 'activation is what the reclamation side reads'


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


# --- forced requirements: the child's file half (§4.7.1) --------------------


def _tree_overrides(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body, encoding='utf-8')
    return str(path)


def test_override_args_default_is_the_base_answer_not_a_fallback(tmp_path, monkeypatch):
    """The shared cache file is what ``None`` *means*, and getting it wrong is silent.

    Handing base an environment's file would drop the tree's own ``ai/**/overrides.txt`` out of
    the compile that governs the engine runtime and the whole legacy path, and nothing would
    fail -- the resolution would simply stop honouring an override honoured since it was written.
    """
    shared = tmp_path / 'cache' / 'overrides-combined.txt'
    shared.parent.mkdir(parents=True)
    shared.write_text('tabulate==0.9.0\n', encoding='utf-8')
    monkeypatch.setattr(D, '_get_overrides_path', lambda: str(shared))

    args = D._override_args(str(tmp_path))
    assert args[0] == '--override'
    assert args[1].replace('\\', '/') == 'cache/overrides-combined.txt'


def test_override_args_takes_the_environments_file_when_given_one(tmp_path):
    env_file = tmp_path / 'venvs' / 'p' / 'v1' / 'overrides-combined.txt'
    env_file.parent.mkdir(parents=True)
    env_file.write_text('tabulate==0.9.0\n', encoding='utf-8')

    args = D._override_args(str(tmp_path), str(env_file))
    assert args[0] == '--override'
    assert args[1].replace('\\', '/') == 'venvs/p/v1/overrides-combined.txt'


def test_override_args_is_empty_for_an_absent_or_empty_file(tmp_path):
    missing = str(tmp_path / 'nope.txt')
    assert D._override_args(str(tmp_path), missing) == []
    empty = tmp_path / 'empty.txt'
    empty.touch()
    assert D._override_args(str(tmp_path), str(empty)) == []


def test_active_overrides_path_follows_the_active_environment(tmp_path, restore_active_env):
    """The runtime install path cannot be passed an environment, so it reads the active one.

    ``depends()`` is called by node code mid-run, which has no environment to hand over and no
    way to learn one; the active context is the only thing that knows.
    """
    paths = V.env_paths(V.env_dir(str(tmp_path), 'p', 'v1'))
    os.makedirs(paths.site_packages, exist_ok=True)
    D.activate_env(D.register_env(paths.env_dir))
    assert D._active_overrides_path() == os.path.join(paths.env_dir, 'overrides-combined.txt')

    D.activate_env(None)
    assert D._active_overrides_path() == D._get_overrides_path()


def test_write_env_overrides_merges_forced_over_the_tree(tmp_path, monkeypatch):
    env_dir = tmp_path / 'env'
    env_dir.mkdir()
    tree = _tree_overrides(tmp_path, 'overrides.txt', 'tabulate==0.8.10\nsix==1.16.0\n')

    text = 'tabulate==0.9.0\n'
    digest = 'a' * 64
    forced_dir = tmp_path / 'cache' / 'forced'
    forced_dir.mkdir(parents=True)
    (forced_dir / f'{digest}.txt').write_text(text, encoding='utf-8')
    monkeypatch.setattr(D, '_forced_path', lambda d: str(forced_dir / f'{d}.txt'))

    out = D._write_env_overrides(str(env_dir), [tree], digest)
    body = open(out, encoding='utf-8').read()
    assert 'tabulate==0.9.0' in body
    assert 'tabulate==0.8.10' not in body, 'forced replaces the tree line for that name'
    assert 'six==1.16.0' in body, 'names forced does not mention keep what the tree said'


def test_write_env_overrides_with_no_forced_is_just_the_tree(tmp_path):
    env_dir = tmp_path / 'env'
    env_dir.mkdir()
    tree = _tree_overrides(tmp_path, 'overrides.txt', 'tabulate==0.8.10\n')
    out = D._write_env_overrides(str(env_dir), [tree], '')
    assert 'tabulate==0.8.10' in open(out, encoding='utf-8').read()


def test_write_env_overrides_is_regenerated_rather_than_cached(tmp_path):
    # No digest in the name and no write-if-absent: it is derived, and a cached one would go
    # stale across an engine upgrade where the tree's override files move and forced does not.
    env_dir = tmp_path / 'env'
    env_dir.mkdir()
    tree = _tree_overrides(tmp_path, 'overrides.txt', 'tabulate==0.8.10\n')
    first = D._write_env_overrides(str(env_dir), [tree], '')
    (tmp_path / 'overrides.txt').write_text('tabulate==0.10.0\n', encoding='utf-8')
    second = D._write_env_overrides(str(env_dir), [tree], '')
    assert first == second
    assert 'tabulate==0.10.0' in open(second, encoding='utf-8').read()


def test_a_non_empty_digest_whose_file_is_missing_refuses_by_name(tmp_path):
    """Refuse rather than compile without the overrides.

    Compiling anyway installs versions the document did not ask for, silently, and this process
    cannot re-create a text it never received -- only the digest crosses the boundary.
    """
    env_dir = tmp_path / 'env'
    env_dir.mkdir()
    with pytest.raises(D.ForcedRequirementsMissing) as excinfo:
        D._write_env_overrides(str(env_dir), [], 'b' * 64)
    message = str(excinfo.value)
    assert 'b' * 64 in message, 'name the file, so the cause is findable'
    assert 'repeatable' in message, 'say the run can simply be launched again'


def test_another_documents_forced_file_is_neither_read_nor_removed(tmp_path, monkeypatch):
    # Sweeping is the plausible wrong instinct and it is how one deployed version would delete
    # another's file. The digest naming makes this a non-case rather than a rule.
    env_dir = tmp_path / 'env'
    env_dir.mkdir()
    forced_dir = tmp_path / 'cache' / 'forced'
    forced_dir.mkdir(parents=True)
    stale = forced_dir / ('c' * 64 + '.txt')
    stale.write_text('six==1.16.0\n', encoding='utf-8')
    monkeypatch.setattr(D, '_forced_path', lambda d: str(forced_dir / f'{d}.txt'))

    out = D._write_env_overrides(str(env_dir), [], '')
    assert stale.is_file(), 'a file this run does not name must survive'
    assert 'six' not in open(out, encoding='utf-8').read(), 'and must not be read either'


def test_tree_include_lines_are_absolutised_into_the_merged_file(tmp_path):
    r"""The half of ``write_combined`` a replacement drops without anything failing.

    uv resolves an include relative to the file holding it, and the merged file lives elsewhere;
    a requirement file also treats ``\`` as an escape, so ``-r C:\x\y.txt`` reaches uv as
    ``C:xy.txt``. No override file in the tree carries a flag line today, which is exactly what
    would make losing this silent until someone adds one on Windows.
    """
    env_dir = tmp_path / 'env'
    env_dir.mkdir()
    (tmp_path / 'inner.txt').write_text('idna==3.6\n', encoding='utf-8')
    tree = _tree_overrides(tmp_path, 'overrides.txt', '-r inner.txt\n')

    out = D._write_env_overrides(str(env_dir), [tree], '')
    body = open(out, encoding='utf-8').read()
    assert '-r inner.txt' not in body
    assert '/inner.txt' in body and '\\inner.txt' not in body


# --- forced requirements: the warning channel (2C-FR step 4) ----------------


def _warning_plan(tmp_path, requirements, resolution):
    """A plan whose constraints file already holds ``resolution``, as if uv had just run."""
    req = tmp_path / 'r.txt'
    req.write_text(requirements, encoding='utf-8')
    plan = V.plan_install(str(tmp_path), 'p', 'v1', [str(req)])
    with open(plan.paths.constraints, 'w', encoding='utf-8') as fh:
        fh.write(resolution)
    return plan


def _capture(monkeypatch):
    sent = []
    monkeypatch.setattr(D, 'updateProgress', sent.append)
    monkeypatch.setattr(D, 'error', lambda message: None)
    return sent


def test_warnings_reach_the_progress_channel(tmp_path, monkeypatch):
    """The channel is the one already reporting ``Downloading torch (2.7GiB)``.

    The install-lock sidecar beside it is not a second channel: it is inter-process, read by a
    process waiting on the lock, and reaches no user.
    """
    plan = _warning_plan(tmp_path, 'tabulate==0.8.10\n', 'tabulate==0.8.10\n')
    forced_dir = tmp_path / 'cache' / 'forced'
    forced_dir.mkdir(parents=True)
    digest = 'd' * 64
    (forced_dir / f'{digest}.txt').write_text('tabulaet==0.9.0\n', encoding='utf-8')
    monkeypatch.setattr(D, '_forced_path', lambda d: str(forced_dir / f'{d}.txt'))

    sent = _capture(monkeypatch)
    D._report_forced_warnings(plan, digest)
    assert any('tabulaet' in message for message in sent)


def test_no_forced_text_says_nothing(tmp_path, monkeypatch):
    # Every environment on the day this ships. Reporting has to be free for them.
    plan = _warning_plan(tmp_path, 'tabulate==0.8.10\n', 'tabulate==0.8.10\n')
    sent = _capture(monkeypatch)
    D._report_forced_warnings(plan, '')
    assert sent == []


def test_a_missing_forced_file_does_not_raise_from_the_reporting_helper(tmp_path, monkeypatch):
    # The install path raises ForcedRequirementsMissing for real; re-raising here would give one
    # cause two call sites and let a *reporting* helper fail a run.
    plan = _warning_plan(tmp_path, 'tabulate==0.8.10\n', 'tabulate==0.8.10\n')
    sent = _capture(monkeypatch)
    D._report_forced_warnings(plan, 'e' * 64)
    assert sent == []


# --- forced requirements: which environment each --override caller serves ---
#
# These are call-site assertions rather than behavioural ones, and they are here as a pair on
# purpose. `_override_args` is read from three places and none of them belongs to one path
# alone, so there are two ways to get it wrong and neither fails anything: stopping at the
# compile *mostly works* and diverges occasionally, while handing base an environment's file
# drops the tree's own `ai/**/overrides.txt` out of the compile that governs the engine runtime.
# Nothing that merely checks a resolved version notices either.


class _FakeCompleted:
    def __init__(self, stdout='', stderr='', returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _FakePopen:
    """Just enough of Popen for the install path: a stdout to drain and a zero exit."""

    def __init__(self, args, **_kwargs):
        self.args = args
        self.stdout = iter(())
        self.returncode = 0

    def wait(self):
        return 0


@pytest.fixture
def uv_argv(monkeypatch):
    """Capture every uv argv this module builds, without running uv."""
    seen = []

    def fake_run(args, **_kwargs):
        seen.append(list(args))
        # The dry-run parses "+ name==version" lines to decide there is work to do.
        return _FakeCompleted(stdout='+ somepkg==1.0\n')

    def fake_popen(args, **kwargs):
        seen.append(list(args))
        return _FakePopen(args, **kwargs)

    monkeypatch.setattr(D.subprocess, 'run', fake_run)
    monkeypatch.setattr(D.subprocess, 'Popen', fake_popen)
    monkeypatch.setattr(D, '_uv_available', lambda: True)
    monkeypatch.setattr(D, '_uv_abs_path', lambda: 'uv')
    monkeypatch.setattr(D, '_start_heartbeat', lambda: None)
    monkeypatch.setattr(D, '_stop_heartbeat', lambda: None)
    monkeypatch.setattr(D, 'updateProgress', lambda message: None)
    return seen


def _override_value(argv):
    """The path passed to ``--override`` in one argv, or ``None``."""
    for index, token in enumerate(argv):
        if token == '--override':
            return argv[index + 1].replace('\\', '/')
    return None


def _nonempty(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('tabulate==0.9.0\n')
    return path


def test_the_compile_is_told_which_environment_it_serves(tmp_path, uv_argv, monkeypatch):
    monkeypatch.setattr(D, '_get_executable_dir', lambda: str(tmp_path))
    combined = _nonempty(str(tmp_path / 'combined.txt'))
    constraints = str(tmp_path / 'constraints.txt')
    env_overrides = _nonempty(str(tmp_path / 'venvs' / 'p' / 'v1' / 'overrides-combined.txt'))

    D._run_uv_compile(combined, constraints, env_overrides)
    assert _override_value(uv_argv[-1]) == 'venvs/p/v1/overrides-combined.txt'


def test_the_compile_keeps_the_shared_file_on_the_base_path(tmp_path, uv_argv, monkeypatch):
    monkeypatch.setattr(D, '_get_executable_dir', lambda: str(tmp_path))
    shared = _nonempty(str(tmp_path / 'cache' / 'overrides-combined.txt'))
    monkeypatch.setattr(D, '_get_overrides_path', lambda: shared)
    combined = _nonempty(str(tmp_path / 'combined.txt'))

    D._run_uv_compile(combined, str(tmp_path / 'constraints.txt'))
    assert _override_value(uv_argv[-1]) == 'cache/overrides-combined.txt'


def test_both_install_readers_follow_the_active_overlay(tmp_path, uv_argv, monkeypatch, restore_active_env):
    """`_install_dry_run` and `_install_requirements_inner`, in one run of the install path.

    They cannot be *told* an environment: ``depends()`` is called by node code at runtime,
    which has no environment to hand over. They read the active context instead.
    """
    monkeypatch.setattr(D, '_get_executable_dir', lambda: str(tmp_path))
    paths = V.env_paths(V.env_dir(str(tmp_path), 'p', 'v1'))
    os.makedirs(paths.site_packages, exist_ok=True)
    env_overrides = _nonempty(os.path.join(paths.env_dir, 'overrides-combined.txt'))
    D.activate_env(D.register_env(paths.env_dir))

    requirements = _nonempty(str(tmp_path / 'r.txt'))
    constraints = str(tmp_path / 'constraints.txt')
    with open(constraints, 'w', encoding='utf-8') as fh:
        fh.write('tabulate==0.9.0\n')

    D._install_requirements_inner(requirements, constraints)

    overrides_seen = [_override_value(argv) for argv in uv_argv if _override_value(argv)]
    assert len(overrides_seen) >= 2, 'both the dry-run and the install must pass --override'
    relative = os.path.relpath(env_overrides, str(tmp_path)).replace('\\', '/')
    assert set(overrides_seen) == {relative}


def test_both_install_readers_keep_the_shared_file_on_base(tmp_path, uv_argv, monkeypatch, restore_active_env):
    # The mirror mistake, and the worse one: base losing the tree's own overrides would change
    # what the engine runtime resolves, and nothing would fail.
    monkeypatch.setattr(D, '_get_executable_dir', lambda: str(tmp_path))
    shared = _nonempty(str(tmp_path / 'cache' / 'overrides-combined.txt'))
    monkeypatch.setattr(D, '_get_overrides_path', lambda: shared)
    D.activate_env(None)

    requirements = _nonempty(str(tmp_path / 'r.txt'))
    constraints = str(tmp_path / 'constraints.txt')
    with open(constraints, 'w', encoding='utf-8') as fh:
        fh.write('tabulate==0.9.0\n')

    D._install_requirements_inner(requirements, constraints)

    overrides_seen = [_override_value(argv) for argv in uv_argv if _override_value(argv)]
    assert len(overrides_seen) >= 2
    assert set(overrides_seen) == {'cache/overrides-combined.txt'}


# --- the scoped install is a FOURTH place the override is needed ------------
#
# Found by the live run of 2C-FR step 7, not by any of the tests above, and the reason is worth
# keeping: the design counted the three *readers* of `_override_args` and treated that as the set
# of places the override belongs. `_install_target` read it nowhere, so it was never counted —
# and without forced it never had to, because an environment's requirement file and its own
# compiled resolution could not disagree. Forced is exactly the thing that makes them disagree.


def test_the_scoped_install_passes_the_same_override_the_compile_did(tmp_path, uv_argv, monkeypatch):
    """The live failure, reproduced: `-r` says one version, `-c` says the overridden one.

    uv re-resolves the requirements against the constraints, and without the override it sees a
    requirement it cannot satisfy — *"Because you require tabulate==0.9.0 and tabulate==0.10.0,
    we can conclude that your requirements are unsatisfiable"*. The run fails outright rather
    than installing the wrong thing, which is the loud half of an otherwise quiet class.
    """
    monkeypatch.setattr(D, '_get_executable_dir', lambda: str(tmp_path))
    paths = V.env_paths(V.env_dir(str(tmp_path), 'p', 'v1'))
    os.makedirs(paths.site_packages, exist_ok=True)

    with open(paths.combined, 'w', encoding='utf-8') as fh:
        fh.write('tabulate==0.10.0\n')  # what the node declared
    with open(paths.constraints, 'w', encoding='utf-8') as fh:
        fh.write('tabulate==0.9.0\n')  # what the override turned it into
    overrides = _nonempty(os.path.join(paths.env_dir, 'overrides-combined.txt'))

    D._install_target(paths.combined, paths.constraints, paths.site_packages, overrides)

    install = [argv for argv in uv_argv if 'install' in argv][-1]
    assert _override_value(install) is not None, 'the install must carry the override, not only the compile'
    assert _override_value(install) == os.path.relpath(overrides, str(tmp_path)).replace('\\', '/')


def test_the_scoped_install_without_an_environment_file_still_takes_base(tmp_path, uv_argv, monkeypatch):
    # The same default as everywhere else: `None` is the base answer, not a fallback.
    monkeypatch.setattr(D, '_get_executable_dir', lambda: str(tmp_path))
    shared = _nonempty(str(tmp_path / 'cache' / 'overrides-combined.txt'))
    monkeypatch.setattr(D, '_get_overrides_path', lambda: shared)
    paths = V.env_paths(V.env_dir(str(tmp_path), 'p', 'v2'))
    os.makedirs(paths.site_packages, exist_ok=True)
    with open(paths.combined, 'w', encoding='utf-8') as fh:
        fh.write('tabulate==0.10.0\n')
    with open(paths.constraints, 'w', encoding='utf-8') as fh:
        fh.write('tabulate==0.10.0\n')

    D._install_target(paths.combined, paths.constraints, paths.site_packages)

    install = [argv for argv in uv_argv if 'install' in argv][-1]
    assert _override_value(install) == 'cache/overrides-combined.txt'


def test_the_compile_and_the_install_get_the_SAME_override_file(tmp_path, uv_argv, monkeypatch):
    """The wiring, not the two functions — which is what the earlier tests could not reach.

    A version where only the compile got the environment's file shipped and ran: the compile
    honoured the override, the install re-resolved without it, and uv refused the run. Every
    test passed, because each one called a function directly with a path of its own. This one
    drives both from the same caller and asserts they agree.
    """
    monkeypatch.setattr(D, '_get_executable_dir', lambda: str(tmp_path))
    shared = _nonempty(str(tmp_path / 'cache' / 'overrides-combined.txt'))
    monkeypatch.setattr(D, '_get_overrides_path', lambda: shared)
    monkeypatch.setattr(D, 'bootstrap', lambda: None)

    tree = _nonempty(str(tmp_path / 'overrides.txt'))
    forced_dir = tmp_path / 'cache' / 'forced'
    forced_dir.mkdir(parents=True, exist_ok=True)
    digest = 'f' * 64
    (forced_dir / f'{digest}.txt').write_text('tabulate==0.9.0\n', encoding='utf-8')
    monkeypatch.setattr(D, '_forced_path', lambda d: str(forced_dir / f'{d}.txt'))

    plan = V.plan_install(str(tmp_path), 'p', 'v1', [], create=True)
    with open(plan.paths.combined, 'w', encoding='utf-8') as fh:
        fh.write('tabulate==0.10.0\n')
    with open(plan.paths.constraints, 'w', encoding='utf-8') as fh:
        fh.write('tabulate==0.9.0\n')

    D._scoped_compile_and_install(plan, [tree], digest)

    used = [_override_value(argv) for argv in uv_argv if _override_value(argv)]
    expected = os.path.relpath(os.path.join(plan.paths.env_dir, 'overrides-combined.txt'), str(tmp_path)).replace(
        '\\', '/'
    )
    assert len(used) >= 2, 'both the compile and the install must pass --override'
    assert set(used) == {expected}, f'compile and install disagree: {set(used)}'
    assert shared.replace('\\', '/') not in [os.path.join(str(tmp_path), u).replace('\\', '/') for u in used]
