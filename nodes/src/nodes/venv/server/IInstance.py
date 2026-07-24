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
    """The ``venv_server`` (child-side) bridge node.

    Thin over the shared base; both roles of a boundary share **one** socket (the ``remote``
    request/response model), which the ``/venv/pipe`` route binds to both instances:

    - **Forward ingress** (``sourceEnv == 'main'``): the route runs its ``handleWebSocket``
      accept loop, which reads main->child lanes and applies them to the local (venv)
      pipeline via ``callLocal`` (framing opens/closes the child object; data lanes flow
      downstream).
    - **Return egress** (``targetEnv == 'main'``): the engine data-drives its inherited
      ``write*`` (from the forward-ingress call stack, nested on the same event loop), which
      ``callRemote``-serializes the venv's output back over the **same** socket and reads its
      own ack -- so the return re-enters main through the round-trip client's ack channel.
      It sends data only; object framing is owned by the main round-trip node, so the
      inherited no-op ``open``/``closing``/``close`` are correct here.
    """

    def handleWebSocket(self, webSocket: WebSocket):
        """Service a forward-ingress (main -> child) connection: receive lanes and apply them
        to the local pipeline, acking each. The return egress's ``callRemote`` sends nest
        inside this loop's ``callLocal`` (same socket, same thread), so no second loop runs.
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
