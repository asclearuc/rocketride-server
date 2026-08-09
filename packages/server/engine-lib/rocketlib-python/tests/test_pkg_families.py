# =============================================================================
# MIT License — Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""Rules of the shared-namespace package families.

Pure: no engine, no ``uv``, no GPU, no network. Every case here is one of the rules that is
easy to undo by accident, and the docstring says which failure it prevents rather than
restating the assertion.
"""

import os
import sys
import textwrap

import pytest

import pkg_families
from pkg_families import markers
from pkg_families.onnxruntime import ONNXRUNTIME
from pkg_families.opencv import CV2

# tests/ -> rocketlib-python -> engine-lib -> server -> packages -> repo root
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), *([os.pardir] * 5)))
TORCH_REQUIREMENTS = os.path.join(REPO_ROOT, 'packages', 'ai', 'src', 'ai', 'common', 'torch', 'requirements.txt')

LINUX = {
    'platform_system': 'Linux',
    'platform_machine': 'x86_64',
    'sys_platform': 'linux',
    'os_name': 'posix',
    'python_version': '3.12',
    'python_full_version': '3.12.13',
}
DARWIN = dict(LINUX, platform_system='Darwin', platform_machine='arm64', sys_platform='darwin')


def facts(environment=LINUX, gpu=None, torch_requirements=None):
    """Facts with every input injected — the whole point of the class being constructible."""

    def probe():
        if gpu is None:
            raise RuntimeError('pynvml not installed')
        return (1 if gpu else 0, '550.1' if gpu else None)

    return pkg_families.Facts(
        exe_dir=os.path.dirname(sys.executable),
        environment=environment,
        torch_requirements=torch_requirements,
        gpu_probe=probe,
    )


# ---------------------------------------------------------------------------
# the registry itself
# ---------------------------------------------------------------------------


def test_the_registry_holds_both_families_and_each_is_reachable():
    """Why onnxruntime arrived second, kept because the assertion no longer says it: registering
    a family changes what its environments install *at that moment*, and registering this one
    before its version was declared would have had an ``agent_crewai``-shaped overlay install a
    ~200 MB CUDA build at a *derived* version nobody validated. It landed together with the
    declared version and the deletion of the five copied pins.
    """
    assert [family.name for family in pkg_families.families()] == ['cv2', 'onnxruntime']
    assert pkg_families.family_by_name('onnxruntime') is ONNXRUNTIME
    assert pkg_families.family_by_import('onnxruntime') is ONNXRUNTIME


def test_all_member_dists_covers_every_registered_family():
    """The early return subtracts this set, which is what keeps a member excluded *by design*
    (plain onnxruntime on non-Darwin) from reading as permanently missing work and reinstalling
    the world on every call. Miss a member here and that member becomes the leak.
    """
    assert pkg_families.all_member_dists() == {
        'opencv-python-headless',
        'opencv-python',
        'opencv-contrib-python-headless',
        'opencv-contrib-python',
        'onnxruntime',
        'onnxruntime-gpu',
    }


# ---------------------------------------------------------------------------
# alignment
# ---------------------------------------------------------------------------


def test_align_takes_the_minimum_over_every_member_that_appeared():
    """Each member in the resolution is the resolver's answer about the shared namespace."""
    resolved = {
        'opencv-python-headless': '4.11.0.86',
        'opencv-python': '4.13.0.92',
        'opencv-contrib-python': '5.0.0.93',
    }
    assert pkg_families.align(CV2, resolved) == '4.11.0.86'


def test_a_resolution_naming_only_a_non_applicable_member_still_yields_a_version():
    """The rule a "matching members only" alignment would get wrong, and it is invisible in
    production until the constraint transfer removes onnxruntime's declared version — which is
    exactly why it is asserted. On Linux nothing names ``onnxruntime-gpu``; ``gliner`` resolves
    plain ``onnxruntime``. Under "matching only" no matching member ever appears, V is undefined,
    and the family stops working on the platform it exists for.
    """
    derived = pkg_families.Family(
        name='onnxruntime-derived',
        import_name='onnxruntime',
        members=ONNXRUNTIME.members,
    )
    assert pkg_families.align(derived, {'onnxruntime': '1.27.0'}) == '1.27.0'


