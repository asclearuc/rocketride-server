# =============================================================================
# MIT License — Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""Tests for ``venv_env`` — overlay layout, install planning, and the venv switch."""

from __future__ import annotations

import os

import pytest

import venv_env as V


# --- ROCKETRIDE_SERVER_USE_VENV switch --------------------------------------


def test_use_venv_mode_states():
    assert V.use_venv_mode({}) == V.USE_AUTO
    assert V.use_venv_mode({'ROCKETRIDE_SERVER_USE_VENV': '0'}) == V.USE_OFF
    assert V.use_venv_mode({'ROCKETRIDE_SERVER_USE_VENV': '1'}) == V.USE_ON
    assert V.use_venv_mode({'ROCKETRIDE_SERVER_USE_VENV': 'yes'}) == V.USE_AUTO


def test_mode_is_frozen_against_a_mid_process_rewrite(monkeypatch):
    # The attack: a node rewrites the switch before another node's depends() re-reads it.
    monkeypatch.setenv('ROCKETRIDE_SERVER_USE_VENV', '1')
    assert V.use_venv_mode() == V.USE_ON
    monkeypatch.setenv('ROCKETRIDE_SERVER_USE_VENV', '0')
    assert V.use_venv_mode() == V.USE_ON


def test_unset_freezes_to_auto(monkeypatch):
    # Freezing the absence matters as much: a later export must not turn scoping on mid-process.
    monkeypatch.delenv('ROCKETRIDE_SERVER_USE_VENV', raising=False)
    assert V.use_venv_mode() == V.USE_AUTO
    monkeypatch.setenv('ROCKETRIDE_SERVER_USE_VENV', '1')
    assert V.use_venv_mode() == V.USE_AUTO


def test_explicit_env_is_never_cached(monkeypatch):
    monkeypatch.setenv('ROCKETRIDE_SERVER_USE_VENV', '1')
    assert V.use_venv_mode() == V.USE_ON
    assert V.use_venv_mode({'ROCKETRIDE_SERVER_USE_VENV': '0'}) == V.USE_OFF
    assert V.use_venv_mode({}) == V.USE_AUTO
    assert V.use_venv_mode() == V.USE_ON


def test_reset_restores_first_read_behaviour(monkeypatch):
    monkeypatch.setenv('ROCKETRIDE_SERVER_USE_VENV', '1')
    assert V.use_venv_mode() == V.USE_ON
    monkeypatch.setenv('ROCKETRIDE_SERVER_USE_VENV', '0')
    V._reset_venv_env_cache()
    assert V.use_venv_mode() == V.USE_OFF


def test_scoping_enabled_semantics():
    assert V.scoping_enabled(V.USE_OFF, has_isolated_group=True) is False
    assert V.scoping_enabled(V.USE_ON, has_isolated_group=False) is True
    assert V.scoping_enabled(V.USE_AUTO, has_isolated_group=True) is True
    assert V.scoping_enabled(V.USE_AUTO, has_isolated_group=False) is False


# --- ROCKETRIDE_VENV_ENV_ID (which environment this process installs into) --


def test_resolve_env_id_inherited_beats_the_literal(monkeypatch):
    # The C++ hook hard-codes 'main' for every process, so the inherited value has to win.
    monkeypatch.setenv('ROCKETRIDE_VENV_ENV_ID', 'v1')
    assert V.resolve_env_id('main') == 'v1'


def test_resolve_env_id_falls_through_when_absent(monkeypatch):
    monkeypatch.delenv('ROCKETRIDE_VENV_ENV_ID', raising=False)
    assert V.resolve_env_id('main') == 'main'
    assert V.resolve_env_id(None) is None


@pytest.mark.parametrize('raw', ['', '   '])
def test_resolve_env_id_empty_is_absent(monkeypatch, raw):
    # Otherwise an exported-but-empty value would name an env whose directory is 'default'.
    monkeypatch.setenv('ROCKETRIDE_VENV_ENV_ID', raw)
    assert V.resolve_env_id('main') == 'main'


def test_resolve_env_id_is_consumed_on_first_read(monkeypatch):
    monkeypatch.setenv('ROCKETRIDE_VENV_ENV_ID', 'v1')
    assert V.resolve_env_id('main') == 'v1'
    assert 'ROCKETRIDE_VENV_ENV_ID' not in os.environ
    monkeypatch.setenv('ROCKETRIDE_VENV_ENV_ID', 'v2')
    assert V.resolve_env_id('main') == 'v1'


