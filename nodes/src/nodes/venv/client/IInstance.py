# =============================================================================
# MIT License
# Copyright (c) 2026 Aparavi Software AG
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
# =============================================================================

from websockets.sync.client import connect

from rocketlib import Entry

from .IGlobal import IGlobal
from ..base import IInstanceBase


class IInstance(IInstanceBase):
    """The ``venv`` (egress) bridge node.

    Thin over the shared base: the 12 data ``write*`` egress overrides are inherited
    from the base; this class adds only the connect lifecycle and the object-framing
    lanes (``open``/``closing``/``close``), which the egress side drives forward -- the
    server applies inbound framing via ``callLocal`` and does not override these.
    """

    IGlobal: IGlobal

    def beginInstance(self):
        # Step 6: the venv child (and thus its loopback endpoint) is spawned in step 7.
        if not self.IGlobal.urlProcess:
            raise Exception('venv bridge transport is not wired yet (spawn + routing land in step 7)')

        # Connect to the WebSocket synchronously and keep it open
        webSocket = connect(
            self.IGlobal.urlProcess, additional_headers=self.IGlobal.headers, open_timeout=None, close_timeout=None
        )

        self.connect(webSocket)

    def endInstance(self):
        self.disconnect()

    def open(self, object: Entry):
        data = object.toDict()
        data['url'] = object.url
        self.callRemote('open', data)

    def closing(self):
        self.callRemote('closing')

    def close(self):
        self.callRemote('close')
