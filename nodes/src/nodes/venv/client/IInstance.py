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
    """The ``venv`` (main-side) bridge client: the round-trip splice of a venv boundary.

    It sits mid-chain in main (``producer -> venv -> consumer``) and dials the child once.
    The venv boundary is a request/response splice of a **single** object -- the object never
    forks -- so the whole boundary rides one socket with the ``remote`` request/response
    protocol (``callRemote``), and the object is never re-opened on the main pipe:

    - **Framing** (``open``/``closing``/``close``): sent forward via ``callRemote`` and then
      returned normally, so the engine's default *also* propagates the framing to the
      downstream consumer -- the same object opens/closes on both this node and, e.g., a
      ``response`` node, once.
    - **Forward data** (inherited ``write*``): sent forward via ``callRemote`` and
      ``preventDefault``-ed so the forward stream does not leak into the downstream consumer.
    - **Return data**: the venv's output arrives interleaved on ``callRemote``'s ack channel
      (during ``closing``/``close``) and is applied downstream via ``callLocal`` ->
      ``self.instance.write*`` -> the consumer, on the already-open object.
    """

    IGlobal: IGlobal

    def beginInstance(self):
        if not self.IGlobal.urlProcess:
            raise Exception('venv bridge transport not configured: no urlProcess injected at spawn')

        # Dial the child's /venv/pipe once and keep it open for the node's lifetime.
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