def test_resolve_env_id_freezes_the_value_not_the_result(monkeypatch):
    # Only the inherited half is frozen; `passed` still resolves on every call.
    monkeypatch.delenv('ROCKETRIDE_VENV_ENV_ID', raising=False)
    assert V.resolve_env_id('main') == 'main'
    assert V.resolve_env_id('other') == 'other'


def test_resolve_env_id_explicit_env_is_neither_cached_nor_popped():
    env = {'ROCKETRIDE_VENV_ENV_ID': 'v1'}
    assert V.resolve_env_id('main', env) == 'v1'
    assert env['ROCKETRIDE_VENV_ENV_ID'] == 'v1'
    assert V.resolve_env_id('main', {}) == 'main'


# --- ROCKETRIDE_VENV_ISOLATED (the raw document fact) -----------------------


@pytest.mark.parametrize(
    'raw,expected',
    [('1', True), ('true', True), ('YES', True), ('0', False), ('', False), ('  ', False), ('nope', False)],
)
def test_isolated_from_env_truth_table(raw, expected):
    assert V.isolated_from_env({'ROCKETRIDE_VENV_ISOLATED': raw}) is expected


def test_isolated_from_env_absent_is_false():
    assert V.isolated_from_env({}) is False


def test_isolated_from_env_is_consumed_on_first_read(monkeypatch):
    monkeypatch.setenv('ROCKETRIDE_VENV_ISOLATED', '1')
    assert V.isolated_from_env() is True
    assert 'ROCKETRIDE_VENV_ISOLATED' not in os.environ
    monkeypatch.delenv('ROCKETRIDE_VENV_ISOLATED', raising=False)
    assert V.isolated_from_env() is True


def test_isolated_from_env_false_is_frozen_too(monkeypatch):
    monkeypatch.delenv('ROCKETRIDE_VENV_ISOLATED', raising=False)
    assert V.isolated_from_env() is False
    monkeypatch.setenv('ROCKETRIDE_VENV_ISOLATED', '1')
    assert V.isolated_from_env() is False


def test_isolated_cannot_switch_scoping_on_under_off():
    # Decision 1 in one assertion: the flag travels as the raw document fact, so however stale it
    # gets it cannot defeat =0 -- scoping_enabled's first branch settles it.
    assert V.scoping_enabled(V.USE_OFF, V.isolated_from_env({'ROCKETRIDE_VENV_ISOLATED': '1'})) is False


# --- id shortening + layout -------------------------------------------------


def test_short_id_rules():
    assert V.short_id(None) == V.DEFAULT_ID
    assert V.short_id('') == V.DEFAULT_ID
    assert V.short_id('----') == V.DEFAULT_ID
    assert V.short_id('main') == 'main'  # lossless and short: kept as-is
    assert V.short_id('group_1').startswith('group1-')  # separator dropped -> disambiguated
    assert V.short_id('0d4f3caa-1234-5678-9abc').startswith('0d4f3caa-')


def test_short_id_stays_within_max_path_budget():
    assert len(V.short_id('0d4f3caa-1234-5678-9abc-0123456789ab')) == V._ID_MAX + 1 + V._HASH_LEN


def test_short_id_is_stable():
    assert V.short_id('test-tool_daytona') == V.short_id('test-tool_daytona')


@pytest.mark.parametrize(
    'a, b',
    [
        # Readable ids spend the whole prefix on their common part; truncation alone
        # collided here and made two node tests share one overlay.
        ('test-tool_daytona', 'test-tool_tavily'),
        ('test-llm_openai_api', 'test-llm_openai_compatible'),
        ('test-vectordb_postgres', 'test-vectordb_qdrant'),
        # Cleaning drops separators, so these differ only in what cleaning removes.
        ('group-1', 'group_1'),
        # GUIDs sharing the leading block.
        ('0d4f3caa-1111-2222', '0d4f3caa-3333-4444'),
    ],
)
def test_short_id_distinguishes_ids_sharing_a_prefix(a, b):
    assert V.short_id(a) != V.short_id(b)


