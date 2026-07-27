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

from rocketlib import APERR, Ec, error

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

            if lane == 'open':
                # Start of a new object: forget the previous one before anything can ship it.
                # The base replaces `_obj` only *after* its type-check and `data['url']` access, so
                # a malformed open frame would otherwise leave the previous entry in place and the
                # except-branch below would merge that stale object into this one's error path.
                self._obj = None
                self._entryMerged = False

            try:
                # Send it to the local pipeline
                self.callLocal(lane, data, header)

                # The object is finished: ship its entry home before acking, so main can fold the
                # response and the failure into the object the client actually reads (§4.12).
                if lane == 'close':
                    self._mergeBackEntry()

                # Send the success signal to the bridged pipeline
                self._send('error', APERR().toDict())

            except Exception as e:
                # Ship the entry on *any* failing lane, not just `close`. A node that raises during
                # the data phase kills this loop, so the `close` frame never arrives -- a close-only
                # hook would merge nothing and main would report the wrapped RemoteException
                # instead of the child's own error.
                self._mergeBackEntry()

                # Determine whether this error originated from the far side.
                # If so, propagate the original error code; otherwise wrap it as
                # a new RemoteException so the far side sees what went wrong.
                if isinstance(e, APERR) and e.ec == Ec.RemoteException:
                    self._send('error', e.toDict())
                else:
                    self._send('error', APERR(Ec.RemoteException, str(e)).toDict())

                raise

    def _mergeBackEntry(self):
        """Ship this object's entry to main so its response and failure are not lost (§4.12).

        A ``response``/``end`` node inside a venv writes into the **child's** entry, which no
        client ever reads -- ``data_conn.close_sync`` only ever reads main's root entry. This is
        the frame that carries it home; main folds it into the object the client awaits.

        Read from ``self._obj``, not ``self.instance.currentObject``: by the time the object is
        closed ``cb_close`` has set ``pyCurrentEntry`` to ``None`` and cleared ``currentEntry``.
        ``self._obj`` is the Python-held ``Entry`` the base keeps alive, and ``cb_open`` binds the
        engine to it by reference, so the child's ``response`` node wrote into that very object.

        Never raises: it runs on the failure path too, where an exception here would replace the
        original one on its way to the caller's error ack.
        """
        try:
            if self._entryMerged or self._obj is None:
                return

            payload = self._obj.toDict()
            objectFailed = self._obj.objectFailed

            # Nothing to contribute: stay off the wire entirely, so a venv without a `response`
            # node that simply succeeds behaves exactly as it did before merge-back existed.
            # Gate on the response, NOT on the payload: `toDict` always emits at least `name`
            # (it falls back to the url's filename), so a payload emptiness check would never fire.
            if not payload.get('response') and not objectFailed:
                return

            payload['objectFailed'] = objectFailed
            payload['completionError'] = self._obj.completionError

            self._entryMerged = True
            self.callRemote('entry', payload)

        except Exception as e:
            error(e)

    # Shared global reference and socket state
    IGlobal: IGlobal = None

    # One `entry` frame per object; reset when the next `open` arrives.
    _entryMerged: bool = False
