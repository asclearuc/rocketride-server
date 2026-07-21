# =============================================================================
# MIT License — Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""Acceptance for the headline promise: an environment's pin beats the base runtime.

Two levels, both automated and both talking to the real ``uv``:

* the **mechanism** — ``uv --target`` does not treat the base runtime as satisfying,
  which is the single property everything else rests on and the one that would break
  silently on a uv upgrade;
* the **result** — a node's requirement set is discovered, compiled and installed into
  its own overlay at its own version, while the base runtime is left alone.

The end-to-end level (a real pipeline, driven through a running server) is a manual
procedure until the ``vtest_*`` fixtures are staged into ``dist/server/nodes`` by the
build; the steps are written down in design §8.3.

These run under the engine interpreter (``builder server:run-rocketlib-test``) and need
a package index; they skip rather than fail when either is unavailable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from glob import glob

import pytest

import ast_deps
import venv_env as V

try:
    import depends as D

    _HAVE_ENGLIB = True
except ImportError:  # engLib is built into engine.exe
    _HAVE_ENGLIB = False

pytestmark = pytest.mark.skipif(not _HAVE_ENGLIB, reason='depends needs engLib (engine interpreter)')

# The fixture node pins this exact version; the value is the assertion, so keep it
# in lockstep with nodes/test/fixtures/nodes/vtest_alpha/requirements.txt.
_ALPHA_PIN = '0.8.10'


def _repo_root() -> str:
    """Repository root, derived from this file rather than from the cwd."""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), *([os.pardir] * 5)))


def _fixtures_nodes_src() -> str:
    """Node source root holding the ``vtest_*`` fixtures (``<root>/nodes/<name>/``)."""
    return os.path.join(_repo_root(), 'nodes', 'test', 'fixtures')


def _require_uv():
    if not D._uv_available():
        pytest.skip('uv is not bootstrapped in this installation')


def _skip_if_offline(result: subprocess.CompletedProcess):
    """Skip when uv could not reach an index — offline is not a failing assertion."""
    output = (result.stderr or '') + (result.stdout or '')
    if result.returncode != 0 and any(
        marker in output.lower() for marker in ('network', 'dns', 'timed out', 'failed to fetch', 'no such host')
    ):
        pytest.skip(f'package index unreachable: {output.strip()[:200]}')


def _installed_version(dist_info_dir: str, package: str) -> str | None:
    """Version of ``package`` installed in ``dist_info_dir``, read from ``*.dist-info``."""
    for path in glob(os.path.join(dist_info_dir, f'{package}-*.dist-info')):
        name = os.path.basename(path)[: -len('.dist-info')]
        return name.split('-', 1)[1]
    return None


# --- level 1: the mechanism --------------------------------------------------


def test_target_install_does_not_treat_base_as_satisfying(tmp_path):
    """``uv --target`` plans a version the base already holds at another version.

    This is what lets a node pin its own dependency: were the base counted as
    satisfying, the overlay would stay empty and the node would silently run against
    the base version instead of the one it asked for.
    """
    _require_uv()
    installed = _installed_version(D._get_site_packages(), 'requests')
    if not installed:
        pytest.skip('requests is not installed in the base runtime, nothing to shadow')
    wanted = '2.32.3' if installed != '2.32.3' else '2.31.0'

    result = subprocess.run(
        [
            D._uv_abs_path(),
            'pip',
            'install',
            '--python',
            sys.executable,
            f'requests=={wanted}',
            '--target',
            str(tmp_path / 'overlay'),
            '--dry-run',
            '--no-color',
            '--index-strategy',
            'unsafe-best-match',
            '--no-build-isolation',
        ],
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        check=False,
        cwd=D._get_executable_dir(),
    )
    _skip_if_offline(result)
    output = result.stderr + result.stdout
    assert result.returncode == 0, output
    assert f'+ requests=={wanted}' in output, f'base {installed} was treated as satisfying:\n{output}'


# --- level 2: the result -----------------------------------------------------


@pytest.fixture
def alpha_requirements():
    """The requirement set the AST walk reaches from the ``vtest_alpha`` fixture."""
    nodes_src = _fixtures_nodes_src()
    if not os.path.isdir(os.path.join(nodes_src, 'nodes', 'vtest_alpha')):
        pytest.skip('vtest_alpha fixture is not present in this checkout')
    found = ast_deps.discover_for_providers(
        ['vtest_alpha'],
        nodes_src=nodes_src,
        ai_src=os.path.join(_repo_root(), 'packages', 'ai', 'src'),
    )
    assert found.unresolved_providers == []
    return found.requirement_files


def test_discovery_reaches_only_the_nodes_own_requirements(alpha_requirements):
    """Scoping starts from the AST-reachable set, not from a glob of the tree."""
    assert [os.path.basename(p) for p in alpha_requirements] == ['requirements.txt']
    assert 'vtest_alpha' in alpha_requirements[0].replace('\\', '/')


@pytest.fixture
def acceptance_env():
    """An overlay under the engine's own ``venvs/``, removed afterwards.

    Not a ``tmp_path``: overlays live beside the executable by construction, and the
    install passes ``-c`` as a path relative to the executable directory (uv splits the
    value on whitespace, #1256), which cannot be expressed across drives.
    """
    exe_dir = D._get_executable_dir()
    project_id = 'rocketlib-acceptance'
    yield exe_dir, project_id
    shutil.rmtree(os.path.join(V.venv_root(exe_dir), V.short_id(project_id)), ignore_errors=True)


@pytest.mark.timeout(300)
def test_environment_installs_its_own_pin_and_leaves_base_alone(acceptance_env, alpha_requirements):
    """The node's pin lands in its overlay; the base runtime is not touched.

    Drives the real machinery — plan, compile, install — against the fixture's own
    requirement file, so it fails if scoping stops resolving per environment.
    """
    _require_uv()
    exe_dir, project_id = acceptance_env
    base_site = D._get_site_packages()
    base_before = _installed_version(base_site, 'tabulate')

    plan = V.plan_install(exe_dir, project_id, 'main', alpha_requirements)
    assert plan.needs_rebuild is True

    try:
        D._compile_constraints_at(plan.paths.combined, plan.paths.constraints)
    except RuntimeError as exc:  # offline, or an index that refuses us
        pytest.skip(f'constraints compile unavailable: {exc}')

    # The environment resolves from its own combined file alone — no global base.
    constraints = open(plan.paths.constraints, encoding='utf-8').read()
    assert f'tabulate=={_ALPHA_PIN}' in constraints

    D._install_target(plan.paths.combined, plan.paths.constraints, plan.paths.site_packages)

    assert _installed_version(plan.paths.site_packages, 'tabulate') == _ALPHA_PIN
    assert _installed_version(base_site, 'tabulate') == base_before, 'the scoped install must not touch base'


def test_overlay_precedes_the_base_runtime_on_sys_path(tmp_path, monkeypatch):
    """An installed overlay wins at import time, which is what "beats base" means."""
    monkeypatch.delenv('ROCKETRIDE_MOCK', raising=False)
    monkeypatch.setattr(D, '_inserted_overlay', None)
    base_site = D._get_site_packages()
    monkeypatch.setattr(D.sys, 'path', ['/some/framework/dir', base_site])
    overlay = str(tmp_path / 'venvs' / 'acc' / 'main' / 'site-packages')

    D._apply_overlay_path(overlay)

    assert D.sys.path.index(overlay) < D.sys.path.index(base_site)