def test_env_dir_layout():
    exe = os.path.join('X:', 'engine')
    d = V.env_dir(exe, '0d4f3caa-1111-2222', 'group_7').replace('\\', '/').split('/')
    assert d[-3] == 'venvs'
    assert d[-2] == V.short_id('0d4f3caa-1111-2222')
    assert d[-1] == V.short_id('group_7')
    assert V.env_dir(exe, None, None).replace('\\', '/').endswith('venvs/default/main')


def test_env_paths_names():
    p = V.env_paths(os.path.join('X:', 'e', 'venvs', 'p', 'main'))
    assert os.path.basename(p.site_packages) == 'site-packages'
    assert os.path.basename(p.combined) == 'combined.txt'
    assert os.path.basename(p.constraints) == 'constraints.txt'
    assert os.path.basename(p.hash_file) == 'requirements.hash'
    assert os.path.basename(p.lock_file) == 'install.lock'


def test_base_paths_keeps_metadata_and_target_apart():
    # The base runtime is the one env whose install target is not under its metadata dir.
    cache = os.path.join('X:', 'e', 'cache')
    site = os.path.join('X:', 'e', 'lib', 'site-packages')
    p = V.base_paths(cache, site)
    assert p.env_dir == cache
    assert p.site_packages == site
    assert p.constraints == os.path.join(cache, 'constraints.txt')
    assert p.lock_file == os.path.join(cache, 'install.lock')


def test_env_contexts_do_not_share_processed_sets():
    a = V.EnvContext(key='a', paths=V.env_paths('a'), is_overlay=True)
    b = V.EnvContext(key='b', paths=V.env_paths('b'), is_overlay=True)
    a.processed.add('r.txt')
    assert 'r.txt' not in b.processed


# --- hash / combine ---------------------------------------------------------


def _req(tmp_path, name, body):
    f = tmp_path / name
    f.write_text(body, encoding='utf-8')
    return str(f)


def test_requirements_hash_order_independent_and_content_sensitive(tmp_path):
    a = _req(tmp_path, 'a.txt', 'tabulate==0.8.10\n')
    b = _req(tmp_path, 'b.txt', 'six==1.16.0\n')
    assert V.requirements_hash([a, b]) == V.requirements_hash([b, a])
    h1 = V.requirements_hash([a])
    (tmp_path / 'a.txt').write_text('tabulate==0.9.0\n', encoding='utf-8')
    assert V.requirements_hash([a]) != h1


def test_write_combined_concatenates_with_headers(tmp_path):
    a = _req(tmp_path, 'a.txt', 'tabulate==0.8.10\n')
    b = _req(tmp_path, 'b.txt', 'six==1.16.0\n')
    out = str(tmp_path / 'combined.txt')
    V.write_combined([a, b], out)
    text = (tmp_path / 'combined.txt').read_text(encoding='utf-8')
    assert 'tabulate==0.8.10' in text and 'six==1.16.0' in text
    assert text.count('# Source:') == 2


# --- `-r` includes ----------------------------------------------------------


def test_write_combined_absolutizes_relative_includes(tmp_path):
    # uv resolves `-r` against the file holding the line; combined.txt sits elsewhere,
    # so a relative include would be looked for next to it and the compile would fail.
    (tmp_path / 'src').mkdir()
    inner = _req(tmp_path / 'src', 'requirements.other.txt', 'idna==3.18\n')
    outer = _req(tmp_path / 'src', 'requirements.txt', '-r requirements.other.txt\ntabulate==0.9.0\n')
    out = str(tmp_path / 'cache' / 'combined.txt')
    os.makedirs(os.path.dirname(out))

    V.write_combined([outer], out)

    text = open(out, encoding='utf-8').read()
    assert '-r requirements.other.txt' not in text
    assert f'-r {inner.replace(os.sep, "/")}' in text
    assert 'tabulate==0.9.0' in text


def test_write_combined_include_path_has_no_backslashes(tmp_path):
    # A requirements file treats `\` as an escape, so uv reads `C:\x\y.txt` as `C:xy.txt`
    # and reports a missing file. Forward slashes work on every platform.
    (tmp_path / 'src').mkdir()
    _req(tmp_path / 'src', 'other.txt', 'idna==3.18\n')
    outer = _req(tmp_path / 'src', 'requirements.txt', '-r other.txt\n')
    out = str(tmp_path / 'combined.txt')

    V.write_combined([outer], out)

    include_line = next(ln for ln in open(out, encoding='utf-8') if ln.startswith('-r '))
    assert '\\' not in include_line


