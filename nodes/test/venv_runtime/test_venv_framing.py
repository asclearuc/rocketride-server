# =============================================================================
# MIT License
# Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""
Framing tests for the venv bridge: a child object ends in exactly one round-trip.

The engine's ``pipe.close()`` already runs the closing pass and *then* the close pass
(``pipe.instance.cpp``: ``Parent::closing()`` + ``Parent::close()``), which is why the client
drives a pipe with ``pipe.close()`` alone (``data_conn.close_sync``). A bridge that forwards a
``closing`` frame as well therefore makes every node inside the child flush **twice**.

That was latent until engine #1667 rebound Python ``instance.closing`` from ``cb_close`` to
``cb_closing``: before it, ``pipe.closing()`` happened to perform both passes and the follow-up
``pipe.close()`` was inert, so the bridge was accidentally correct. These tests pin the collapsed
shape -- one ``close`` frame, sent from ``closing()`` -- so the second pass cannot come back
silently.

Engine-interpreter only (the bridge imports ``rocketlib``/``fastapi``/``websockets``), and
deliberately exercised against the **sources** rather than the ``dist`` copy: ``nodes/src`` goes on
the front of ``sys.path`` and a guard asserts that is really where the classes came from.
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

from websockets.exceptions import ConnectionClosed, ConnectionClosedError  # noqa: E402

from nodes.venv.base import IInstanceBase  # noqa: E402
from nodes.venv.client import IInstance as VenvClient  # noqa: E402


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


def _dead_socket(*args, **kwargs):
    raise ConnectionClosedError(None, None)


def _ingress(pipe):
    """A child-side bridge whose local pipeline is the recording pipe."""
    node = IInstanceBase()
    node.instance = SimpleNamespace(pipe=pipe)
    return node


def _assert_under_test_is_the_source(cls):
    origin = inspect.getfile(cls)
    assert str(_NODES_SRC) in origin, f'{cls.__name__} came from {origin}, not the sources under {_NODES_SRC}'


def test_the_classes_under_test_are_the_sources_not_the_dist_copy():
    # Guards the whole module: a dist import would silently test yesterday's build.
    _assert_under_test_is_the_source(VenvClient)
    _assert_under_test_is_the_source(IInstanceBase)


# ---------------------------------------------------------------------------
# Main side: one frame, sent from closing()
# ---------------------------------------------------------------------------


def test_client_sends_one_close_frame_at_closing_and_nothing_at_close():
    node = VenvClient()
    sent = []
    node.callRemote = lambda lane, data=None, header_extra=None: sent.append(lane)

    node.closing()
    assert sent == ['close'], 'closing() must end the child object with a single close frame'

    node.close()
    assert sent == ['close'], 'close() must be inert -- the round-trip already happened at closing()'


# ---------------------------------------------------------------------------
# Child side: close drives one pipe.close(), closing is refused
# ---------------------------------------------------------------------------


def test_close_lane_drives_pipe_close_once_and_never_pipe_closing():
    # The direct expression of the #1667 regression: driving `pipe.closing()` here as well is
    # what makes the child flush twice.
    pipe = _RecordingPipe()

    _ingress(pipe).callLocal('close', None)

    assert pipe.calls == ['close']


def test_closing_lane_is_rejected_with_a_named_cause():
    pipe = _RecordingPipe()

    with pytest.raises(ValueError, match='closing'):
        _ingress(pipe).callLocal('closing', None)

    assert pipe.calls == [], 'a refused closing frame must not drive the child pipe at all'


# ---------------------------------------------------------------------------
# Dead-socket guard on the one remaining framing round-trip
# ---------------------------------------------------------------------------


def test_dead_socket_is_swallowed_when_the_object_already_carries_the_child_failure():
    # The child died during the data phase: its error already crossed and failed the object, so
    # losing the now-meaningless close frame must not abort main's closing pass.
    node = VenvClient()
    node.callRemote = _dead_socket
    node.instance = SimpleNamespace(currentObject=SimpleNamespace(objectFailed=True))

    node.closing()


@pytest.mark.parametrize(
    'currentObject',
    [SimpleNamespace(objectFailed=False), None],
    ids=['clean-object', 'no-object'],
)
def test_dead_socket_propagates_when_nothing_reported_the_failure(currentObject):
    # A dead socket under a clean object is a child that died with nothing reported; swallowing
    # it would let the object complete as a false success.
    node = VenvClient()
    node.callRemote = _dead_socket
    node.instance = SimpleNamespace(currentObject=currentObject)

    with pytest.raises(ConnectionClosed):
        node.closing()
