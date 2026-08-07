# =============================================================================
# MIT License — Copyright (c) 2026 Aparavi Software AG
# (full text in depends.py)
# =============================================================================

"""The ``onnxruntime`` family: two distributions, mutually exclusive by platform.

**Declared here and deliberately NOT registered yet** — see ``families()``. Registering a
family changes what its environments install the moment it happens, and for this one the
change is not neutral: an environment whose only consumer is transitive (``agent_crewai``
-> ``crewai`` -> ``chromadb``) resolves plain ``onnxruntime`` and installs nothing today,
because the static exclusion drops the plain member and nothing names ``-gpu``. Register
it before the version is declared and the owner fallback would install ``-gpu`` at a
*derived* version — plain onnxruntime's own, five minor releases above anything this tree
has ever pinned — in the increment whose entire contract is "nothing changes", and before
the probes exist. It registers together with the deletion of the five copied pins and the
move of the static exclusion, which is one commit.

Unlike ``cv2`` this family needs only **exclusion**, not agreement: on each platform
exactly one member is active, so they never co-install. That difference dissolves a trap
the current pins are explicitly fighting — one line serving both platforms has to find a
version published for *both* members, and ``1.20.2`` exists only for ``-gpu`` while
``1.20.1`` was withdrawn for ``-gpu`` and survives for the CPU build. A platform-aware
family never asks that question.

``namespace_version`` is a **placeholder**, not a design. The two members do not publish
the same version sets in either direction, so a version inherited from the member the
consumers named can be one the installed member lacks. Closing that needs constraint
*transfer* between distributions — deriving the consumers' declared constraints rather
than their resolved versions and re-resolving the installed member against them — which
uv cannot express and which is the named follow-up. Until it lands, one authored number in
one place replaces ten copied lines, and the probe is what keeps it honest.
"""

from __future__ import annotations

from . import Family, Member, Probe

# This is the probe the whole mechanism exists for: no resolver can check which CUDA an
# `onnxruntime-gpu` wheel was built against, because PyPI metadata does not say. Only
# running it can.
#
# **It separates "wrong version" from "missing runtime", and that line is drawn now rather
# than when the deferred search needs it.** `onnxruntime-gpu` does not vendor the CUDA
# runtime — it expects the `nvidia-*` wheels (which `torch+cu128` drags in) or system
# libraries — and a scoped environment can legitimately hold it *without* torch, since
# `faster-whisper` does not require torch. So "no CUDA provider at all" there means cudnn is
# absent, which no lower version repairs, while "the provider is there and refuses when a
# session is created" is a version fact and is where onnxruntime's own error text names the
# CUDA it wanted. Today both stop the build and the difference only chooses the operator's
# message; when narrowing arrives, only the second may step down. Drawing it later would
# cost ~200 MB per pointless attempt.
_ONNXRUNTIME_PROBE = """
import onnxruntime

providers = onnxruntime.get_available_providers()
if 'CUDAExecutionProvider' not in providers:
    verdict(
        'fail-environment',
        'no CUDAExecutionProvider; the CUDA runtime is absent (available: %s)' % (', '.join(providers) or 'none'),
    )
else:
    try:
        onnxruntime.InferenceSession
        verdict('pass', 'CUDAExecutionProvider available')
    except Exception as exc:  # pragma: no cover - reached only on a real refusing provider
        verdict('fail-version', repr(exc))
"""

ONNXRUNTIME = Family(
    name='onnxruntime',
    import_name='onnxruntime',
    members=(
        Member(dist='onnxruntime', marker="platform_system == 'Darwin'"),
        Member(dist='onnxruntime-gpu', marker="platform_system != 'Darwin'"),
    ),
    # Skipped, not failed, on a CPU-only host: `-gpu` there legitimately offers CPU only, so
    # demanding the CUDA provider would fail a working install.
    probe=Probe(code=_ONNXRUNTIME_PROBE, needs=('gpu',)),
    # Placeholder for the constraint transfer, not a compatibility claim we stand behind.
    namespace_version='1.22.0',
    notes=(
        'Why the number moved to 1.22.0, kept from the requirement-file comment that is '
        'being deleted with the pin: 1.20.1 was withdrawn from PyPI for the -gpu build '
        '(the CPU build of it survives), and 1.20.2 exists only for -gpu, so pinning it '
        'would break macOS.',
        'onnxruntime-gpu does not vendor the CUDA runtime: it expects the nvidia-* wheels '
        '(which torch+cu128 drags in) or system libraries. A scoped environment can hold '
        'it without torch -- faster-whisper does not require torch -- so "no CUDA '
        'provider" there means a missing runtime, which no lower version repairs.',
    ),
)