def test_write_combined_normalizes_absolute_includes_too(tmp_path):
    inner = _req(tmp_path, 'requirements.other.txt', 'idna==3.18\n')
    outer = _req(tmp_path, 'requirements.txt', f'-r {inner}\n')
    out = str(tmp_path / 'combined.txt')
    V.write_combined([outer], out)
    text = open(out, encoding='utf-8').read()
    assert f'-r {inner.replace(os.sep, "/")}' in text


@pytest.mark.parametrize('form', ['-r other.txt', '-rother.txt', '--requirement other.txt', '--requirement=other.txt'])
def test_include_forms_are_all_recognized(tmp_path, form):
    inner = _req(tmp_path, 'other.txt', 'idna==3.18\n')
    outer = _req(tmp_path, 'requirements.txt', f'{form}\n')
    assert V.resolve_includes([outer]) == [os.path.abspath(outer), inner]


def test_resolve_includes_is_transitive_and_cycle_safe(tmp_path):
    c = _req(tmp_path, 'c.txt', 'idna==3.18\n')
    b = _req(tmp_path, 'b.txt', '-r c.txt\n')
    a = _req(tmp_path, 'a.txt', '-r b.txt\n-r a.txt\n')  # self-reference must not loop
    assert V.resolve_includes([a]) == [os.path.abspath(a), os.path.abspath(b), c]


def test_resolve_includes_reports_a_missing_target(tmp_path):
    outer = _req(tmp_path, 'requirements.txt', '-r absent.txt\n')
    with pytest.raises(FileNotFoundError) as excinfo:
        V.resolve_includes([outer])
    # Name the referring file — a uv failure would only name combined.txt.
    assert 'requirements.txt' in str(excinfo.value) and 'absent.txt' in str(excinfo.value)


def test_included_file_participates_in_drift(tmp_path):
    inner = _req(tmp_path, 'inner.txt', 'idna==3.18\n')
    outer = _req(tmp_path, 'requirements.txt', '-r inner.txt\n')
    exe = str(tmp_path)

    plan = V.plan_install(exe, 'p', 'main', [outer])
    V.mark_installed(plan)
    open(plan.paths.constraints, 'w').close()
    assert V.plan_install(exe, 'p', 'main', [outer]).needs_rebuild is False

    open(inner, 'w', encoding='utf-8').write('idna==3.10\n')
    assert V.plan_install(exe, 'p', 'main', [outer]).needs_rebuild is True


# --- install planning -------------------------------------------------------


def test_plan_install_creates_overlay_and_detects_drift(tmp_path):
    exe = str(tmp_path)
    req = _req(tmp_path, 'r.txt', 'tabulate==0.8.10\n')

    plan = V.plan_install(exe, 'proj-guid-aaaa', 'main', [req])
    assert os.path.isdir(plan.paths.site_packages)
    assert plan.needs_rebuild is True
    assert os.path.isfile(plan.paths.combined)

    # simulate a completed install: stored hash + constraints produced by uv
    V.mark_installed(plan)
    open(plan.paths.constraints, 'w').close()
    assert V.plan_install(exe, 'proj-guid-aaaa', 'main', [req]).needs_rebuild is False

    (tmp_path / 'r.txt').write_text('tabulate==0.9.0\n', encoding='utf-8')
    assert V.plan_install(exe, 'proj-guid-aaaa', 'main', [req]).needs_rebuild is True


def test_plan_install_default_env_when_no_project_id(tmp_path):
    plan = V.plan_install(str(tmp_path), None, None, [])
    assert plan.paths.env_dir.replace('\\', '/').endswith('venvs/default/main')
    assert os.path.isdir(plan.paths.site_packages)


# --- uv install argv --------------------------------------------------------


def test_build_install_argv_targets_overlay():
    argv = V.build_install_argv(
        uv_path='uv',
        python_exe='py',
        requirements_path='r.txt',
        target_site='/venvs/p/main/site-packages',
        constraints_path='c.txt',
        excludes_path='ex.txt',
    )
    assert argv[:3] == ['uv', 'pip', 'install']
    assert argv[argv.index('--target') + 1] == '/venvs/p/main/site-packages'
    assert argv[argv.index('-r') + 1] == 'r.txt'
    assert argv[argv.index('-c') + 1] == 'c.txt'
    assert argv[argv.index('--excludes') + 1] == 'ex.txt'
    assert '--no-build-isolation' in argv


