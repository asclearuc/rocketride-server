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


def test_scoping_enabled_semantics():
    assert V.scoping_enabled(V.USE_OFF, has_isolated_group=True) is False
    assert V.scoping_enabled(V.USE_ON, has_isolated_group=False) is True
    assert V.scoping_enabled(V.USE_AUTO, has_isolated_group=True) is True
    assert V.scoping_enabled(V.USE_AUTO, has_isolated_group=False) is False


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
