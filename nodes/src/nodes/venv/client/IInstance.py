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

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from rocketlib import Entry, Lvl, debug

from .IGlobal import IGlobal
from ..base import IInstanceBase


class IInstance(IInstanceBase):
    """The ``venv`` (main-side) bridge client: the round-trip splice of a venv boundary.

    It sits mid-chain in main (``producer -> venv -> consumer``) and dials the child once.
    The venv boundary is a request/response splice of a **single** object -- the object never
    forks -- so the whole boundary rides one socket with the ``remote`` request/response
    protocol (``callRemote``), and the object is never re-opened on the main pipe:

    - **Framing** (``open`` + a single ``close``): sent forward via ``callRemote`` and then
      returned normally, so the engine's default *also* propagates the framing to the
      downstream consumer -- the same object opens/closes on both this node and, e.g., a
      ``response`` node, once. A bridge never sends ``closing``: the child's ``pipe.close()``
      already runs the closing pass and then the close pass, so a second frame would flush
      every node in the child twice (see ``closing`` below).
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
        """Drive the child's entire lifecycle end with the one ``close`` round-trip.

        The engine's ``pipe.close()`` already runs the closing pass and *then* the close pass
        (``pipe.instance.cpp``: ``Parent::closing()`` followed by ``Parent::close()``) -- which is
        why the client drives a pipe with ``pipe.close()`` alone (``data_conn.close_sync``). Sending
        a ``closing`` frame as well would run the closing pass twice in the child, so every node
        there would flush twice.

        The frame goes out from ``closing()`` and not from ``close()`` so the venv's return data
        reaches main's downstream nodes *before* their own ``closing()``: framing is bound per edge
        and a node's Python ``closing()`` runs before ``Parent::closing()`` hands off to its
        consumers, so a producer always closes ahead of everything it feeds.
        """
        try:
            self.callRemote('close')

        except ConnectionClosed:
            # The child tore the socket down before this frame: it died during the data phase, so
            # its error already crossed the boundary and failed this object. Letting the now
            # meaningless close frame raise would abort main's closing pass at the first error and
            # rob the downstream nodes of their flush. A dead socket under a *clean* object is a
            # child that died with nothing reported, so that still propagates.
            currentObject = self.instance.currentObject
            if currentObject is None or not currentObject.objectFailed:
                raise

            debug(
                Lvl.Remoting,
                'venv child closed the connection before its close frame; the object already '
                'carries the child failure, so main keeps closing',
            )

    def close(self):
        # Deliberately inert: `closing()` already drove the child's full close over the single
        # round-trip. The engine calls `Parent::close()` after this returns, so main's own framing
        # still propagates downstream exactly as before.
        pass
