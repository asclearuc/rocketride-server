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

"""
The shared venv bridge base.

This is the ``venv`` sibling of ``remote/base/IInstance.py`` (design decision A2:
a new base under ``venv`` only, leaving network-``remote`` untouched). It carries the
WebSocket transport, the ``callRemote``/``callLocal`` request/response machinery and
list chunking -- and, unlike ``remote`` which hand-writes a handful of lanes in each of
its client and server nodes, it drives **every** engine data lane from the single
``lanes`` table (``lanes.py``).

Two things live here that ``remote`` splits across client/server, because both venv
nodes need them:

- ``callLocal`` (table-driven) runs on **both** sides -- ``venv_server`` uses it for the
  forward path (main -> venv), and the egress client's ``callRemote`` receive-loop uses
  it for return lanes (venv -> main).
- the 12 data ``write*`` egress overrides -- ``remote`` puts these in both its client
  (forward) and its server (return) nodes; here they sit once in the base and both
  subclasses inherit them. The client adds the connect lifecycle + framing overrides;
  the server adds the accept loop.

Header threading (design decision B1): audio/video/image put ``action``/``mime`` in the
JSON header so the media buffer travels as raw bytes. ``_send``/``_recv``/``callRemote``/
``callLocal`` therefore carry an extra header dict compared with ``remote`` -- but only
in this venv copy, so ``remote`` is unaffected.
"""

import asyncio
import json

import nest_asyncio
from fastapi import WebSocket as ServerConnection
from websockets.sync.client import ClientConnection

from rocketlib import APERR, Ec, Entry, IInstanceBase, Lvl, debug, error

from . import lanes, merge

# Enable running the async coroutines in a synchronous context
nest_asyncio.apply()


def _ecFromCode(code):
    """Rebuild an engine error code from the child's integer, falling back to ``Ec.Failed``.

    A ``try``/``except`` alone is not enough. Verified against the shipped engine: an unknown
    *positive* value yields a nameless ``Ec.???`` rather than raising -- only a negative or a
    non-integer raises -- so the constructed value has to be checked by name.
    """
    try:
        ec = type(Ec.NoErr)(code)
    except Exception:
        return Ec.Failed

    return Ec.Failed if '?' in str(ec) else ec


def _childErrorMessage(childError: dict) -> str:
    """The child's message with its own source location folded in.

    Only the fallback path needs this: ``completionCode`` builds its ``APERR`` inside the binding,
    so the structured ``file``/``line``/``function`` would point at ``bindings.cpp`` instead of the
    node that actually failed. The decoration path keeps them intact and leaves the message alone.
    """
    origin = '{}:{} {}'.format(childError.get('file'), childError.get('line'), childError.get('function'))
    return '{} (in venv at {})'.format(childError.get('message'), origin)


