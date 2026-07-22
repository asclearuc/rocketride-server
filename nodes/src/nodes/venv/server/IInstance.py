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

from fastapi import WebSocket

from rocketlib import APERR, Ec

from .IGlobal import IGlobal
from ..base import IInstanceBase


class IInstance(IInstanceBase):
    """The ``venv_server`` (ingress) bridge node.

    Thin over the shared base. It adds only the accept-loop; everything else is
    inherited:

    - ``callLocal`` (from the base) handles the **forward** path -- inbound lanes land
      on ``self.instance.write*`` / ``self.instance.pipe.*``.
    - the 12 data ``write*`` overrides (from the base) handle the **return** path -- when
      the venv pipeline emits data into this node, it is serialized and ``callRemote``-ed
      back to the client.

    It does not override the framing lanes: inbound framing is applied through
    ``callLocal``, and the server never drives framing back.
    """

    def handleWebSocket(self, webSocket: WebSocket):
        """
        Handle the main WebSocket loop.

        Receive the input lanes from the bridged (main) pipeline, process them with the
        local (venv) pipeline and send any results back.
        """
        self.connect(webSocket)

        while True:
            # Receive the next call from the bridged pipeline
            lane, data, header = self._recv()

            try:
                # Send it to the local pipeline
                self.callLocal(lane, data, header)

                # Send the success signal to the bridged pipeline
                self._send('error', APERR().toDict())

            except Exception as e:
                # Determine whether this error originated from the far side.
                # If so, propagate the original error code; otherwise wrap it as
                # a new RemoteException so the far side sees what went wrong.
                if isinstance(e, APERR) and e.ec == Ec.RemoteException:
                    self._send('error', e.toDict())
                else:
                    self._send('error', APERR(Ec.RemoteException, str(e)).toDict())

                raise

    # Shared global reference and socket state
    IGlobal: IGlobal = None