def test_a_declared_namespace_version_is_returned_without_derivation():
    """Keeps the declared answer from being quietly re-derived into the gap it exists to close:
    the two onnxruntime members do not publish the same version sets, so a version inherited
    from the member the consumers named can be one the installed member lacks.
    """
    assert pkg_families.align(ONNXRUNTIME, {'onnxruntime': '1.27.0'}) == '1.22.0'


def test_align_returns_nothing_when_no_member_was_resolved():
    assert pkg_families.align(CV2, {'numpy': '2.5.1'}) is None


def test_version_key_orders_a_prerelease_below_its_release():
    """The one PEP 440 rule that would otherwise bite a plain string comparison."""
    assert pkg_families.version_key('4.13.0rc1') < pkg_families.version_key('4.13.0')
    assert pkg_families.version_key('4.11.0.86') < pkg_families.version_key('4.13.0.92')
    assert pkg_families.version_key('4.13.0.92') < pkg_families.version_key('5.0.0.93')


# ---------------------------------------------------------------------------
# applicability and the install set
# ---------------------------------------------------------------------------


def test_applicable_selects_one_onnxruntime_member_per_platform():
    assert [m.dist for m in pkg_families.applicable(ONNXRUNTIME, facts(DARWIN))] == ['onnxruntime']
    assert [m.dist for m in pkg_families.applicable(ONNXRUNTIME, facts(LINUX))] == ['onnxruntime-gpu']


def test_the_owner_needs_no_darwin_special_case():
    """On macOS ``-gpu`` does not apply, so plain onnxruntime is both the only applicable member
    and the owner — the definition falls out rather than being written twice.
    """
    assert pkg_families.owner(ONNXRUNTIME, facts(DARWIN)).dist == 'onnxruntime'
    assert pkg_families.owner(ONNXRUNTIME, facts(LINUX)).dist == 'onnxruntime-gpu'


def test_install_set_is_what_the_environment_resolved_not_the_whole_family():
    """A member nobody named is not installed just because it owns the namespace. Adding the
    owner would put the **non-headless** contrib build into a Surya environment that asked for
    headless, which then fails ``import cv2`` on a host with no ``libGL`` — a rule meant to
    guarantee the superset turning a working configuration into a build failure.
    """
    resolved = {'opencv-python-headless': '4.11.0.86'}
    assert [m.dist for m in pkg_families.install_set(CV2, resolved, facts())] == ['opencv-python-headless']


def test_the_owner_stands_in_only_when_nothing_applicable_was_resolved():
    """The empty case is not a corner: once the explicit pins go, an environment whose only
    onnxruntime consumer is transitive resolves the *plain* distribution, which does not apply
    on Linux — and nothing would be installed at all without this fallback.
    """
    members = pkg_families.install_set(ONNXRUNTIME, {'onnxruntime': '1.27.0'}, facts(LINUX))
    assert [m.dist for m in members] == ['onnxruntime-gpu']


def test_install_set_keeps_declared_order_regardless_of_resolution_order():
    resolved = {'opencv-contrib-python': '4.13.0.92', 'opencv-python-headless': '4.13.0.92'}
    assert [m.dist for m in pkg_families.install_set(CV2, resolved, facts())] == [
        'opencv-python-headless',
        'opencv-contrib-python',
    ]


# ---------------------------------------------------------------------------
# ordering and the forced re-lay
# ---------------------------------------------------------------------------


def test_headless_is_ordered_before_the_gui_build():
    """The regression for "doctr and easyocr in one environment, GUI build overwritten by the
    headless one". The four variants are a lattice, and the order the distribution *names*
    suggest is the wrong one.
    """
    order = [m.dist for m in CV2.members]
    assert order.index('opencv-python-headless') < order.index('opencv-python')
    assert order.index('opencv-python') < order.index('opencv-contrib-python')


def test_the_widest_member_is_forced_when_something_earlier_is_laid_down():
    """The regression for "contrib was already there, headless arrived, ximgproc vanished". uv
    reports the satisfied contrib and skips the write, so headless ends up the last writer.
    """
    members = pkg_families.install_set(
        CV2,
        {'opencv-python-headless': '4.13.0.92', 'opencv-contrib-python': '4.13.0.92'},
        facts(),
    )
    passes = pkg_families.install_batches(CV2, '4.13.0.92', members, {'opencv-contrib-python': '4.13.0.92'})
    assert [(p.dist, p.force_reinstall) for p in passes] == [
        ('opencv-python-headless', False),
        ('opencv-contrib-python', True),
    ]


