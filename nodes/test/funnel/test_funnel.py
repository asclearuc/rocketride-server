# =============================================================================
# MIT License
# Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""Engine-free unit tests for the funnel node.

The funnel's forwarding is the engine's default — the node overrides no ``write*`` method
for a whole-value lane, so there is nothing there to unit-test and an override would be the
bug. What *is* testable is the media guard, and it is the only reason the node has code at
all: it keeps an interleaved stream a loud failure instead of a corrupt payload.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src' / 'nodes'))


class _Action:
    """Stand-in for an ``AVI_ACTION`` member, quirks included.

    The real members are pybind values that are **not** ints and do not compare equal to
    their own int (``0 == AVI_ACTION.BEGIN`` is False) — the trap ``lanes.py`` documents.
    Modelled here so a guard rewritten as ``action == AVI_ACTION.BEGIN`` against a caller
    holding the int, or as ``action == 0``, fails these tests instead of production.
    """

    def __init__(self, value: int):
        self._value = value

    def __int__(self) -> int:
        return self._value

    def __eq__(self, other) -> bool:
        return self is other

    __hash__ = None


BEGIN, WRITE, END = _Action(0), _Action(1), _Action(2)


def _install_stubs() -> None:
    rocketlib = types.ModuleType('rocketlib')

    class _Base:
        def __init__(self):
            self.prevented = 0

        def preventDefault(self):
            self.prevented += 1

    rocketlib.IInstanceBase = _Base
    rocketlib.IGlobalBase = object
    rocketlib.Entry = object
    rocketlib.AVI_ACTION = types.SimpleNamespace(BEGIN=BEGIN, WRITE=WRITE, END=END)
    sys.modules['rocketlib'] = rocketlib


@contextmanager
def _scoped_stubs() -> Iterator[None]:
    """Install the stubs for the import only, then put ``sys.modules`` back.

    Left installed, the stub is inherited by whatever shares this xdist worker, and the
    failure is silent and far from here -- the collection guard exists because it happened.
    """
    original = sys.modules.get('rocketlib')
    _install_stubs()
    try:
        yield
    finally:
        if original is None:
            sys.modules.pop('rocketlib', None)
        else:
            sys.modules['rocketlib'] = original


with _scoped_stubs():
    from funnel.IInstance import IInstance


@pytest.fixture
def funnel():
    node = IInstance()
    node.open(object())
    return node


def test_one_stream_passes(funnel):
    funnel.writeImage(BEGIN, 'image/png')
    funnel.writeImage(WRITE, 'image/png', b'x')
    funnel.writeImage(END, 'image/png')


def test_streams_may_follow_each_other(funnel):
    # The shape a well-behaved producer emits, and the shape two of them produce when each
    # emits inside one callback: sequential, never overlapping.
    for _ in range(2):
        funnel.writeImage(BEGIN, 'image/png')
        funnel.writeImage(END, 'image/png')


def test_an_overlapping_stream_is_refused(funnel):
    funnel.writeImage(BEGIN, 'image/png')
    with pytest.raises(ValueError, match='image'):
        funnel.writeImage(BEGIN, 'image/png')


def test_lanes_are_tracked_apart(funnel):
    # An open image stream must not make an audio stream look like an overlap; the guard is
    # per lane, and one shared flag would refuse a legitimate pipeline.
    funnel.writeImage(BEGIN, 'image/png')
    funnel.writeAudio(BEGIN, 'audio/wav')
    funnel.writeVideo(BEGIN, 'video/mp4')


def test_open_clears_a_stream_left_dangling(funnel):
    # A producer that dies mid-stream leaves the flag set. Without the reset the *next*
    # object would be refused for the previous one's failure.
    funnel.writeImage(BEGIN, 'image/png')
    funnel.open(object())
    funnel.writeImage(BEGIN, 'image/png')


def test_the_guard_never_suppresses_the_default_forward(funnel):
    # The whole design: the guard checks and gets out of the way. A preventDefault here
    # would silently drop every media frame the funnel was built to carry.
    funnel.writeImage(BEGIN, 'image/png')
    funnel.writeImage(WRITE, 'image/png', b'x')
    funnel.writeImage(END, 'image/png')
    assert funnel.prevented == 0


def test_whole_value_lanes_are_left_to_the_engine():
    # Forwarding is the default; an override that forwarded explicitly would emit twice.
    for method in ('writeText', 'writeJson', 'writeDocuments', 'writeTag'):
        assert method not in vars(IInstance), f'{method} must stay the engine default'