def test_build_install_argv_optional_flags_omitted():
    argv = V.build_install_argv('uv', 'py', 'r.txt', '/site')
    assert '-c' not in argv and '--excludes' not in argv


def test_build_install_argv_without_target_is_the_base_install():
    # One builder serves both paths: base is the overlay form minus --target, so a flag
    # can no longer be added to one install path and forgotten in the other.
    overlay = V.build_install_argv('uv', 'py', 'r.txt', '/site', 'c.txt', 'ex.txt')
    base = V.build_install_argv('uv', 'py', 'r.txt', None, 'c.txt', 'ex.txt')
    assert '--target' not in base
    assert base == [a for a in overlay if a not in ('--target', '/site')]


# --- run_scoped_install orchestration ---------------------------------------


def _stub_discover(files):
    def _d(providers):
        _d.called_with = list(providers)
        return files

    _d.called_with = None
    return _d


def test_run_scoped_install_off_is_noop(tmp_path):
    d = _stub_discover([])
    calls = []
    site = V.run_scoped_install(
        str(tmp_path),
        'p',
        'main',
        ['webhook'],
        discover=d,
        compile_and_install=lambda plan: calls.append('install'),
        mode=V.USE_OFF,
    )
    assert site is None
    assert d.called_with is None
    assert calls == []


def test_run_scoped_install_on_installs_and_overlays(tmp_path):
    req = _req(tmp_path, 'r.txt', 'tabulate==0.8.10\n')
    d = _stub_discover([req])
    installed, overlaid = [], []
    site = V.run_scoped_install(
        str(tmp_path),
        'proj-aaaa',
        'main',
        ['webhook', 'detect'],
        discover=d,
        compile_and_install=lambda plan: installed.append(plan.paths.site_packages),
        on_overlay=overlaid.append,
        mode=V.USE_ON,
    )
    assert site.endswith('site-packages')
    assert d.called_with == ['webhook', 'detect']
    assert installed == [site]
    # on_overlay receives the whole layout, so the caller need not re-derive the
    # constraints path from the site-packages path.
    assert [p.site_packages for p in overlaid] == [site]
    assert overlaid[0].constraints == V.env_paths(os.path.dirname(site)).constraints
    assert os.path.isfile(V.env_paths(os.path.dirname(site)).hash_file)


def test_run_scoped_install_skips_install_when_up_to_date(tmp_path):
    req = _req(tmp_path, 'r.txt', 'tabulate==0.8.10\n')
    d = _stub_discover([req])

    def ci(plan):
        open(plan.paths.constraints, 'w').close()
        ci.n += 1

    ci.n = 0
    V.run_scoped_install(str(tmp_path), 'p', 'main', ['x'], discover=d, compile_and_install=ci, mode=V.USE_ON)
    V.run_scoped_install(str(tmp_path), 'p', 'main', ['x'], discover=d, compile_and_install=ci, mode=V.USE_ON)
    assert ci.n == 1


def test_run_scoped_install_auto_needs_isolated_group(tmp_path):
    req = _req(tmp_path, 'r.txt', 'x\n')
    base = dict(discover=_stub_discover([req]), compile_and_install=lambda plan: None, mode=V.USE_AUTO)
    assert V.run_scoped_install(str(tmp_path), 'p', 'main', ['x'], has_isolated_group=False, **base) is None
    assert V.run_scoped_install(str(tmp_path), 'p', 'main', ['x'], has_isolated_group=True, **base) is not None


def test_run_scoped_install_records_the_environment_before_re_raising_a_deferred_error(tmp_path):
    """Restart-required is recorded *then* refused, or the restart repeats the whole build.

    The condition travels back as a returned exception rather than being thrown from inside
    ``compile_and_install``, precisely because ``mark_installed`` lives on this side. Raising
    first would leave the hash unwritten, so the restart the message asks for rebuilds
    everything and the operator watches the fix appear not to take.
    """
    req = _req(tmp_path, 'r.txt', 'tabulate==0.8.10\n')
    refusal = RuntimeError('cv2 is already imported in this process')
    overlaid = []

    def ci(plan):
        open(plan.paths.constraints, 'w').close()
        return refusal

    with pytest.raises(RuntimeError) as raised:
        V.run_scoped_install(
            str(tmp_path),
            'p',
            'main',
            ['x'],
            discover=_stub_discover([req]),
            compile_and_install=ci,
            on_overlay=overlaid.append,
            mode=V.USE_ON,
        )
    assert raised.value is refusal
    paths = V.env_paths(V.env_dir(str(tmp_path), 'p', 'main'))
    assert os.path.isfile(paths.hash_file), 'recorded before the refusal'
    assert overlaid == [], 'the run is refused, so the overlay is never applied'


