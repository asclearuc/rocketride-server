# =============================================================================
# MIT License — Copyright (c) 2026 Aparavi Software AG
# (full text in depends.py)
# =============================================================================

"""The ``onnxruntime`` family: two distributions, mutually exclusive by platform.

Registering a family changes what its environments install the moment it happens, which is
why this one was declared here well before it joined ``families()``: an environment whose
only consumer is transitive (``agent_crewai`` -> ``crewai`` -> ``chromadb``) resolves plain
``onnxruntime`` and used to install nothing at all, because the static exclusion dropped
the plain member and nothing names ``-gpu``. Registering before the version was declared
would have had the owner fallback install ``-gpu`` at a *derived* version — plain
onnxruntime's own, five minor releases above anything this tree has ever pinned. So
registration, the declared version and the deletion of the five copied pins landed as one
commit, and that overlay now gets ``-gpu`` at the authored number instead of nothing.

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

# **What this probe actually proves today: that `import onnxruntime` works in the environment
# just built** — the namespace resolves and the extension loads, which is the shared-namespace
# question this package exists for. It does *not* prove the CUDA runtime is present. Measured,
# so nobody has to re-derive it:
#
# - the `fail-version` arm cannot fire. `onnxruntime.InferenceSession` below is an attribute
#   reference, not a session construction, so the guarded expression cannot raise;
# - the `fail-environment` arm cannot fire either. `get_available_providers()` reports the
#   providers the **wheel was built with**, not the ones whose DLLs can load: ORT does not
#   preload the CUDA DLLs on import (`preload_dlls()` is the caller's to invoke), and an import
#   with `torch/lib` off the DLL search path still lists `CUDAExecutionProvider`.
#
# The split below is therefore an **authored intention, not a running check** — kept because it
# is what the deferred narrowing search will key on, and because the distinction is real even
# where this code cannot yet observe it: `onnxruntime-gpu` does not vendor the CUDA runtime (it
# expects the `nvidia-*` wheels that `torch+cu128` drags in, or system libraries) and a scoped
# environment can legitimately hold it *without* torch, since `faster-whisper` does not require
# torch. There, "no CUDA provider" would mean a missing runtime that no lower version repairs,
# while a provider refusing at session creation would be a version fact.
#
# Making it a real check means `preload_dlls()` under a `getattr` guard, or a session on a
# synthesized model. Both add a new way to fail a build, and `preload_dlls()` looks for
# `torch/lib` — which the torch-free scoped environment above legitimately lacks. Named
# follow-up, deliberately not taken here.
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
        'What bounds the number, kept from the same deleted comment and re-checked against '
        'installed metadata: rtmlib and gliner declare onnxruntime unpinned, faster-whisper '
        'declares <2,>=1.14. The exact value is ours to choose, but it has to stay inside '
        'that range -- this is the only place that constraint is now written down.',
    ),
)
