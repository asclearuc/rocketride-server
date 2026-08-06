# =============================================================================
# MIT License — Copyright (c) 2026 Aparavi Software AG
# (full text in depends.py)
# =============================================================================

"""What this build is, and what it is running on — the inputs family rules read.

Four facts, two about the build and two about the host:

- **python** and **platform**, straight from the interpreter;
- **the CUDA this build targets**, parsed out of ``ai/common/torch/requirements.txt``
  rather than declared a second time. That file is the single source of truth for the
  wheel selection, so a fact derived from it cannot drift out of sync with it. The parse
  is **marker-aware**: the file carries a Darwin line without any ``+cuNNN`` beside the
  non-Darwin ``torch==2.10.0+cu128``, and a naive suffix search would report CUDA 12.8 on
  a Mac, where the selected wheel has no CUDA at all;
- **whether the host has a GPU, and its driver version**, via a guarded ``pynvml`` import
  in the shape ``ai/modules/task/task_metrics.py`` already uses.

The GPU fact is resolved **lazily**, and that is load-bearing rather than tidy:
``nvidia-ml-py`` is declared in ``ai/requirements.txt``, installed by the *first*
``depends()`` call of the whole startup. Resolve it eagerly at import and that call
reports "no GPU" on a machine that has one — silently, because a skipped probe is
legitimate elsewhere.

"Unknown" is a third answer, never folded into "absent": a probe whose facts cannot be
evaluated is skipped and said to be skipped.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Callable, Optional

from . import markers

# torch's local version carries the CUDA the build targets: `+cu128` -> 12.8.
_LOCAL_CUDA = re.compile(r'\+cu(\d{3,})\b')


def torch_requirements_path(exe_dir: str) -> str:
    """Where the CUDA fact is read from, relative to the engine executable directory."""
    return os.path.join(exe_dir, 'ai', 'common', 'torch', 'requirements.txt')


def parse_cuda(text: str, environment: dict[str, str]) -> Optional[str]:
    """The CUDA version this build targets, or ``None`` when the selected wheel has none.

    Reads the first requirement line whose marker holds on ``environment`` and whose
    version carries a ``+cuNNN`` local segment. Lines starting with ``-`` (the
    ``--extra-index-url``) are skipped on purpose: the URL names the same CUDA, but only
    the pinned line is marker-qualified, and the marker is the whole point.
    """
    for raw in text.splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line or line.startswith('-'):
            continue
        spec, _, marker = line.partition(';')
        if marker.strip():
            try:
                if not markers.evaluate(marker, environment):
                    continue
            except markers.UnsupportedMarker:
                # Cannot tell whether this line applies, so it must not be allowed to
                # answer. Leaving it out is the safe direction: an absent CUDA fact skips
                # the probe loudly, a wrong one would fail a working environment.
                continue
        found = _LOCAL_CUDA.search(spec)
        if found:
            digits = found.group(1)
            return f'{digits[:-1]}.{digits[-1:]}'
    return None


def _nvml_probe() -> tuple[int, Optional[str]]:
    """(GPU count, driver version) via NVML. Raises when the library is unavailable."""
    import pynvml

    pynvml.nvmlInit()
    count = pynvml.nvmlDeviceGetCount()
    driver = None
    try:
        driver = pynvml.nvmlSystemGetDriverVersion()
        if isinstance(driver, bytes):
            driver = driver.decode('utf-8', 'replace')
    except Exception:
        driver = None
    return count, driver


class Facts:
    """The environment facts, each computed once and only when first asked for.

    Every input is injectable, so the rules and probes are testable without a GPU, a
    torch install, or an engine.
    """

    def __init__(
        self,
        *,
        exe_dir: Optional[str] = None,
        environment: Optional[dict[str, str]] = None,
        torch_requirements: Optional[str] = None,
        gpu_probe: Optional[Callable[[], tuple[int, Optional[str]]]] = None,
    ):
        self._exe_dir = exe_dir if exe_dir is not None else os.path.dirname(os.path.abspath(sys.executable))
        self._environment = markers.marker_environment(environment)
        self._torch_requirements = torch_requirements
        self._gpu_probe = gpu_probe or _nvml_probe
        self._cuda_resolved = False
        self._cuda: Optional[str] = None
        self._gpu_resolved = False
        self._gpu_present: Optional[bool] = None
        self._driver: Optional[str] = None

    # -- build facts --------------------------------------------------------

    @property
    def environment(self) -> dict[str, str]:
        """The PEP 508 marker variables, for evaluating a member's marker."""
        return self._environment

    @property
    def python(self) -> str:
        return self._environment['python_version']

    @property
    def platform_system(self) -> str:
        return self._environment['platform_system']

    @property
    def cuda(self) -> Optional[str]:
        """The CUDA this build targets (``'12.8'``), or ``None`` on a build without one."""
        if not self._cuda_resolved:
            self._cuda_resolved = True
            path = self._torch_requirements or torch_requirements_path(self._exe_dir)
            try:
                with open(path, 'r', encoding='utf-8') as fh:
                    text = fh.read()
            except OSError:
                self._cuda = None
            else:
                self._cuda = parse_cuda(text, self._environment)
        return self._cuda

    # -- host facts ---------------------------------------------------------

    def _resolve_gpu(self) -> None:
        if self._gpu_resolved:
            return
        self._gpu_resolved = True
        try:
            count, driver = self._gpu_probe()
        except Exception:
            # Unknown, not absent: pynvml may simply not be installed yet.
            self._gpu_present = None
            self._driver = None
            return
        self._gpu_present = count > 0
        self._driver = driver

    @property
    def gpu(self) -> Optional[bool]:
        """``True``/``False`` when NVML answered, ``None`` when it could not be asked."""
        self._resolve_gpu()
        return self._gpu_present

    @property
    def driver_version(self) -> Optional[str]:
        """The NVIDIA driver version, so a failure message can separate driver from wheel."""
        self._resolve_gpu()
        return self._driver

    # -- probe support ------------------------------------------------------

    def has(self, name: str) -> Optional[bool]:
        """Whether fact ``name`` holds — ``None`` when it is unknown.

        The tri-state is the contract: a probe whose ``needs`` cannot be evaluated is
        recorded as *skipped*, which must never look like a pass.
        """
        if name == 'gpu':
            return self.gpu
        if name == 'cuda':
            return self.cuda is not None
        return None

    def describe(self) -> str:
        """One line for a failure message: what the build targets, what the host has.

        Resolves the GPU fact — by the time anything asks for this line the build has long
        finished bootstrapping, and "GPU present, driver 550.x, build targets CUDA 12.8, no
        CUDA provider" is exactly what separates a driver problem from a wheel problem.
        """
        parts = [f'python {self.python}', f'platform {self.platform_system}']
        parts.append(f'build targets CUDA {self.cuda}' if self.cuda else 'build targets no CUDA')
        present = self.gpu
        if present is None:
            parts.append('GPU unknown')
        elif present:
            parts.append(f'GPU present, driver {self.driver_version or "unknown"}')
        else:
            parts.append('no GPU')
        return ', '.join(parts)