def test_nothing_is_forced_when_only_the_widest_member_is_missing():
    """Forcing unconditionally would re-download the widest wheel on every build."""
    members = pkg_families.install_set(
        CV2,
        {'opencv-python-headless': '4.13.0.92', 'opencv-contrib-python': '4.13.0.92'},
        facts(),
    )
    passes = pkg_families.install_batches(CV2, '4.13.0.92', members, {'opencv-python-headless': '4.13.0.92'})
    assert [(p.dist, p.force_reinstall) for p in passes] == [('opencv-contrib-python', False)]


def test_the_ordered_passes_are_skipped_when_every_member_already_sits_at_the_version():
    """This is what keeps a suite calling ``depends()`` a hundred times cheap: a directory
    listing answers it, with no ``uv`` process at all.
    """
    members = pkg_families.install_set(CV2, {'opencv-python-headless': '4.13.0.92'}, facts())
    installed = {'opencv-python-headless': '4.13.0.92'}
    assert pkg_families.install_batches(CV2, '4.13.0.92', members, installed) == ()


def test_installed_versions_reads_dist_info_names(tmp_path):
    """dist-info uses underscores; the family speaks in hyphens. They have to meet."""
    (tmp_path / 'opencv_python_headless-4.13.0.92.dist-info').mkdir()
    (tmp_path / 'numpy-2.5.1.dist-info').mkdir()
    (tmp_path / 'not-a-dist-info').mkdir()
    found = pkg_families.installed_versions(str(tmp_path))
    assert found['opencv-python-headless'] == '4.13.0.92'
    assert found['numpy'] == '2.5.1'


def test_installed_versions_of_a_missing_directory_is_empty_not_an_error():
    assert pkg_families.installed_versions(os.path.join('does', 'not', 'exist')) == {}


# ---------------------------------------------------------------------------
# exclusions and the derived block
# ---------------------------------------------------------------------------


def test_every_member_of_a_present_family_is_excluded_from_the_main_install():
    """Including the ones about to be installed: excluded means "not by *that* run", because
    the main install would let uv choose the order and the order is the whole point.
    """
    assert set(pkg_families.excluded(CV2)) == {m.dist for m in CV2.members}


def test_the_derived_block_names_the_install_set_at_the_derived_version():
    members = pkg_families.install_set(CV2, {'opencv-python-headless': '4.11.0.86'}, facts())
    block = pkg_families.derived_block(CV2, '4.11.0.86', members)
    assert pkg_families.DERIVED_MARKER in block
    assert 'opencv-python-headless==4.11.0.86' in block
    assert 'opencv-contrib-python==' not in block


def test_a_family_with_a_declared_version_produces_no_derived_block():
    """Nothing names ``-gpu``, so a block would add a line pass 1 never contains — and pass 2
    would then run on **every** recompile, forever, doubling a compile that resolves the whole
    tree to buy nothing: the ordered install passes the declared version explicitly.
    """
    members = pkg_families.install_set(ONNXRUNTIME, {'onnxruntime': '1.27.0'}, facts(LINUX))
    assert pkg_families.derived_block(ONNXRUNTIME, '1.22.0', members) == ''


def test_the_second_compile_is_skipped_when_the_block_would_change_nothing():
    resolved = {'opencv-python-headless': '4.13.0.92', 'opencv-contrib-python': '4.13.0.92'}
    members = pkg_families.install_set(CV2, resolved, facts())
    assert pkg_families.redundant('4.13.0.92', members, resolved)
    assert not pkg_families.redundant('4.11.0.86', members, resolved)


def test_the_derived_block_does_not_accumulate():
    """An implementation appending to whatever was on disk would grow a block per rebuild and
    pin the environment to the first version it ever computed.
    """
    combined = 'numpy\n' + pkg_families.derived_block(CV2, '4.11.0.86', CV2.members[:1])
    assert pkg_families.strip_derived_block(combined) == 'numpy\n'