def test_run_scoped_install_second_start_does_not_rebuild_after_a_deferred_error(tmp_path):
    """The other half of the same rule: having recorded, the restart proceeds without work."""
    req = _req(tmp_path, 'r.txt', 'tabulate==0.8.10\n')
    calls = []

    def ci(plan):
        open(plan.paths.constraints, 'w').close()
        calls.append(1)
        return RuntimeError('restart required') if len(calls) == 1 else None

    kwargs = dict(discover=_stub_discover([req]), compile_and_install=ci, mode=V.USE_ON)
    with pytest.raises(RuntimeError):
        V.run_scoped_install(str(tmp_path), 'p', 'main', ['x'], **kwargs)
    V.run_scoped_install(str(tmp_path), 'p', 'main', ['x'], **kwargs)
    assert calls == [1], 'the second start found a matching hash and did nothing'


def test_a_family_declaration_change_drifts_only_environments_holding_that_family(tmp_path):
    """The declarations live in ``lib/pkg_families/*.py``, where the requirement-file walk never
    looks. Without them in the hash an operator edits a declared version and nothing happens.
    """
    req = _req(tmp_path, 'r.txt', 'tabulate==0.8.10\n')
    paths = V.env_paths(V.env_dir(str(tmp_path), 'p', 'main'))
    os.makedirs(os.path.dirname(paths.constraints), exist_ok=True)

    with open(paths.constraints, 'w', encoding='utf-8') as fh:
        fh.write('tabulate==0.8.10\n')
    without_family = V.plan_install(str(tmp_path), 'p', 'main', [req]).current_hash

    with open(paths.constraints, 'w', encoding='utf-8') as fh:
        fh.write('tabulate==0.8.10\nopencv-python-headless==4.13.0.92\n')
    with_family = V.plan_install(str(tmp_path), 'p', 'main', [req]).current_hash

    assert with_family != without_family
    assert ':' not in without_family, 'a family-free environment keeps its bytes and does not rebuild'


def test_run_scoped_install_skips_when_no_requirements(tmp_path):
    # nothing to scope (source-only / native-only env): must not compile an absent file
    installed = []
    site = V.run_scoped_install(
        str(tmp_path),
        'p',
        'main',
        ['dropper'],
        discover=lambda provs: [],
        compile_and_install=lambda plan: installed.append(1),
        mode=V.USE_ON,
    )
    assert site is None
    assert installed == []


# --- reclamation: purge / delete / list (8.6) -------------------------------


