# =============================================================================
# MIT License
# Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""
Framing tests for the ``remote`` sub-pipeline bridge: the object ends in one round-trip.

``remote`` has the same shape as the venv bridge and the same latent defect. ``pipe.close()``
already runs the closing pass and *then* the close pass (``pipe.instance.cpp``), so forwarding a
``closing`` frame as well drives the remote closing pass twice. That was harmless while Python
``instance.closing`` was bound to ``cb_close`` -- the first frame did both passes and the second
was inert -- and became real when engine #1667 rebound it to ``cb_closing``. On the venv bridge the
same two-frame shape was measured to **deadlock** the boundary, not merely duplicate output.

One assertion here differs from the venv mirror on purpose. The venv bridge *rejects* a received
``closing`` frame, because both ends of that socket are always the same build. ``remote`` bridges
deployments that can skew, so an older client may still send ``closing`` + ``close``; it therefore
**ignores** the frame, which leaves exactly one closing pass under either engine binding.

``remote`` has no live stand, so this is the only guard behind that change.
"""

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip('rocketlib', reason='engine-interpreter only (the bridge pulls engLib)')

_REPO_ROOT = Path(__file__).resolve().parents[3]
_NODES_SRC = _REPO_ROOT / 'nodes' / 'src'
if str(_NODES_SRC) not in sys.path:
    sys.path.insert(0, str(_NODES_SRC))

from nodes.remote.base import IInstanceBase  # noqa: E402
from nodes.remote.client import IInstance as RemoteClient  # noqa: E402


class _RecordingPipe:
    """Stands in for ``instance.pipe`` and records which lifecycle calls the bridge drives."""

    def __init__(self):
        self.calls = []

    def open(self, entry):
        self.calls.append('open')

    def closing(self):
        self.calls.append('closing')

    def close(self):
        self.calls.append('close')


def _server(pipe):
    """A remote-side bridge whose local pipeline is the recording pipe."""
    node = IInstanceBase()
    node.instance = SimpleNamespace(pipe=pipe)
    return node


def test_the_classes_under_test_are_the_sources_not_the_dist_copy():
    for cls in (RemoteClient, IInstanceBase):
        origin = inspect.getfile(cls)
        assert str(_NODES_SRC) in origin, f'{cls.__name__} came from {origin}, not {_NODES_SRC}'


def test_client_sends_one_close_frame_at_closing_and_nothing_at_close():
    node = RemoteClient()
    sent = []
    node.callRemote = lambda lane, data=None: sent.append(lane)

    node.closing()
    assert sent == ['close'], 'closing() must end the remote object with a single close frame'

    node.close()
    assert sent == ['close'], 'close() must be inert -- the round-trip already happened at closing()'


def test_close_lane_drives_pipe_close_once_and_never_pipe_closing():
    pipe = _RecordingPipe()

    _server(pipe).callLocal('close', None)

    assert pipe.calls == ['close']


def test_closing_lane_is_ignored_rather_than_rejected():
    # Deliberately different from the venv bridge: an older remote client may still send this
    # frame, and ignoring it leaves exactly one closing pass instead of breaking that peer.
    pipe = _RecordingPipe()

    _server(pipe).callLocal('closing', None)

    assert pipe.calls == [], 'an ignored closing frame must not drive the remote pipe at all'
