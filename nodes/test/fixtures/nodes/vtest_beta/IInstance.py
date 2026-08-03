"""VTest Beta instance — text-lane filter importing ``tabulate`` (pinned 0.9.0).

Implements the real filter contract: the engine dispatches inbound lane data to the
matching ``writeX`` handler (here ``writeText``); output is emitted via
``self.instance.writeX(...)``. Imports nothing from ``ai.*``.
"""

import tabulate

from rocketlib import IInstanceBase


class IInstance(IInstanceBase):
    """Passes text through unchanged, touching tabulate so the pinned wheel loads."""

    def writeText(self, text: str):
        """Handle inbound text: exercise the pinned tabulate, then report what it imported."""
        tabulate.tabulate([[text]], tablefmt='github')
        # See vtest_alpha: same lane, attributes only, so the discovery walk is unchanged.
        self.instance.writeText(f'{text}\nbeta={tabulate.__version__}@{tabulate.__file__}')