# ---------------------------------------------------------------------------
# reading the compiled artifact
# ---------------------------------------------------------------------------


CONSTRAINTS = textwrap.dedent("""
    # This file was autogenerated by uv
    --extra-index-url https://download.pytorch.org/whl/cu128
    opencv-python-headless==4.11.0.86
        # via
        #   -r cache/combined.txt
        #   surya-ocr
    opencv-contrib-python==4.13.0.92
        # via img2table
    numpy==2.5.1 ; python_version >= '3.9'
        # via opencv-python-headless
    """)


def test_parse_resolution_skips_index_urls_and_keeps_markers_out_of_the_version():
    resolved = pkg_families.parse_resolution(CONSTRAINTS)
    assert resolved['opencv-python-headless'] == '4.11.0.86'
    assert resolved['numpy'] == '2.5.1'
    assert '--extra-index-url' not in resolved


def test_via_annotations_are_readable_because_a_message_depends_on_them():
    """``--no-annotate`` must never be added to either compile as a tidiness measure: this is
    where "who asked for this version" comes from.
    """
    annotations = pkg_families.parse_annotations(CONSTRAINTS)
    assert annotations['opencv-python-headless'] == ('-r cache/combined.txt', 'surya-ocr')
    assert annotations['opencv-contrib-python'] == ('img2table',)


def test_members_in_finds_a_family_arriving_transitively():
    """Both real cases arrive that way, so detection reads the resolution rather than any
    requirements file.
    """
    assert [f.name for f in pkg_families.members_in(pkg_families.parse_resolution(CONSTRAINTS))] == ['cv2']


def test_a_resolution_with_no_family_member_produces_no_work():
    assert pkg_families.members_in({'numpy': '2.5.1'}) == ()


# ---------------------------------------------------------------------------
# the drift-hash contribution
# ---------------------------------------------------------------------------


def test_changing_a_declaration_drifts_only_the_environments_that_hold_that_family():
    """Both halves matter. Without the first, an operator edits the declared version and
    watches nothing happen. Without the second, bumping one family rebuilds every environment
    in the installation — the shape §4.8 already learned to avoid.
    """
    with_cv2 = pkg_families.declaration_digest({'opencv-python-headless': '4.13.0.92'})
    without = pkg_families.declaration_digest({'numpy': '2.5.1'})
    assert with_cv2 != ''
    assert without == ''

    moved = pkg_families.Family(name='cv2', import_name='cv2', members=CV2.members, namespace_version='9.9.9')
    original = pkg_families._registry

    try:
        pkg_families._registry = lambda: (moved,)
        after = pkg_families.declaration_digest({'opencv-python-headless': '4.13.0.92'})
    finally:
        pkg_families._registry = original

    assert after != with_cv2


def test_a_family_free_environment_keeps_its_hash_byte_identical():
    """Or every environment in the installation rebuilds once for a mechanism it never uses."""
    assert pkg_families.combine_hash('abc123', '') == 'abc123'
    assert pkg_families.combine_hash('abc123', 'deadbeef') == 'abc123:deadbeef'


# ---------------------------------------------------------------------------
# environment facts
# ---------------------------------------------------------------------------


def test_the_cuda_fact_parses_out_of_the_real_torch_file():
    """Pins the parse against the shipped declaration: if that file is ever reformatted this
    fails, instead of the fact silently becoming ``None``.
    """
    assert os.path.isfile(TORCH_REQUIREMENTS), TORCH_REQUIREMENTS
    assert facts(LINUX, torch_requirements=TORCH_REQUIREMENTS).cuda == '12.8'


def test_the_cuda_fact_is_absent_under_darwin_markers_rather_than_wrong():
    """A naive ``+cuNNN`` search reports CUDA 12.8 on a Mac, where the selected wheel has no
    CUDA at all — and every rule reading that fact would then reason about a build that does
    not exist on that host.
    """
    assert facts(DARWIN, torch_requirements=TORCH_REQUIREMENTS).cuda is None