class IInstance(IInstanceBase):
    """WebSocket bridge base shared by the ``venv`` and ``venv_server`` nodes."""

    # -------------------------------------------------------------------------
    # Connection lifecycle
    # -------------------------------------------------------------------------
    def connect(self, webSocket: (ClientConnection, ServerConnection)):
        # Check connection state and type
        if self._webSocket:
            raise Exception('WebSocket already connected')
        elif not webSocket:
            raise ValueError('WebSocket not specified')
        elif isinstance(webSocket, ClientConnection):
            pass
        elif isinstance(webSocket, ServerConnection):
            pass
        else:
            raise ValueError(f'Invalid WebSocket: {webSocket}')

        # Store connection
        self._webSocket = webSocket

    def disconnect(self):
        if self._webSocket:
            if isinstance(self._webSocket, ClientConnection):
                # Close client connection
                self._webSocket.close()
            # elif isinstance(self._webSocket, ServerConnection):
            #     pass
            else:
                raise Exception(f'Unexpected WebSocket state: {self._webSocket}')

            # Release connection object
            self._webSocket = None

    # -------------------------------------------------------------------------
    # Transport
    # -------------------------------------------------------------------------
    def _send(self, lane: str, data: None | str | bytes | dict | list = None, header_extra: dict = None):
        # Determine data type
        if isinstance(data, str):
            datatype = 'str'
        elif isinstance(data, bytes):
            datatype = 'bytes'
        elif isinstance(data, (dict, list)):
            datatype = 'json'
        elif data is None:
            datatype = 'none'
        else:
            raise TypeError(f'Unknown data type {type(data)}')

        # Create the message header. Any extra fields (e.g. audio/video/image
        # `action`/`mime`) ride here so the payload can travel as raw bytes.
        header = {'lane': lane, 'type': datatype}
        if header_extra:
            header.update(header_extra)

        if isinstance(self._webSocket, ClientConnection):
            # Send the header
            self._webSocket.send(json.dumps(header))

            # Prepare json data
            if isinstance(data, (dict, list)):
                data = json.dumps(data)

            # Send the actual data
            if data is not None:
                self._webSocket.send(data)

        elif isinstance(self._webSocket, ServerConnection):
            # Send the header
            IInstance.runAsync(self._webSocket.send_json(header))

            # Send the actual data
            if datatype == 'str':
                IInstance.runAsync(self._webSocket.send_text(data))
            elif datatype == 'bytes':
                IInstance.runAsync(self._webSocket.send_bytes(data))
            elif datatype == 'json':
                IInstance.runAsync(self._webSocket.send_json(data))

        else:
            raise Exception(f'Unexpected WebSocket state: {self._webSocket}')

    def _recv(self) -> (str, None | str | bytes | dict | list, dict):
        if isinstance(self._webSocket, ClientConnection):
            # Get the data response string
            headerStr = self._webSocket.recv()

            # Convert from json
            header = json.loads(headerStr)

            # Grab the type
            lane, datatype = header['lane'], header['type']

            # Receive data according to its type
            if datatype == 'str':
                data = self._webSocket.recv()
            elif datatype == 'bytes':
                data = self._webSocket.recv()
            elif datatype == 'json':
                data_str = self._webSocket.recv()
                data = json.loads(data_str)
            elif datatype == 'none':
                data = None
            else:
                raise TypeError(f'Unknown data type {datatype}')

        elif isinstance(self._webSocket, ServerConnection):
            # Get the data response json
            header = IInstance.runAsync(self._webSocket.receive_json())

            # Grab the type
            lane, datatype = header['lane'], header['type']

            # Receive data according to its type
            if datatype == 'str':
                data = IInstance.runAsync(self._webSocket.receive_text())
            elif datatype == 'bytes':
                data = IInstance.runAsync(self._webSocket.receive_bytes())
            elif datatype == 'json':
                data = IInstance.runAsync(self._webSocket.receive_json())
            elif datatype == 'none':
                data = None
            else:
                raise TypeError(f'Unknown data type {datatype}')

        else:
            raise Exception(f'Unexpected WebSocket state: {self._webSocket}')

        # Return the lane, its data, and the full header (carries action/mime for AV)
        return lane, data, header

    # -------------------------------------------------------------------------
    # Bridge dispatch
    # -------------------------------------------------------------------------
    def callRemote(self, lane: str, data: None | str | bytes | dict | list = None, header_extra: dict = None):
        """
        Send lane data to the bridged pipeline.

        Handle all responses with the local pipeline.
        """
        dataChunks = None
        if data and isinstance(data, list):
            # Split the list into sublists, each with a size smaller than WebSocket max_size.
            dataChunks = IInstance.listChunks(data)
        else:
            dataChunks = (data,)

        for dataChunk in dataChunks:
            # Send the call to the bridged pipeline
            self._send(lane, dataChunk, header_extra)

            while True:
                # Receive the next call from the bridged pipeline
                rspLane, rspData, rspHeader = self._recv()

                # Check if the bridged pipeline completed the call and returned an error code.
                if rspLane == 'error':
                    ccode = APERR.fromDict(rspData)
                    if ccode.ec != Ec.NoErr:
                        # Only on a real error: this branch is also the *success* terminator, and
                        # decorating there would consume the stash on a clean ack and silently
                        # disable the fallback that exists for a child which fails without raising.
                        self._decorateWithChildFailure(ccode)
                    ccode.check_raise()
                    break

                try:
                    # Send it to the local pipeline
                    self.callLocal(rspLane, rspData, rspHeader)

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

    def callLocal(self, lane: str, data, header: dict = None):
        """
        Send specified lane data to the local pipeline.

        Framing lanes drive the object lifecycle through ``self.instance.pipe.*``; every data
        lane is dispatched through the ``lanes`` table, which writes to ``self.instance.write*``.
        The framing pair is ``open``/``close`` only -- ``pipe.close()`` runs the closing pass and
        then the close pass, so a ``closing`` frame would double the child's flush and is rejected.
        """
        if lane == 'open':
            if not isinstance(data, dict):
                raise TypeError(f'Unexpected data type {type(data)} for lane {lane}')

            url = data['url']
            del data['url']

            self._obj = Entry(url)  # preserve object from release
            self._obj.fromDict(data)

            self.instance.pipe.open(self._obj)

        elif lane == 'closing':
            # A bridge drives the child's lifecycle end with `close` alone: `pipe.close()` runs the
            # closing pass and then the close pass by itself. Honouring a `closing` frame as well
            # would flush every node in the child twice, and silently -- so say so instead.
            raise ValueError(
                'Unexpected framing lane "closing": a venv bridge ends the child object with a '
                'single "close" frame, because pipe.close() already runs the closing pass; '
                'honouring this frame would run the closing pass of the child twice'
            )

        elif lane == 'close':
            if data is not None:
                raise TypeError(f'Unexpected data {data} for lane {lane}')

            self.instance.pipe.close()

        elif lane == 'entry':
            # Merge-back (§4.12). NOT a `lanes.py` entry on purpose: this is our own frame, not an
            # engine lane, and it must never become bindable.
            #
            # It lives in the base rather than the client subclass because BOTH roles reach it
            # through `callLocal`. It does not recurse across a chain: under graph serialization
            # (§4.6) a bridge is never nested inside a child -- every venv's bridge node lives in
            # main -- so each child's entry merges DIRECTLY into main's root entry, one level.
            self._applyChildEntry(data)

        else:
            # Every data lane -- including `words`, which raises LaneNotBridgeable --
            # is dispatched from the single table.
            lanes.decode(self.instance, lane, data, header or {})

    # -------------------------------------------------------------------------
    # Merge-back (§4.12): fold a child's entry into this side's object
    # -------------------------------------------------------------------------
    def _applyChildEntry(self, data):
        """Apply a child's ``entry`` frame to the local object. Never raises.

        Two halves land differently. The **response** is merged here and now. The **failure** is
        only *stashed*: the child's provenance rides home on a ``__formatted`` decoration of the
        exception the boundary already raises, which lets the engine reconstruct the child's exact
        code and Python source location. ``_applyStashedChildFailure`` is the safety net for a
        child that fails without raising.

        Mirrors ``data_conn.close_sync``'s extract-despite-failure shape: whatever goes wrong here,
        the caller must still ack, so nothing escapes.
        """
        try:
            if not isinstance(data, dict):
                raise TypeError(f'Unexpected data type {type(data)} for lane entry')

            entry = self.instance.currentObject
            if entry is None:
                debug(Lvl.Remoting, 'venv merge-back: no open object on this side, dropping the child entry')
                return

            # Apply whitelist: the response only. The frame carries the child's whole entry so the
            # whitelist can widen later without a wire change, but identity (objectId/instanceId/
            # version/parentId) must never overwrite this side's.
            childResponse = data.get('response') or {}
            if childResponse:
                merged = merge.merge_response(entry.response.toDict(), childResponse)
                for key, value in merged.items():
                    entry.response[key] = value

            if data.get('objectFailed'):
                self._childError = data.get('completionError')

        except Exception as e:
            error(e)

    def _decorateWithChildFailure(self, ccode: APERR):
        """Carry the child's code and Python source location home on the raised exception.

        ``bindings.cpp`` tags engine errors raised into Python with ``__formatted`` plus
        ``code``/``message``/``filename``/``function``/``line``, and ``call.hpp`` restores those
        fields verbatim -- *before* it looks at an ``APERR``'s own ``ec``. Decorating the exception
        the boundary already raises therefore hands the engine the child's real error, with no
        reconstruction on our side.

        This matters because the ``error`` lane cannot carry it: the child wraps whatever a node
        raised into ``APERR(Ec.RemoteException, ...)``, so the ``entry`` frame is the only carrier
        of the child's true code.

        Consumes the stash, so the fallback does not also fire.
        """
        childError = self._childError
        if not childError:
            return

        self._childError = None

        # All five attributes or none: `call.hpp` reads them unconditionally, and a missing one
        # makes the cast throw, which degrades to a generic exception -- worse than not decorating.
        required = ('code', 'message', 'file', 'line', 'function')
        if any(childError.get(key) is None for key in required):
            debug(
                Lvl.Remoting, f'venv merge-back: incomplete child error {childError}, leaving the boundary error as is'
            )
            return

        # `__formatted` written inside a class body would be name-mangled to `_IInstance__formatted`
        # and the engine's hasattr() would miss it -- so set it by name.
        setattr(ccode, '__formatted', True)
        setattr(ccode, 'code', childError['code'])
        setattr(ccode, 'message', childError['message'])
        setattr(ccode, 'filename', childError['file'])  # completionError exposes `file`
        setattr(ccode, 'line', childError['line'])
        setattr(ccode, 'function', childError['function'])

    def _applyStashedChildFailure(self):
        """Fallback for a child that failed *without* raising across the boundary.

        A node can set a completion code and still return cleanly, so the child acks OK and this
        side sees no exception to decorate. Applying the code here keeps that failure from
        vanishing. Never raises.
        """
        childError = self._childError
        if not childError:
            return

        self._childError = None

        try:
            entry = self.instance.currentObject
            if entry is None or entry.objectFailed:
                debug(
                    Lvl.Remoting,
                    f'venv merge-back: not applying the child failure, object already failed: {childError}',
                )
                return

            entry.completionCode(_ecFromCode(childError.get('code')), _childErrorMessage(childError))

        except Exception as e:
            error(e)

    def _encode_and_send(self, lane: str, *write_args):
        """Serialize a ``write*`` call via the table, ship it over the bridge, and suppress
        the engine's default downstream propagation.

        Both bridge roles the engine data-drives ship a data lane over the socket with the
        synchronous request/response ack protocol (``callRemote``): the main round-trip node
        sends the forward stream, the child return egress sends the stream back. Neither may
        ALSO let the engine forward the same lane to the next local filter -- the main node
        would leak the forward stream into its return consumer, and the child egress is
        terminal -- so after the round-trip we ``preventDefault`` to drop the default write.
        The return values the peer sends back on the ack channel re-enter through
        ``callRemote``'s receive loop (``callLocal`` -> ``self.instance.write*``), downstream.
        """
        header_extra, payload = lanes.encode(lane, *write_args)
        self.callRemote(lane, payload, header_extra)
        self.preventDefault()

    # -------------------------------------------------------------------------
    # Data-lane egress overrides (inherited by both the client and the server).
    # Each forwards through the single lane table; the engine only calls the ones
    # bound to this bridge node, so no per-lane config gating is needed.
    # -------------------------------------------------------------------------
    def writeTag(self, tag):
        self._encode_and_send('tags', tag)

    def writeText(self, text):
        self._encode_and_send('text', text)

    def writeTable(self, table):
        self._encode_and_send('table', table)

    def writeJson(self, data):
        self._encode_and_send('json', data)

    def writeAudio(self, action, mimeType, buffer=None):
        self._encode_and_send('audio', action, mimeType, buffer)

    def writeVideo(self, action, mimeType, buffer=None):
        self._encode_and_send('video', action, mimeType, buffer)

    def writeImage(self, action, mimeType, buffer=None):
        self._encode_and_send('image', action, mimeType, buffer)

    def writeQuestions(self, question):
        self._encode_and_send('questions', question)

    def writeAnswers(self, answer):
        self._encode_and_send('answers', answer)

    def writeDocuments(self, documents):
        self._encode_and_send('documents', documents)

    def writeClassifications(self, classifications, classificationPolicy, classificationRules):
        self._encode_and_send('classifications', classifications, classificationPolicy, classificationRules)

    def writeClassificationContext(self, classifications):
        self._encode_and_send('classificationContext', classifications)

    # -------------------------------------------------------------------------
    # Static helpers (verbatim from remote/base -- transport-agnostic utilities)
    # -------------------------------------------------------------------------
    @staticmethod
    def runAsync(asyncAction):
        """
        Run an async coroutine in a synchronous context using the current event loop.
        """
        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(asyncAction)
        return result

    @staticmethod
    def estimateJsonLength(obj):
        """
        Estimates the JSON-serialized length (in characters) of a Python object.
        """
        if obj is None:
            return 4
        elif isinstance(obj, bool):
            return 4 if obj else 5
        elif isinstance(obj, (int, float)):
            return len(str(obj))
        elif isinstance(obj, str):
            return len(obj) + 2  # quotes
        elif isinstance(obj, list):
            if not obj:
                return 2
            return sum(IInstance.estimateJsonLength(i) + 1 for i in obj) + 1
        elif isinstance(obj, dict):
            if not obj:
                return 2
            total = 2
            for i, (k, v) in enumerate(obj.items()):
                total += len(k) + 2  # key with quotes
                total += 1  # colon
                total += IInstance.estimateJsonLength(v)
                if i != len(obj) - 1:
                    total += 1  # comma
            return total
        else:
            raise TypeError(f'Unsupported type: {type(obj)}')

    @staticmethod
    def listChunks(data: list):
        """
        Split a list of data items into chunks.

        Ensure that the estimated JSON-encoded size of each chunk does not exceed
        the WebSocket `max_size`.
        """
        MAX_CHUNK_SIZE = int(0.98 * (2**20))  # WebSocket max_size with error factor
        chunkIdx, chunkSize = 0, 0
        for idx in range(len(data)):
            itemSize = IInstance.estimateJsonLength(data[idx])
            if chunkSize + itemSize >= MAX_CHUNK_SIZE:
                yield data[chunkIdx:idx]
                chunkIdx, chunkSize = idx, itemSize
            else:
                chunkSize += itemSize
        yield data[chunkIdx:]

    _webSocket: (ClientConnection, ServerConnection) = None
    _obj: Entry = None

    # The child's `completionError`, held between the `entry` frame and whichever of the two
    # failure paths consumes it -- exactly one of them must, or a run reports two different errors.
    _childError: dict = None
