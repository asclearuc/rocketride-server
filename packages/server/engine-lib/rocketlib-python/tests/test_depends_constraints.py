# =============================================================================
# MIT License — Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""What makes ``ensure_constraints`` decide to recompile.

The cache key is one expression, and every input folded into it was added because leaving
it out failed silently — an edit that changes the resolution while the gate reports "up to
date". A test that only checks "a requirement file changed" cannot tell the difference
between the full key and a key that dropped two of its three inputs, which is exactly the
mistake a merge makes: both halves still compile, both still cache, and the environment
quietly stops rebuilding for reasons it used to.

So each test here names one input and proves it can invalidate on its own, plus the two
cases that must NOT rebuild — steady state, and a family-free environment meeting a changed
declaration. The overlay path's equivalent lives in ``test_venv_env.py``
(``plan_install().current_hash``); this file is the base-runtime half, which had none.

``depends`` imports ``engLib``, so these run under the engine interpreter
(``builder server:run-rocketlib-test``) and are skipped under a bare Python.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

import pkg_families

try:
    import depends as D

    _HAVE_ENGLIB = True
except ImportError:  # engLib is built into engine.exe
    _HAVE_ENGLIB = False

pytestmark = pytest.mark.skipif(not _HAVE_ENGLIB, reason='depends needs engLib (engine interpreter)')


@pytest.fixture
def exe_dir(tmp_path, monkeypatch):
    """Point ``depends`` at a throwaway engine directory and return it.

    Only works because ``conftest`` clears the base-environment registry between tests —
    ``_base_env`` caches the context it built from the *first* ``_get_executable_dir`` any
    test in the process asked for.
    """
    monkeypatch.setattr(D, '_get_executable_dir', lambda: str(tmp_path))
    return tmp_path


@pytest.fixture
def compiler(monkeypatch):
    """Stand in for uv: record the calls, and write whatever resolution the test wants back.

    The written file matters as much as the count — ``hash_contribution`` reads the
    *previous* resolution to decide which families this environment contains.
    """
    state = SimpleNamespace(calls=[], resolution='numpy==2.5.1\n')

    def fake_compile(constraints_path):
        state.calls.append(constraints_path)
        with open(constraints_path, 'w', encoding='utf-8') as fh:
            fh.write(state.resolution)

    monkeypatch.setattr(D, '_compile_constraints', fake_compile)
    monkeypatch.setattr(D, 'updateProgress', lambda message: None)
    return state


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


def _bump_the_declared_namespace_version(monkeypatch):
    """Edit the cv2 declaration the only way a test can: swap the registry it comes from."""
    cv2 = pkg_families.family_by_name('cv2')
    moved = pkg_families.Family(
        name=cv2.name,
        import_name=cv2.import_name,
        members=cv2.members,
        namespace_version='9.9.9',
    )
    monkeypatch.setattr(pkg_families, '_registry', lambda: (moved,))


# ---------------------------------------------------------------------------
# the gate itself
# ---------------------------------------------------------------------------


def test_an_untouched_tree_compiles_once_and_then_stays_cached(exe_dir, compiler):
    """The floor. Without it every assertion below is satisfied by a gate that always rebuilds."""
    _write(exe_dir / 'requirements.txt', 'numpy\n')

    first = D.ensure_constraints()
    second = D.ensure_constraints()

    assert first == second
    assert len(compiler.calls) == 1


def test_a_changed_requirement_file_recompiles(exe_dir, compiler):
    req = _write(exe_dir / 'requirements.txt', 'numpy\n')
    D.ensure_constraints()

    req.write_text('numpy\nrequests\n', encoding='utf-8')
    D.ensure_constraints()

    assert len(compiler.calls) == 2


# ---------------------------------------------------------------------------
# input 1: files reached through an include directive
# ---------------------------------------------------------------------------


def test_a_change_behind_an_include_directive_recompiles(exe_dir, compiler):
    """``-r`` shapes the resolution, so it has to be able to invalidate it.

    The file walk never sees ``extra.txt``: it is not matched by any requirements glob, it is
    reached only by following the directive out of a file that is. A key computed over the
    matched files alone therefore holds steady while the resolution changes underneath it —
    an operator edits a pin and the build ignores it.
    """
    extra = _write(exe_dir / 'extra.txt', 'numpy==2.5.1\n')
    _write(exe_dir / 'requirements.txt', '-r extra.txt\n')

    D.ensure_constraints()
    assert len(compiler.calls) == 1

    extra.write_text('numpy==2.4.0\n', encoding='utf-8')
    D.ensure_constraints()

    assert len(compiler.calls) == 2, 'the include is part of the key, or editing it does nothing'


def test_an_include_that_did_not_change_does_not_recompile(exe_dir, compiler):
    """Folding includes in must not cost a rebuild per call — the digest has to be stable."""
    _write(exe_dir / 'extra.txt', 'numpy==2.5.1\n')
    _write(exe_dir / 'requirements.txt', '-r extra.txt\n')

    D.ensure_constraints()
    D.ensure_constraints()

    assert len(compiler.calls) == 1


# ---------------------------------------------------------------------------
# input 2: the package-family declarations
# ---------------------------------------------------------------------------


def test_a_changed_family_declaration_recompiles_an_environment_that_holds_the_family(exe_dir, compiler, monkeypatch):
    """The declarations live in ``lib/pkg_families/``, where the file walk never looks.

    Without this input an operator can edit the declared namespace version and watch nothing
    happen — no recompile, no reinstall, the environment keeps what it had. A remedy that
    appears to do nothing is worse than no remedy.
    """
    _write(exe_dir / 'requirements.txt', 'opencv-python-headless\n')
    compiler.resolution = 'opencv-python-headless==4.13.0.92\n'

    D.ensure_constraints()
    assert len(compiler.calls) == 1

    _bump_the_declared_namespace_version(monkeypatch)
    D.ensure_constraints()

    assert len(compiler.calls) == 2, 'the declarations are part of the key, or the remedy is silent'


def test_a_family_free_environment_does_not_recompile_for_a_declaration_change(exe_dir, compiler, monkeypatch):
    """The load-bearing half of ``combine_hash``: no family present means the stored bytes
    stay identical, so an installation full of family-free environments does not rebuild
    once each merely because this mechanism exists.
    """
    _write(exe_dir / 'requirements.txt', 'numpy\n')
    compiler.resolution = 'numpy==2.5.1\n'

    D.ensure_constraints()
    assert len(compiler.calls) == 1

    _bump_the_declared_namespace_version(monkeypatch)
    D.ensure_constraints()

    assert len(compiler.calls) == 1, 'an environment with no family member must not rebuild'


# ---------------------------------------------------------------------------
# the rest of the gate
# ---------------------------------------------------------------------------


def test_a_deleted_constraints_file_recompiles_even_when_the_hash_agrees(exe_dir, compiler):
    """The stored hash describes the inputs, not the output — the gate has to check both."""
    _write(exe_dir / 'requirements.txt', 'numpy\n')
    constraints = D.ensure_constraints()

    os.remove(constraints)
    D.ensure_constraints()

    assert len(compiler.calls) == 2


def test_no_requirement_files_means_no_compile(exe_dir, compiler):
    D.ensure_constraints()

    assert compiler.calls == []