def test_an_unavailable_gpu_probe_answers_unknown_and_never_false():
    """Unknown is a third answer: a probe whose facts cannot be evaluated is skipped and said to
    be skipped, never quietly treated as passed. The provider of this fact is installed by the
    *first* ``depends()`` call of all, so early asks legitimately cannot answer.
    """
    assert facts(gpu=None).gpu is None
    assert facts(gpu=False).gpu is False
    assert facts(gpu=True).gpu is True
    assert facts(gpu=True).driver_version == '550.1'


def test_the_gpu_fact_is_not_resolved_until_something_asks():
    """Resolve it eagerly at import and the first call of startup reports "no GPU" on a machine
    that has one, silently, because skipping is legitimate elsewhere.
    """
    calls = []

    def probe():
        calls.append(1)
        return (1, '550.1')

    subject = pkg_families.Facts(environment=LINUX, gpu_probe=probe, torch_requirements=TORCH_REQUIREMENTS)
    assert calls == []
    assert subject.gpu is True
    assert calls == [1]
    assert subject.gpu is True
    assert calls == [1], 'resolved once, then cached'


def test_facts_has_answers_the_probe_needs_tri_state():
    assert facts(gpu=True).has('gpu') is True
    assert facts(gpu=None).has('gpu') is None
    assert facts(LINUX, torch_requirements=TORCH_REQUIREMENTS).has('cuda') is True
    assert facts(DARWIN, torch_requirements=TORCH_REQUIREMENTS).has('cuda') is False


def test_the_failure_line_separates_a_driver_problem_from_a_wheel_problem():
    line = facts(LINUX, gpu=True, torch_requirements=TORCH_REQUIREMENTS).describe()
    assert 'CUDA 12.8' in line
    assert 'driver 550.1' in line


# ---------------------------------------------------------------------------
# marker evaluation
# ---------------------------------------------------------------------------


def test_markers_cover_the_grammar_this_repository_writes():
    assert markers.evaluate("platform_system == 'Darwin'", DARWIN)
    assert not markers.evaluate("platform_system == 'Darwin'", LINUX)
    assert markers.evaluate("platform_system != 'Darwin'", LINUX)
    assert markers.evaluate("platform_system == 'Darwin' and platform_machine == 'arm64'", DARWIN)
    assert not markers.evaluate("platform_system == 'Darwin' and platform_machine == 'arm64'", LINUX)
    assert markers.evaluate('', LINUX)


def test_an_unreadable_marker_raises_rather_than_reading_as_false():
    """A marker we cannot evaluate must not silently drop a distribution from the install set."""
    with pytest.raises(markers.UnsupportedMarker):
        markers.evaluate("python_version >= '3.9'", LINUX)
    with pytest.raises(markers.UnsupportedMarker):
        markers.evaluate("extra == 'gpu'", LINUX)


# ---------------------------------------------------------------------------
# probes: the script, the verdicts, the marker
# ---------------------------------------------------------------------------


def test_the_overlay_reaches_the_probe_as_a_path_insert_not_an_env_var():
    """§4.11 records that the engine's isolated ``PyConfig`` ignores ``PYTHONPATH``. A probe
    relying on it would measure base while believing it tested the overlay — a pass for an
    environment nobody checked, which is the most damaging answer available.
    """
    script = pkg_families.probes.probe_script('cv2', 'verdict("pass")', '/over/lay', ('a',), '1.0')
    assert "sys.path.insert(0, '/over/lay')" in script
    assert 'PYTHONPATH' not in script


def test_the_preamble_carries_what_a_declaration_must_branch_on():
    """So a family's code stays a static string and still asks what *this* environment
    installed — cv2 asserts ximgproc only where a contrib member landed.
    """
    script = pkg_families.probes.probe_script('cv2', 'verdict("pass")', '/s', ('opencv-contrib-python',), '4.13.0.92')
    assert "INSTALL_SET = ('opencv-contrib-python',)" in script
    assert "VERSION = '4.13.0.92'" in script


def test_a_raising_probe_becomes_fail_environment_rather_than_a_crash():
    """The harness owns the default verdict, so a declaration only has to name the cases it
    can tell apart. For cv2 that default *is* the right answer for both of its failures.
    """
    script = pkg_families.probes.probe_script('cv2', 'raise RuntimeError("no libGL")', '/s', (), '1.0')
    assert 'fail-environment' in script
    assert '_probe()' in script


