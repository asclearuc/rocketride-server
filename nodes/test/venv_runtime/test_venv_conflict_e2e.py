# =============================================================================
# MIT License — Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""End-to-end acceptance for design §8.3: two unsatisfiable pins, one per environment.

`webhook(main) -> [v1: vtest_alpha] -> [v2: vtest_beta] -> response(main)`. Both fixtures
pin `tabulate` at mutually unsatisfiable versions and report `<name>=<version>@<file>` into
the text lane, so one payload carries both and the assertion is about what the nodes
*imported*, not about which files happen to sit on disk.

Three things make this the first automated check of the venv runtime rather than another
unit test:

* it runs under **`auto`** — the mode `builder test` uses — because the document carries
  isolated groups and 8.7B made `auto` scope on that signal. Until the fixtures moved out
  of the startup glob's reach this acceptance was structurally `=1`-only;
* it is the first check in the suite that spawns venv children at all;
* it also discharges §8.3's other owed bullet, "a node's own pin beats the base", by
  *establishing* the base side rather than assuming it — see `base_holds_other_version`.

Slow, index-dependent and engine-spawning, unlike its neighbours in this directory, which
are in-process unit tests with stubbed imports. It skips visibly when `uv` or the package
index is unavailable; it must never vanish silently.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

# The two fixture pins, and a THIRD version for base. A third on purpose: were base to hold
# 0.8.10, an overlay that silently fell through to base would still report 0.8.10 and this
# test would pass while measuring nothing.
ALPHA_PIN = '0.8.10'
BETA_PIN = '0.9.0'
BASE_PIN = '0.8.9'

pytestmark = [pytest.mark.asyncio, pytest.mark.xdist_group('venv_conflict_e2e')]


