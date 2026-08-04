"""Test-only dependency-conflict fixture node (see services.json). Never shipped.

Pins ``tabulate==0.8.10`` and imports nothing from ``ai.*`` so a pipeline that
uses both ``vtest_alpha`` and ``vtest_beta`` produces a deterministic constraints
conflict isolated to the venv-scoping mechanism.

Deliberately does NOT call ``depends()``, unlike the local-node convention in
``docs/README-nodes.md``. With one, the pin would arrive through the runtime backstop and
design §8.3 would be proving the backstop rather than per-environment scoping. The only
route to ``tabulate`` here must be the AST-discovered compile-and-install — so the pins are
installed only when scoping is active (an isolated group, or ``=1``).
"""

from .IInstance import IInstance

__all__ = ['IInstance']