def test_a_probe_that_answers_nothing_is_a_failure_of_the_probe():
    script = pkg_families.probes.probe_script('cv2', 'pass', '/s', (), '1.0')
    assert 'the probe finished without reporting a verdict' in script


def test_parse_verdict_reads_the_three_verdicts_and_nothing_else():
    P = pkg_families.probes
    assert P.parse_verdict(f'{P.VERDICT_PREFIX} pass 4.13.0.92') == (P.PASS, '4.13.0.92')
    assert P.parse_verdict(f'{P.VERDICT_PREFIX} fail-version wrong cuda') == (P.FAIL_VERSION, 'wrong cuda')
    assert P.parse_verdict(f'{P.VERDICT_PREFIX} fail-environment no libGL') == (P.FAIL_ENVIRONMENT, 'no libGL')
    assert P.parse_verdict(f'{P.VERDICT_PREFIX} something-else oops') is None


def test_no_verdict_line_is_inconclusive_not_a_negative_verdict():
    """A non-zero exit is not evidence about a version: the binary might fail to start, a DLL
    might be missing. Treated as a verdict, it sends an operator to change a version number
    over a machine that could not run the check at all.
    """
    assert pkg_families.probes.parse_verdict('Traceback...\nImportError: DLL load failed\n') is None
    assert pkg_families.probes.parse_verdict('') is None


def test_needs_are_tri_state_so_unknown_never_reads_as_absent():
    """A GPU box whose probe was skipped is the one shape that looks like success while
    checking nothing, so it has to stay distinguishable from "no GPU here, correctly skipped".
    """
    met = pkg_families.probes.needs_met
    assert met((), facts(gpu=None)) is True, 'no needs is always met'
    assert met(('gpu',), facts(gpu=True)) is True
    assert met(('gpu',), facts(gpu=False)) is False
    assert met(('gpu',), facts(gpu=None)) is None


def test_the_marker_survives_per_family_so_a_pass_cannot_clear_a_sibling(tmp_path):
    """Two families can be present, and a passing probe must not erase the record of the one
    that failed beside it.
    """
    P = pkg_families.probes
    env = str(tmp_path)
    assert P.read_marker(env) == {}
    P.update_marker(env, 'cv2', P.UNPROVED_FAILED)
    P.update_marker(env, 'onnxruntime', P.UNPROVED_DOWNGRADED)
    assert P.read_marker(env) == {'cv2': P.UNPROVED_FAILED, 'onnxruntime': P.UNPROVED_DOWNGRADED}
    P.update_marker(env, 'cv2', None)
    assert P.read_marker(env) == {'onnxruntime': P.UNPROVED_DOWNGRADED}
    P.update_marker(env, 'onnxruntime', None)
    assert P.read_marker(env) == {}
    assert not os.path.exists(P.marker_path(env)), 'the last entry removes the file'


def test_a_missing_marker_is_empty_rather_than_an_error(tmp_path):
    assert pkg_families.probes.read_marker(str(tmp_path / 'nope')) == {}


def test_both_shipped_families_declare_a_probe_and_only_one_needs_a_gpu():
    """cv2's demand is always correct, so it carries no `needs`; onnxruntime's is correct only
    where a GPU exists, so on a CPU-only host it must skip rather than fail a working install.
    """
    assert CV2.probe is not None and CV2.probe.needs == ()
    assert ONNXRUNTIME.probe is not None and ONNXRUNTIME.probe.needs == ('gpu',)


def test_the_cv2_probe_asserts_ximgproc_only_where_a_contrib_member_landed():
    """A fixed assertion would fail correct environments once most of them request only
    headless; keyed on the install set it states what the ordering owes.
    """
    assert 'ximgproc' in CV2.probe.code
    assert "'contrib' in dist" in CV2.probe.code


def test_the_onnxruntime_probe_separates_a_missing_runtime_from_a_wrong_version():
    """-gpu does not vendor the CUDA runtime, and an environment may legitimately hold it
    without torch — so "no provider" means cudnn is absent, which no lower version repairs.
    """
    code = ONNXRUNTIME.probe.code
    assert "'CUDAExecutionProvider' not in providers" in code
    assert 'fail-environment' in code
    assert 'fail-version' in code
