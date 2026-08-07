# =============================================================================
# MIT License — Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""How ``depends`` wires the package families in.

The rules asserted here are the ones that fail *silently* when implemented the obvious way:
a trigger that can never fire, an early return that never fires again, an exclusions file two
callers fight over, and a refusal raised before the environment was recorded.

``depends`` imports ``engLib``, so these run under the engine interpreter
(``builder server:run-rocketlib-test``) and are skipped under a bare Python.
"""

from __future__ import annotations

import os

import pytest

import pkg_families

try:
    import depends as D

    _HAVE_ENGLIB = True
except ImportError:  # engLib is built into engine.exe
    _HAVE_ENGLIB = False

pytestmark = pytest.mark.skipif(not _HAVE_ENGLIB, reason='depends needs engLib (engine interpreter)')

CV2_MEMBERS = ('opencv-python-headless', 'opencv-python', 'opencv-contrib-python-headless', 'opencv-contrib-python')


def _constraints(tmp_path, text):
    path = tmp_path / 'constraints.txt'
    path.write_text(text, encoding='utf-8')
    return str(path)


def _read(path):
    with open(path, 'r', encoding='utf-8') as fh:
        return fh.read()


class _NoopPopen:
    """A ``uv`` that succeeds without doing anything, for the paths that must reach past it."""

    returncode = 0

    def __init__(self, *args, **kwargs):
        self.stdout = iter(())

    def wait(self):
        return 0


# ---------------------------------------------------------------------------
# the excludes file
# ---------------------------------------------------------------------------


def test_two_exclusion_sets_get_two_paths_and_the_same_set_gets_one():
    """The content stopped being a constant, and two sets now have to be alive at the same
    moment — the trigger's dry-run wants the base set while the install wants the larger one.
    One rewritten path would have them overwrite each other, and under the base runtime the
    varying axis is the *call*, so a per-environment path would fix nothing.
    """
    base = D._write_excludes_file()
    again = D._write_excludes_file()
    with_family = D._write_excludes_file(CV2_MEMBERS)

    assert base == again, 'same bytes, same path — idempotent and race-free'
    assert base != with_family
    assert 'opencv-python-headless' not in _read(base)
    assert 'opencv-python-headless' in _read(with_family)
    assert 'uv' in _read(base)


def test_the_excludes_file_is_content_addressed_not_rewritten():
    """Two callers computing different exclusions must not be able to clobber one another."""
    first = D._write_excludes_file(('alpha',))
    second = D._write_excludes_file(('beta',))
    assert first != second
    assert 'alpha' in _read(first) and 'beta' not in _read(first)
    assert 'beta' in _read(second) and 'alpha' not in _read(second)


# ---------------------------------------------------------------------------
# the trigger, and what it must not be given
# ---------------------------------------------------------------------------


def test_the_dry_run_is_given_the_base_exclusion_set_and_not_the_family_one(monkeypatch, tmp_path):
    """The single easiest way to implement the whole increment wrongly and still see green.

    Hand the dry-run the family exclusions and it resolves *without* the members, so its list
    contains none of them, the trigger concludes the family is not in play, the ordered passes
    never run — and ``cv2`` disappears from every environment as an ImportError rather than a
    build failure.
    """
    seen = {}

    def fake_dry_run(requirements_path, constraints_path, excludes_path):
        seen['content'] = _read(excludes_path)
        return []

    monkeypatch.setattr(D, '_install_dry_run', fake_dry_run)
    monkeypatch.setattr(D, '_family_work', lambda *a, **k: [])

    requirements = tmp_path / 'r.txt'
    requirements.write_text('numpy\n', encoding='utf-8')
    D._install_requirements_inner(str(requirements), _constraints(tmp_path, 'numpy==2.5.1\n'))

    assert 'uv' in seen['content']
    for member in CV2_MEMBERS:
        assert member not in seen['content']


def test_a_dry_run_naming_only_family_members_still_counts_as_nothing_to_do(monkeypatch, tmp_path):
    """A member's presence in that list is not work the caller owes — it is the family's
    business, answered from the target's ``*.dist-info``. Without the subtraction a member
    excluded *by design* reads as permanently missing and every call reinstalls the world.
    """
    monkeypatch.setattr(D, '_install_dry_run', lambda *a, **k: ['opencv-python-headless'])
    monkeypatch.setattr(D, '_family_work', lambda *a, **k: [])
    ran = []
    monkeypatch.setattr(D.subprocess, 'Popen', lambda *a, **k: ran.append(a) or (_ for _ in ()).throw(AssertionError))

    requirements = tmp_path / 'r.txt'
    requirements.write_text('surya-ocr\n', encoding='utf-8')
    D._install_requirements_inner(str(requirements), _constraints(tmp_path, 'opencv-python-headless==4.13.0.92\n'))

    assert ran == [], 'returned at the gate instead of running a full install'


def test_a_call_touching_no_member_does_not_trigger_the_family_step(tmp_path):
    """Keeps opencv out of the ``ai/requirements.txt`` bootstrap. Detection answers "which
    family is this"; the dry-run answers "is it in play right now". Conflating them drags every
    opencv wheel the installation resolves into a package that never asked for one.
    """
    constraints = _constraints(tmp_path, 'opencv-python-headless==4.13.0.92\nnumpy==2.5.1\n')
    assert D._family_work(constraints, str(tmp_path), trigger=['numpy']) == []
    assert [w.family.name for w in D._family_work(constraints, str(tmp_path), trigger=['opencv-python-headless'])] == [
        'cv2'
    ]


def test_presence_in_the_resolution_is_the_trigger_where_there_is_no_dry_run(tmp_path):
    """``_install_target`` installs the whole combined file in one go, so a family present in
    the resolution *is* by construction a family being installed. An implementer looking for a
    dry-run on that path will not find one; that is not an omission to fix.
    """
    constraints = _constraints(tmp_path, 'opencv-python-headless==4.13.0.92\n')
    assert [w.family.name for w in D._family_work(constraints, str(tmp_path))] == ['cv2']


def test_family_work_reports_no_passes_when_the_target_already_holds_the_version(tmp_path):
    site = tmp_path / 'site'
    (site / 'opencv_python_headless-4.13.0.92.dist-info').mkdir(parents=True)
    constraints = _constraints(tmp_path, 'opencv-python-headless==4.13.0.92\n')
    work = D._family_work(constraints, str(site))
    assert [w.version for w in work] == ['4.13.0.92']
    assert work[0].passes == ()


# ---------------------------------------------------------------------------
# the already-imported refusal
# ---------------------------------------------------------------------------


def _work(version, passes=()):
    family = pkg_families.family_by_name('cv2')
    return D._FamilyWork(family=family, version=version, members=family.members[:1], passes=passes)


def test_a_loaded_namespace_about_to_be_rewritten_demands_a_restart(monkeypatch):
    """On Linux the write *succeeds* and the running process keeps serving the old module while
    the environment reports the new one — the silent half of the problem, and the worse one.
    """
    monkeypatch.setitem(D.sys.modules, 'cv2', type('M', (), {'__version__': '4.13.0.92'})())
    passes = (pkg_families.InstallPass(dist='opencv-python-headless', version='4.11.0.86'),)
    assert D._shadowing(_work('4.11.0.86', passes)) is not None


def test_a_loaded_namespace_at_another_version_demands_a_restart_with_nothing_to_install(monkeypatch):
    """Shadowing: base aligns over the union of every requirement file, an overlay over its own
    consumers, so the two legitimately differ — and a ``sys.path`` insert does not re-import
    what is already loaded. The environment is right and the *process* is wrong, which is the
    one case measurement cannot catch.
    """
    monkeypatch.setitem(D.sys.modules, 'cv2', type('M', (), {'__version__': '4.13.0.92'})())
    assert D._shadowing(_work('4.11.0.86')) is not None


def test_an_agreeing_loaded_namespace_with_nothing_to_install_is_not_a_refusal(monkeypatch):
    monkeypatch.setitem(D.sys.modules, 'cv2', type('M', (), {'__version__': '4.13.0.92'})())
    assert D._shadowing(_work('4.13.0.92')) is None


def test_an_unimported_namespace_is_never_a_refusal(monkeypatch):
    monkeypatch.delitem(D.sys.modules, 'cv2', raising=False)
    passes = (pkg_families.InstallPass(dist='opencv-python-headless', version='4.11.0.86'),)
    assert D._shadowing(_work('4.11.0.86', passes)) is None


def test_the_base_path_raises_restart_required_directly(monkeypatch, tmp_path):
    """The base runtime has no per-install bookkeeping to protect — ``ensure_constraints`` wrote
    its hash back at compile time — so there is no ``mark_installed`` to precede here.
    """
    monkeypatch.setattr(D, '_install_dry_run', lambda *a, **k: ['numpy'])
    monkeypatch.setattr(D, '_family_work', lambda *a, **k: [])
    monkeypatch.setattr(D, '_handle_families', lambda *a, **k: D.RestartRequired('cv2 shadowed'))
    monkeypatch.setattr(D.subprocess, 'Popen', _NoopPopen)
    monkeypatch.setattr(D, '_get_executable_dir', lambda: str(tmp_path))

    requirements = tmp_path / 'r.txt'
    requirements.write_text('numpy\n', encoding='utf-8')
    with pytest.raises(D.RestartRequired):
        D._install_requirements_inner(str(requirements), _constraints(tmp_path, 'numpy==2.5.1\n'))


def test_shadowing_is_checked_even_when_there_is_nothing_to_do(monkeypatch, tmp_path):
    """Found by writing this test, and it was a real hole. Behind the "is there work" gate the
    check would never fire in the shape that matters most: an overlay whose hash matched was
    never rebuilt and a satisfied ``depends()`` returns early, so the pipeline would run
    silently on the build the parent had already loaded.
    """
    monkeypatch.setitem(D.sys.modules, 'cv2', type('M', (), {'__version__': '4.13.0.92'})())
    monkeypatch.setattr(D, '_install_dry_run', lambda *a, **k: [])
    monkeypatch.setattr(D, '_family_work', lambda *a, **k: [])

    requirements = tmp_path / 'r.txt'
    requirements.write_text('numpy\n', encoding='utf-8')
    constraints = _constraints(tmp_path, 'opencv-python-headless==4.11.0.86\n')

    with pytest.raises(D.RestartRequired) as raised:
        D._install_requirements_inner(str(requirements), constraints)
    assert '4.13.0.92' in str(raised.value) and '4.11.0.86' in str(raised.value)


def test_the_shadowing_check_costs_nothing_when_the_namespace_is_not_loaded(monkeypatch, tmp_path):
    """A ``sys.modules`` lookup per registered family; the resolution is read only past that.
    ``builder nodes:test`` makes a great many ``depends()`` calls, and this one is on all of them.
    """
    monkeypatch.delitem(D.sys.modules, 'cv2', raising=False)
    reads = []
    monkeypatch.setattr(D, '_read_resolution', lambda path: reads.append(path) or {})
    assert D._shadowing_check(str(tmp_path / 'absent.txt')) is None
    assert reads == []


def test_the_shadowing_check_is_silent_when_the_environment_agrees(monkeypatch, tmp_path):
    monkeypatch.setitem(D.sys.modules, 'cv2', type('M', (), {'__version__': '4.11.0.86'})())
    assert D._shadowing_check(_constraints(tmp_path, 'opencv-python-headless==4.11.0.86\n')) is None


def test_the_shadowing_check_is_silent_when_this_environment_has_no_such_family(monkeypatch, tmp_path):
    """A loaded ``cv2`` says nothing about an environment that does not contain opencv at all."""
    monkeypatch.setitem(D.sys.modules, 'cv2', type('M', (), {'__version__': '4.13.0.92'})())
    assert D._shadowing_check(_constraints(tmp_path, 'numpy==2.5.1\n')) is None


# ---------------------------------------------------------------------------
# the ordered install argv
# ---------------------------------------------------------------------------


def test_the_ordered_passes_go_through_the_shared_argv_builder(monkeypatch, tmp_path):
    """Not a hand-rolled argv: that builder exists because while there were two ways to
    construct an install command, a flag added to one silently diverged from the other. The
    ``-c`` is load-bearing here — the compile emits the index URLs into the constraints file,
    and a pass installing by explicit spec has no other source of them.
    """
    captured = []

    class _Result:
        returncode = 0
        stdout = ''
        stderr = ''

    monkeypatch.setattr(D.subprocess, 'run', lambda argv, **kw: captured.append(argv) or _Result())
    # Same drive as the constraints file: uv splits -c on whitespace so the code passes it
    # relative to the subprocess cwd, and os.path.relpath cannot cross drives on Windows.
    monkeypatch.setattr(D, '_get_executable_dir', lambda: str(tmp_path))
    constraints = _constraints(tmp_path, 'opencv-python-headless==4.13.0.92\n')

    work = _work(
        '4.13.0.92',
        (
            pkg_families.InstallPass(dist='opencv-python-headless', version='4.13.0.92'),
            pkg_families.InstallPass(dist='opencv-contrib-python', version='4.13.0.92', force_reinstall=True),
        ),
    )
    D._run_family_passes(work, constraints, None)

    assert len(captured) == 2
    assert 'opencv-python-headless==4.13.0.92' in captured[0]
    assert '--reinstall-package' not in captured[0]
    assert 'opencv-contrib-python==4.13.0.92' in captured[1]
    assert captured[1][captured[1].index('--reinstall-package') + 1] == 'opencv-contrib-python'
    for argv in captured:
        assert '-r' not in argv, 'explicit specs, not a throwaway requirements file'
        assert '--excludes' in argv


def test_an_ordered_pass_is_never_given_its_own_family_to_exclude(monkeypatch, tmp_path):
    """Measured on the live engine, because the failure is silent. ``--excludes`` excludes from
    *resolution*, so a pass handed its family's set drops the member it was asked to install:
    every pass logs success, nothing lands, and ``import cv2`` fails afterwards with the family
    machinery reporting a clean build.
    """
    captured = []

    class _Result:
        returncode = 0
        stdout = ''
        stderr = ''

    monkeypatch.setattr(D.subprocess, 'run', lambda argv, **kw: captured.append(argv) or _Result())
    monkeypatch.setattr(D, '_get_executable_dir', lambda: str(tmp_path))
    constraints = _constraints(tmp_path, 'opencv-python-headless==4.13.0.92\n')

    work = _work('4.13.0.92', (pkg_families.InstallPass(dist='opencv-python-headless', version='4.13.0.92'),))
    D._run_family_passes(work, constraints, None)

    argv = captured[0]
    excludes = _read(os.path.join(str(tmp_path), argv[argv.index('--excludes') + 1]))
    assert 'uv' in excludes, 'the base set is still needed'
    for member in CV2_MEMBERS:
        assert member not in excludes


# ---------------------------------------------------------------------------
# the messages, which are the feature
# ---------------------------------------------------------------------------


def test_a_pass_two_failure_naming_a_member_is_reported_as_a_namespace_conflict():
    family = pkg_families.family_by_name('cv2')
    resolved = {'opencv-python-headless': '4.11.0.86', 'opencv-python': '4.13.0.92'}
    annotations = {'opencv-python-headless': ('surya-ocr',)}
    failure = D.CompileFailed('because opencv-python-headless==4.11.0.86 and you require opencv-python-headless==4.13')

    raised = D._alignment_failure([(family, '4.11.0.86', family.members)], resolved, annotations, failure)

    text = str(raised)
    assert 'cv2' in text
    assert '4.11.0.86' in text
    assert 'surya-ocr' in text, 'attribution comes from the # via annotation, not from a guess'
    assert 'Virtual Environment container' in text


def test_a_pass_two_failure_naming_no_member_stays_a_plain_compile_failure():
    """Sending a user to build a container over an unreachable index would be worse than a
    generic error, so the message checks before it claims.
    """
    family = pkg_families.family_by_name('cv2')
    failure = D.CompileFailed('failed to fetch https://pypi.example/simple: connection refused')
    raised = D._alignment_failure([(family, '4.11.0.86', family.members)], {}, {}, failure)
    assert raised is failure


def test_a_missing_declared_version_reports_the_declaration_not_a_conflict():
    """Nobody's consumers disagree — the authored number is wrong, exactly as onnxruntime
    1.20.1 was withdrawn for the -gpu build. Getting this backwards sends an operator to split
    a pipeline over a number they could change in one line.
    """
    from pkg_families.onnxruntime import ONNXRUNTIME

    work = D._FamilyWork(family=ONNXRUNTIME, version='1.22.0', members=ONNXRUNTIME.members, passes=())
    text = D._family_install_message(work, 'onnxruntime-gpu==1.22.0', 'no matching distribution')

    assert 'declared' in text
    assert 'pkg_families' in text
    assert 'container' not in text.lower()


def test_an_alignment_that_moved_a_member_is_logged_with_what_imposed_it(monkeypatch):
    """Not a failure — but a user whose doctr quietly rides Surya's version should be able to
    see why and decide to split.
    """
    said = []
    monkeypatch.setattr(D, 'monitorStatus', said.append)
    family = pkg_families.family_by_name('cv2')
    resolved = {'opencv-python-headless': '4.11.0.86', 'opencv-python': '4.13.0.92'}
    annotations = {'opencv-python-headless': ('surya-ocr',)}

    D._log_alignment_moves([(family, '4.11.0.86', family.members[:2])], resolved, annotations)

    assert len(said) == 1
    assert 'opencv-python' in said[0]
    assert '4.13.0.92 -> 4.11.0.86' in said[0]
    assert 'surya-ocr' in said[0]


def test_nothing_is_logged_when_alignment_moved_nobody(monkeypatch):
    said = []
    monkeypatch.setattr(D, 'monitorStatus', said.append)
    family = pkg_families.family_by_name('cv2')
    resolved = {'opencv-python-headless': '4.13.0.92'}
    D._log_alignment_moves([(family, '4.13.0.92', family.members[:1])], resolved, {})
    assert said == []


# ---------------------------------------------------------------------------
# probes: verdicts, the marker, and the one lever
# ---------------------------------------------------------------------------

P = pkg_families.probes


def _probe_work(version='4.13.0.92', passes=(pkg_families.InstallPass(dist='opencv-python-headless', version='x'),)):
    family = pkg_families.family_by_name('cv2')
    return D._FamilyWork(family=family, version=version, members=family.members[:1], passes=passes)


def _answer(monkeypatch, verdict, detail=''):
    """Inject the runner. Every rule below is about what is *done* with an answer, not about
    producing one — the subprocess itself is covered by the live check, not by a unit test.
    """
    monkeypatch.setattr(D, '_run_probe', lambda work, site: (verdict, detail))


def test_a_passing_probe_clears_the_marker(monkeypatch, tmp_path):
    P.update_marker(str(tmp_path), 'cv2', P.UNPROVED_FAILED)
    _answer(monkeypatch, P.PASS, '4.13.0.92')
    D._apply_probe(_probe_work(), str(tmp_path), str(tmp_path))
    assert P.read_marker(str(tmp_path)) == {}


def test_a_failing_probe_writes_the_marker_and_stops_the_build(monkeypatch, tmp_path):
    """Without the marker the next start finds a matching hash and every member already at V,
    skips the ordered passes and never re-probes — an environment measured and found broken
    goes silently into service on the second attempt.
    """
    _answer(monkeypatch, P.FAIL_ENVIRONMENT, 'no libGL')
    monkeypatch.setenv(D.PROBE_STRICT_ENV, '1')
    with pytest.raises(RuntimeError, match='did not prove out'):
        D._apply_probe(_probe_work(), str(tmp_path), str(tmp_path))
    assert P.read_marker(str(tmp_path)) == {'cv2': P.UNPROVED_FAILED}


def test_an_inconclusive_probe_fails_hard_and_says_it_is_about_the_probe(monkeypatch, tmp_path):
    """A non-zero exit is not evidence about a version. Reported as one, it sends an operator
    to change a number over a machine that could not run the check at all.
    """
    _answer(monkeypatch, P.INCONCLUSIVE, 'exited with code 1 and reported no verdict')
    with pytest.raises(RuntimeError) as raised:
        D._apply_probe(_probe_work(), str(tmp_path), str(tmp_path))
    assert 'inconclusive' in str(raised.value)
    assert 'statement about the probe' in str(raised.value)


def test_the_two_failure_verdicts_carry_different_messages():
    """Today both stop the build; the distinction buys the operator's message, and it is what
    the deferred search will key on later.
    """
    work = _probe_work()
    version_msg = D._probe_failure_message(work, P.FAIL_VERSION, 'built for another CUDA')
    environment_msg = D._probe_failure_message(work, P.FAIL_ENVIRONMENT, 'no libGL')
    assert 'fail-version' in version_msg and 'built for another CUDA' in version_msg
    assert 'fail-environment' in environment_msg and 'no libGL' in environment_msg
    assert version_msg != environment_msg


def test_the_message_names_a_lever_only_where_one_exists():
    """cv2's failures are a missing system library or a lost namespace race — no version knob
    repairs either, so offering one would point the operator at the wrong thing.
    """
    from pkg_families.onnxruntime import ONNXRUNTIME

    cv2_msg = D._probe_failure_message(_probe_work(), P.FAIL_ENVIRONMENT, 'no libGL')
    assert 'namespace_version' not in cv2_msg
    assert 'No version change repairs this' in cv2_msg

    declared = D._FamilyWork(family=ONNXRUNTIME, version='1.22.0', members=ONNXRUNTIME.members, passes=())
    assert 'namespace_version' in D._probe_failure_message(declared, P.FAIL_VERSION, 'wrong CUDA')


def test_a_skipped_probe_is_distinguishable_from_a_pass_and_clears_nothing(monkeypatch, tmp_path):
    """A skip proves nothing, so a previous failure survives it — and it is logged where an
    operator sees it, because a silent skip reads exactly like success.
    """
    said = []
    monkeypatch.setattr(D, 'monitorStatus', said.append)
    P.update_marker(str(tmp_path), 'cv2', P.UNPROVED_FAILED)
    _answer(monkeypatch, P.SKIPPED, 'a required fact is unknown')
    D._apply_probe(_probe_work(), str(tmp_path), str(tmp_path))
    assert P.read_marker(str(tmp_path)) == {'cv2': P.UNPROVED_FAILED}
    assert any('skipped' in line for line in said)


def test_a_downgraded_failure_is_recorded_but_still_marked(monkeypatch, tmp_path):
    """Two rules that look contradictory and are not. Recording the build stops the 2am lever
    from making every start repeat it; the marker stops the exemption from outliving the
    lever, so the fix takes effect when the operator makes it rather than at the next
    unrelated drift. The downgrade itself is visible, so an environment running unproved never
    reads like one that passed.
    """
    said = []
    monkeypatch.setattr(D, 'monitorStatus', said.append)
    monkeypatch.setenv(D.PROBE_STRICT_ENV, '0')
    _answer(monkeypatch, P.FAIL_VERSION, 'wrong CUDA')
    D._apply_probe(_probe_work(), str(tmp_path), str(tmp_path))
    assert P.read_marker(str(tmp_path)) == {'cv2': P.UNPROVED_DOWNGRADED}
    assert any('UNPROVED' in line for line in said)


def test_a_marker_re_runs_the_probe_alone_past_both_gates(monkeypatch, tmp_path):
    """The one thing that crosses the drift gate and the dist-info gate. It re-probes; it does
    not rebuild and it does not install.
    """
    D._reprobed.clear()
    P.update_marker(str(tmp_path), 'cv2', P.UNPROVED_FAILED)
    ran = []
    monkeypatch.setattr(D, '_run_probe', lambda work, site: (ran.append(work.family.name), (P.PASS, ''))[1])
    monkeypatch.setattr(D.subprocess, 'run', lambda *a, **k: pytest.fail('re-proving must not install'))

    constraints = _constraints(tmp_path, 'opencv-python-headless==4.13.0.92\n')
    D._reprove_unproved(constraints, str(tmp_path), str(tmp_path))
    assert ran == ['cv2']
    assert P.read_marker(str(tmp_path)) == {}, 'a pass clears it'


def test_re_proving_happens_once_per_process_not_once_per_depends_call(monkeypatch, tmp_path):
    """The marker survives the gates by design; without this it would also make every later
    `depends()` call in the same start spawn another subprocess.
    """
    D._reprobed.clear()
    P.update_marker(str(tmp_path), 'cv2', P.UNPROVED_FAILED)
    ran = []
    monkeypatch.setattr(D, '_run_probe', lambda work, site: (ran.append(1), (P.FAIL_ENVIRONMENT, 'x'))[1])
    monkeypatch.setenv(D.PROBE_STRICT_ENV, '0')
    monkeypatch.setattr(D, 'monitorStatus', lambda *_: None)
    constraints = _constraints(tmp_path, 'opencv-python-headless==4.13.0.92\n')
    D._reprove_unproved(constraints, str(tmp_path), str(tmp_path))
    D._reprove_unproved(constraints, str(tmp_path), str(tmp_path))
    assert ran == [1]


def test_a_hard_re_probe_failure_keeps_failing_in_the_same_process(monkeypatch, tmp_path):
    """The once-per-process guard is a cost rule, not an exemption. Recorded before the probe
    ran rather than after it, it would let the first `depends()` call raise and every later one
    in the same start proceed — the second node running against precisely the environment the
    marker says was measured and found broken.
    """
    D._reprobed.clear()
    P.update_marker(str(tmp_path), 'cv2', P.UNPROVED_FAILED)
    ran = []
    monkeypatch.setattr(D, '_run_probe', lambda work, site: (ran.append(1), (P.FAIL_ENVIRONMENT, 'no libGL'))[1])
    monkeypatch.setenv(D.PROBE_STRICT_ENV, '1')
    constraints = _constraints(tmp_path, 'opencv-python-headless==4.13.0.92\n')
    for _ in range(2):
        with pytest.raises(RuntimeError, match='did not prove out'):
            D._reprove_unproved(constraints, str(tmp_path), str(tmp_path))
    assert ran == [1, 1], 'the second call must re-probe, not be waved through by the guard'


def test_no_marker_means_no_subprocess_at_all(monkeypatch, tmp_path):
    monkeypatch.setattr(D, '_run_probe', lambda *a: pytest.fail('there is nothing to re-prove'))
    D._reprove_unproved(_constraints(tmp_path, 'opencv-python-headless==4.13.0.92\n'), str(tmp_path), None)


def test_nothing_installed_means_nothing_new_to_prove(monkeypatch, tmp_path):
    """The probe follows the same gate as the ordered passes — otherwise a suite calling
    `depends()` a hundred times pays a subprocess on each.
    """
    monkeypatch.setattr(D, '_run_probe', lambda *a: pytest.fail('the gate should have held'))
    monkeypatch.setattr(D, '_run_family_passes', lambda *a: None)
    D._handle_families([_probe_work(passes=())], _constraints(tmp_path, 'x==1\n'), str(tmp_path))