def _make_env(exe_dir, project_id, env_id, files=('a.py', 'pkg/b.py')):
    """Fabricate one installed overlay and return its EnvPaths."""
    paths = V.env_paths(V.env_dir(str(exe_dir), project_id, env_id))
    os.makedirs(paths.site_packages, exist_ok=True)
    for rel in files:
        target = os.path.join(paths.site_packages, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w', encoding='utf-8') as fh:
            fh.write('x')
    for path in (paths.combined, paths.constraints, paths.hash_file):
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('marker')
    return paths


def test_purge_empties_site_packages_and_keeps_the_inputs(tmp_path):
    paths = _make_env(tmp_path, 'proj', 'main')
    assert V.purge_env(str(tmp_path), 'proj', 'main') is True
    assert os.listdir(paths.site_packages) == []
    assert os.path.isfile(paths.combined)
    assert os.path.isfile(paths.constraints)
    assert not os.path.exists(paths.hash_file)


def test_purge_absent_target_is_idempotent_success(tmp_path):
    assert V.purge_env(str(tmp_path), 'nope', 'main') is False


def test_purge_drops_the_hash_before_wiping(tmp_path, monkeypatch):
    # The invariant is invisible on every happy path and is exactly what a refactor reorders:
    # hash-last would leave a half-wiped env still marked installed, which the next run imports.
    paths = _make_env(tmp_path, 'proj', 'main')

    def _boom(*_a, **_k):
        raise OSError('wipe failed midway')

    monkeypatch.setattr(V, '_empty_dir', _boom)
    with pytest.raises(OSError):
        V.purge_env(str(tmp_path), 'proj', 'main')
    assert not os.path.exists(paths.hash_file), 'hash must already be gone when the wipe fails'


def test_delete_env_removes_the_whole_overlay(tmp_path):
    paths = _make_env(tmp_path, 'proj', 'v1')
    assert V.delete_env(str(tmp_path), 'proj', 'v1') is True
    assert not os.path.isdir(paths.env_dir)


def test_delete_project_removes_every_env_and_the_project_dir(tmp_path):
    # 'chain-daa01f80' hashes, so this also covers the non-idempotent short_id path.
    _make_env(tmp_path, 'chain-daa01f80', 'main')
    _make_env(tmp_path, 'chain-daa01f80', 'v-1')
    root = V.resolve_project_dir(str(tmp_path), 'chain-daa01f80')
    assert V.delete_project(str(tmp_path), 'chain-daa01f80') == 2
    assert not os.path.isdir(root), 'operation C deletes the subtree, not just its contents'


def test_delete_project_absent_is_zero(tmp_path):
    assert V.delete_project(str(tmp_path), 'nope') == 0


def test_resolver_takes_an_on_disk_name_literally(tmp_path):
    # 'v-1' hashes (short but not equal to its cleaned form), so the round trip is real: the
    # name list_envs reports must address the same directory the raw id created.
    paths = _make_env(tmp_path, 'proj', 'v-1')
    on_disk = os.path.basename(paths.env_dir)
    assert on_disk != 'v-1', 'this case is pointless unless the id actually hashed'
    assert V.resolve_env_dir(str(tmp_path), 'proj', on_disk) == paths.env_dir
    assert V.resolve_env_dir(str(tmp_path), 'proj', 'v-1') == paths.env_dir


def test_purge_round_trips_through_a_listed_name(tmp_path):
    paths = _make_env(tmp_path, 'proj', 'v-1')
    rows = V.list_envs(str(tmp_path), 'proj')
    assert len(rows) == 1
    assert V.purge_env(str(tmp_path), rows[0]['projectId'], rows[0]['envId']) is True
    assert os.listdir(paths.site_packages) == []


def test_list_envs_reports_installed_and_filters_by_project(tmp_path):
    _make_env(tmp_path, 'p1', 'main')
    _make_env(tmp_path, 'p2', 'main')
    os.remove(V.env_paths(V.env_dir(str(tmp_path), 'p2', 'main')).hash_file)
    assert [r['projectId'] for r in V.list_envs(str(tmp_path))] == ['p1', 'p2']
    assert V.list_envs(str(tmp_path), 'p1')[0]['installed'] is True
    assert V.list_envs(str(tmp_path), 'p2')[0]['installed'] is False


def test_list_envs_skips_a_childless_project_dir(tmp_path):
    # Must agree with delete_project, which removes the directory: otherwise the closing "list
    # shows them gone" is ambiguous between a bug and an empty shell.
    os.makedirs(os.path.join(V.venv_root(str(tmp_path)), 'empty-one'), exist_ok=True)
    assert V.list_envs(str(tmp_path)) == []


def test_list_envs_sizes_are_opt_in(tmp_path):
    _make_env(tmp_path, 'p1', 'main')
    assert 'bytes' not in V.list_envs(str(tmp_path))[0]
    assert V.list_envs(str(tmp_path), sizes=True)[0]['bytes'] > 0


@pytest.mark.skipif(os.name == 'nt', reason='fcntl is POSIX-only')
def test_purge_reports_busy_against_a_foreign_flock(tmp_path):
    # flock and lockf do not see each other on Linux, so the wrong primitive evaporates the gate
    # on exactly one platform, invisibly. Imported inside the test: a module-level `import fcntl`
    # would take this whole file down on Windows.
    import fcntl

    paths = _make_env(tmp_path, 'proj', 'main')
    with open(paths.lock_file, 'a+b') as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(V.EnvBusy):
            V.purge_env(str(tmp_path), 'proj', 'main')
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
