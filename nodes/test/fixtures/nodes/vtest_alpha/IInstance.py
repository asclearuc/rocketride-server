"""VTest Alpha instance — text-lane filter importing ``tabulate`` (pinned 0.8.10).

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
        tabulate.tabulate([[text]], tablefmt='plain')
        # Folded into the text lane rather than a second one: the acceptance has to prove this
        # node IMPORTED its pin, not merely that the right file sits on disk. Attributes only --
        # a new import would pull another requirements.txt into the discovery walk.
        self.instance.writeText(f'{text}\nalpha={tabulate.__version__}@{tabulate.__file__}')