def _engine_lib():
    """Load `depends`/`venv_env` **from their files**, deliberately bypassing `sys.modules`.

    A plain `import depends` is not safe here. Node tests stub `depends` (nodes do
    `from depends import depends`), and under xdist this test shares a worker with them, so
    `sys.modules['depends']` can already be a `MagicMock`. That failure is silent and vicious:
    `_uv_available()` returns a truthy mock so the skip never fires, `_uv_abs_path()` returns a
    mock, and `subprocess.run` hands it to `CreateProcess`, which reports
    `FileNotFoundError: [WinError 2]` — a message that points at uv rather than at the stub.
    Measured exactly that way in a full `nodes:test` run.
    """
    import importlib.util

    lib_dir = os.path.join(os.path.dirname(sys.executable), 'lib')
    loaded = []
    for name in ('venv_env', 'depends'):  # depends imports venv_env, so seed that first
        path = os.path.join(lib_dir, f'{name}.py')
        if not os.path.isfile(path):
            pytest.skip(f'{name}.py not found beside the engine ({path})')
        spec = importlib.util.spec_from_file_location(f'_e2e_{name}', path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        try:
            spec.loader.exec_module(mod)
        except ImportError:  # engLib is built into engine.exe; absent under a bare Python
            pytest.skip(f'{name} needs the engine interpreter')
        loaded.append(mod)
    venv_env_mod, depends_mod = loaded
    # Cheap proof we got real modules and not a stub that slipped through.
    assert isinstance(depends_mod._uv_abs_path(), str), 'depends is stubbed, not the real module'
    return depends_mod, venv_env_mod


def _installed_version(site_dir: str, package: str):
    """Version of `package` under `site_dir`, read from its *.dist-info, or None."""
    from glob import glob

    for path in glob(os.path.join(site_dir, f'{package}-*.dist-info')):
        return os.path.basename(path)[: -len('.dist-info')].split('-', 1)[1]
    return None


def _venv(container_id, members, name=None):
    """An isolated virtual-environment container, as the canvas nests one."""
    return {
        'id': container_id,
        'provider': 'default',
        'config': {
            'environment': {'name': name or container_id, 'isolated': True},
            'pipeline': {'components': members},
        },
    }


def _document():
    """Source and response stay in main deliberately (§4.13).

    A document whose nodes ALL sit in venvs is either rejected (source-in-venv) or specified
    to collapse to a single process with no bridge — an unimplemented path. Either failure
    would read as a scoping bug, so the shape avoids both.

    The project id is FIXED rather than random: the three overlays are then keyed once and
    reused by every later gate run instead of growing a directory per run.
    """
    return {
        'project_id': 'nodes-test-venv-conflict-e2e',
        'source': 'webhook_1',
        'components': [
            {'id': 'webhook_1', 'provider': 'webhook', 'config': {'mode': 'Source'}},
            _venv(
                'v1',
                [
                    {
                        'id': 'alpha_1',
                        'provider': 'vtest_alpha',
                        'config': {},
                        'input': [{'lane': 'text', 'from': 'webhook_1'}],
                    }
                ],
            ),
            _venv(
                'v2',
                [
                    {
                        'id': 'beta_1',
                        'provider': 'vtest_beta',
                        'config': {},
                        'input': [{'lane': 'text', 'from': 'alpha_1'}],
                    }
                ],
            ),
            {
                'id': 'response_1',
                'provider': 'response',
                'config': {},
                'input': [{'lane': 'text', 'from': 'beta_1'}],
            },
        ],
    }


@pytest.fixture
def base_holds_other_version():
    """Put `tabulate==0.8.9` in the BASE runtime, and take it out again.

    §8.3's written recipe for "a node's own pin beats the base" said to run a `vtest_beta`
    pipeline under `=0` so the legacy glob installed the pin into base. That stopped working
    when the fixtures moved out of the glob's reach — and independently, base holds no
    `tabulate` at all, so the check would have compared base against nothing.

    Mutating the base runtime is the one thing a suite should be reluctant to do. It is
    acceptable *here* for a specific reason: §8.2 chose `tabulate` precisely because nothing
    in the SDK or engine runtime uses it, so even a leaked leftover is an unused
    pure-Python package at a version nothing pins.
    """
    depends, _ = _engine_lib()
    if not depends._uv_available():
        pytest.skip('uv is not bootstrapped in this installation')

    base_site = depends._get_site_packages()
    if _installed_version(base_site, 'tabulate') is not None:
        pytest.skip('base already holds a tabulate; refusing to disturb it')

    uv, exe_dir = depends._uv_abs_path(), depends._get_executable_dir()
    install = subprocess.run(
        [
            uv,
            'pip',
            'install',
            '--python',
            sys.executable,
            f'tabulate=={BASE_PIN}',
            '--index-strategy',
            'unsafe-best-match',
            '--no-build-isolation',
        ],
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        cwd=exe_dir,
        check=False,
    )
    if install.returncode != 0:
        output = (install.stderr or '') + (install.stdout or '')
        if any(m in output.lower() for m in ('network', 'dns', 'timed out', 'failed to fetch', 'no such host')):
            pytest.skip(f'package index unreachable: {output.strip()[:200]}')
        pytest.fail(f'could not stage the base version: {output.strip()[:400]}')

    try:
        yield base_site
    finally:
        subprocess.run(
            [uv, 'pip', 'uninstall', '--python', sys.executable, 'tabulate'],
            capture_output=True,
            text=True,
            cwd=exe_dir,
            check=False,
        )


async def test_conflicting_pins_coexist_and_beat_the_base(client, base_holds_other_version):
    """The headline proof, under the mode everyone actually runs."""
    _, venv_env = _engine_lib()
    base_site = base_holds_other_version
    pipeline = _document()

    # `use_existing` + terminate-in-finally, because the fixed project id is a fixed TOKEN
    # (sha256 over project_id/source), and `ttl=0` leaves the task resident: a second run would
    # otherwise be refused with "Pipeline is already running." Measured on a back-to-back run.
    started = await client.use(pipeline=pipeline, ttl=0, use_existing=True)
    token = started['token'] if isinstance(started, dict) else started
    try:
        result = await client.send(token, 'hello', mimetype='text/plain')
    finally:
        try:
            await client.terminate(token)
        except Exception:
            pass  # best-effort: a lingering task must not mask the assertion below
    payload = json.dumps(result, default=str)

    # 1 + 2: each node imported ITS pin, and from its own overlay. The differing file shapes
    # (0.8.10 is a single module, 0.9.0 a package) corroborate two distributions rather than
    # one reported twice.
    assert f'alpha={ALPHA_PIN}@' in payload, payload
    assert f'beta={BETA_PIN}@' in payload, payload

    exe_dir = os.path.dirname(sys.executable)
    proj = os.path.join(venv_env.venv_root(exe_dir), venv_env.short_id(pipeline['project_id']))
    for env_id, pin in (('v1', ALPHA_PIN), ('v2', BETA_PIN)):
        site = venv_env.env_paths(os.path.join(proj, env_id)).site_packages
        assert _installed_version(site, 'tabulate') == pin, f'{env_id} overlay should hold {pin}'

    # 3: scoping, not mere separation — main never needed tabulate and never got it.
    main_site = venv_env.env_paths(os.path.join(proj, 'main')).site_packages
    assert _installed_version(main_site, 'tabulate') is None, 'main overlay must hold no tabulate'

    # 4: nothing wrote through to base. This is the assertion the third version exists for.
    assert _installed_version(base_site, 'tabulate') == BASE_PIN, 'the base runtime must be untouched'
