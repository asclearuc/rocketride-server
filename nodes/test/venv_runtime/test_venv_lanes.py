# =============================================================================
# MIT License
# Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""
Structural unit tests for the venv bridge lane table (``nodes/venv/base/lanes.py``).

These run **bare** (no engine / no engLib). ``lanes.py`` imports ``ai.common.schema``
and ``rocketlib`` (which pulls ``engLib``), so we stub those in ``sys.modules`` before
loading it, then load ``lanes.py`` directly from its file path -- it has no relative
imports, so it loads standalone without dragging in the base transport / fastapi /
websockets.

Covered here (the cheap, high-value guards):
  - the table's keys match ``binder.hpp::MethodNames`` minus framing -- a new engine
    lane fails this loudly instead of leaving a silent gap;
  - ``words`` raises ``LaneNotBridgeable`` on both encode and decode;
  - per-lane dispatch structure: encode -> decode reaches the right ``instance.write*``,
    including the audio/video/image BEGIN/WRITE/END shape (optional buffer).

Real byte-level serialization (actual ``Doc``/``IJson``/``TAG`` round-trips) needs the
engine interpreter and is left to that tier -- the stubs here are shallow by design.
"""

import importlib.util
import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LANES_PATH = _REPO_ROOT / 'nodes' / 'src' / 'nodes' / 'venv' / 'base' / 'lanes.py'
_BINDER_HPP = _REPO_ROOT / 'packages' / 'server' / 'engine-lib' / 'engLib' / 'store' / 'headers' / 'binder.hpp'


# ---------------------------------------------------------------------------
# Shallow stubs for the engine/SDK types lanes.py imports at module scope
# ---------------------------------------------------------------------------
class _MockDoc:
    def __init__(self, data=None):
        self.data = data or {}

    def toDict(self):
        return self.data

    @staticmethod
    def fromDict(data):
        return _MockDoc(data)


class _MockPydantic:
    """Stand-in for pydantic Question/Answer: model_dump / model_validate."""

    def __init__(self, data=None):
        self.data = data or {}

    def model_dump(self, mode=None):
        return self.data

    @classmethod
    def model_validate(cls, data):
        return cls(data)


class _MockQuestion(_MockPydantic):
    pass


class _MockAnswer(_MockPydantic):
    pass


class _MockAviAction:
    """Stand-in for one ``AVI_ACTION`` member.

    Deliberately faithful to the pybind original in the three ways that bite: it is not
    an ``int``, it is not JSON-serializable, and it does not compare equal to its own
    int. The earlier version of these tests passed a bare ``7`` instead, which is why
    the bridge shipped an unserializable header for every AV frame.
    """

    def __init__(self, value, name):
        self.value = value
        self.name = name

    def __int__(self):
        return self.value

    def __repr__(self):
        return f'<AVI_ACTION.{self.name}: {self.value}>'


class _MockAVIACTION:
    BEGIN = _MockAviAction(0, 'BEGIN')
    WRITE = _MockAviAction(1, 'WRITE')
    END = _MockAviAction(2, 'END')


class _MockIJson:
    def __init__(self, data):
        self.data = data

    def __str__(self):
        # lanes._encode_json extracts an IJson instance's value via json.loads(str(ijson)).
        return json.dumps(self.data)


_STUBBED_MODULES = ('rocketlib', 'ai', 'ai.common', 'ai.common.schema')


def _install_stubs():
    rocketlib = MagicMock()
    rocketlib.IJson = _MockIJson
    rocketlib.AVI_ACTION = _MockAVIACTION
    sys.modules['rocketlib'] = rocketlib

    schema = MagicMock()
    schema.Doc = _MockDoc
    schema.Question = _MockQuestion
    schema.Answer = _MockAnswer
    ai_common = MagicMock()
    ai_common.schema = schema
    ai = MagicMock()
    ai.common = ai_common
    sys.modules['ai'] = ai
    sys.modules['ai.common'] = ai_common
    sys.modules['ai.common.schema'] = schema


def _load_lanes():
    """Load lanes.py standalone under shallow stubs, then restore ``sys.modules``.

    The stubs are installed only long enough to import ``lanes.py`` -- once loaded,
    the module's globals keep the stub types, so the tests use them regardless. We
    restore the real modules immediately so this test never pollutes ``sys.modules``
    for the rest of a shared pytest session (e.g. under ``builder nodes:test``, where
    the engine-tier tests need the real ``rocketlib``/``ai``).
    """
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULES}
    try:
        _install_stubs()
        spec = importlib.util.spec_from_file_location('venv_lanes_under_test', _LANES_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, original in saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


lanes = _load_lanes()


class _Capture:
    """Records every ``write*`` call made on it as (method_name, args)."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _record(*args):
            self.calls.append((name, args))

        return _record


# ---------------------------------------------------------------------------
# binder.hpp cross-check -- the strongest guard against a silent lane gap
# ---------------------------------------------------------------------------
def _binder_data_lanes():
    text = _BINDER_HPP.read_text(encoding='utf-8', errors='ignore')
    match = re.search(r'MethodNames\s*=\s*\{([^}]*)\}', text)
    assert match, 'could not find Binder::MethodNames in binder.hpp'
    names = re.findall(r'"([^"]+)"', match.group(1))
    return [n for n in names if n not in ('open', 'closing', 'close')]


def test_table_covers_exactly_binder_data_lanes():
    binder = set(_binder_data_lanes())
    assert binder, 'binder.hpp parse produced no lanes'
    assert set(lanes.LANES.keys()) == binder, (
        'venv lane table drifted from binder.hpp::MethodNames. '
        f'missing={binder - set(lanes.LANES)} extra={set(lanes.LANES) - binder}'
    )


def test_framing_lanes_are_not_in_the_data_table():
    # Framing is handled directly by the base (instance.pipe.*), never via the table.
    for framing in ('open', 'closing', 'close'):
        assert framing not in lanes.LANES


# ---------------------------------------------------------------------------
# words -- explicit "not bridgeable", never a silent drop
# ---------------------------------------------------------------------------
def test_words_encode_raises():
    with pytest.raises(lanes.LaneNotBridgeable):
        lanes.encode('words', b'anything')


def test_words_decode_raises():
    with pytest.raises(lanes.LaneNotBridgeable):
        lanes.decode(_Capture(), 'words', None, {})


# ---------------------------------------------------------------------------
# Per-lane dispatch structure (encode -> decode reaches the right write*)
# ---------------------------------------------------------------------------
def _roundtrip(lane, *write_args):
    header, payload = lanes.encode(lane, *write_args)
    inst = _Capture()
    lanes.decode(inst, lane, payload, header)
    assert len(inst.calls) == 1, f'{lane}: expected exactly one write* call, got {inst.calls}'
    return header, payload, inst.calls[0]


def test_scalar_lanes():
    for lane, method, value in (
        ('text', 'writeText', 'hello'),
        ('table', 'writeTable', 'a|b|c'),
    ):
        header, payload, (called, args) = _roundtrip(lane, value)
        assert header == {}
        assert payload == value
        assert called == method
        assert args == (value,)


def test_tags_lane():
    tag = MagicMock(asBytes=b'tag-bytes')
    header, payload, (called, args) = _roundtrip('tags', tag)
    assert payload == b'tag-bytes'
    assert called == 'writeTag'
    assert args == (b'tag-bytes',)


def test_json_lane():
    ijson = _MockIJson({'k': 'v'})
    header, payload, (called, args) = _roundtrip('json', ijson)
    assert payload == {'k': 'v'}
    assert called == 'writeJson'
    assert isinstance(args[0], _MockIJson)
    assert args[0].data == {'k': 'v'}


_AV_LANES = [('audio', 'writeAudio'), ('video', 'writeVideo'), ('image', 'writeImage')]


@pytest.mark.parametrize('lane,method', _AV_LANES)
def test_av_write_frame(lane, method):
    header, payload, (called, args) = _roundtrip(lane, _MockAVIACTION.WRITE, 'image/png', b'buf')
    assert header == {'action': 1, 'mime': 'image/png'}
    assert payload == b'buf'
    assert called == method
    # The action arrives as the MEMBER, not the int: node code branches on
    # `action == AVI_ACTION.WRITE`, which an int never satisfies.
    assert args == (_MockAVIACTION.WRITE, 'image/png', b'buf')


@pytest.mark.parametrize('lane,method', _AV_LANES)
def test_av_begin_end_frame_has_no_buffer(lane, method):
    # BEGIN/END carry action+mime but no buffer -> decode must call the 2-arg form.
    header, payload, (called, args) = _roundtrip(lane, _MockAVIACTION.BEGIN, 'audio/wav')
    assert header == {'action': 0, 'mime': 'audio/wav'}
    assert payload is None
    assert called == method
    assert args == (_MockAVIACTION.BEGIN, 'audio/wav')  # no buffer argument at all


@pytest.mark.parametrize('lane,method', _AV_LANES)
@pytest.mark.parametrize('action', [_MockAVIACTION.BEGIN, _MockAVIACTION.WRITE, _MockAVIACTION.END])
def test_av_header_is_json_serializable(lane, method, action):
    """The defect the OCR split found: the header went to `json.dumps` with the enum
    still in it, so EVERY audio/video/image frame died at the venv boundary. No driver
    had ever sent an AV lane across one, and these tests passed a bare int.
    """
    header, _payload = lanes.encode(lane, action, 'image/png', b'buf')
    assert json.loads(json.dumps(header)) == header


@pytest.mark.parametrize('lane,method', _AV_LANES)
def test_av_decode_rejects_an_unknown_action(lane, method):
    inst = _Capture()
    with pytest.raises(ValueError, match='unknown AVI action'):
        lanes.decode(inst, lane, b'buf', {'action': 99, 'mime': 'image/png'})
    assert inst.calls == []


@pytest.mark.parametrize('lane,method', _AV_LANES)
def test_av_accepts_a_plain_int_action(lane, method):
    """`int()` on encode rather than a lookup, so a caller already holding the int
    is unaffected -- and it still arrives decoded as the member.
    """
    header, payload, (called, args) = _roundtrip(lane, 2, 'image/png')
    assert header == {'action': 2, 'mime': 'image/png'}
    assert args == (_MockAVIACTION.END, 'image/png')


def test_questions_lane():
    q = _MockQuestion({'q': 'why'})
    header, payload, (called, args) = _roundtrip('questions', q)
    assert payload == {'q': 'why'}
    assert called == 'writeQuestions'
    assert isinstance(args[0], _MockQuestion)


def test_answers_lane():
    answers = [_MockAnswer({'a': 1}), _MockAnswer({'a': 2})]
    header, payload, (called, args) = _roundtrip('answers', answers)
    assert payload == [{'a': 1}, {'a': 2}]
    assert called == 'writeAnswers'
    assert [a.data for a in args[0]] == [{'a': 1}, {'a': 2}]


def test_documents_lane():
    docs = [_MockDoc({'d': 1}), _MockDoc({'d': 2})]
    header, payload, (called, args) = _roundtrip('documents', docs)
    assert payload == [{'d': 1}, {'d': 2}]
    assert called == 'writeDocuments'
    assert [d.data for d in args[0]] == [{'d': 1}, {'d': 2}]


def test_classifications_lane():
    header, payload, (called, args) = _roundtrip('classifications', {'c': 1}, {'p': 2}, {'r': 3})
    assert payload == {'classifications': {'c': 1}, 'classificationPolicy': {'p': 2}, 'classificationRules': {'r': 3}}
    assert called == 'writeClassifications'
    assert args == ({'c': 1}, {'p': 2}, {'r': 3})


def test_classification_context_lane():
    header, payload, (called, args) = _roundtrip('classificationContext', {'c': 1})
    assert payload == {'classifications': {'c': 1}}
    assert called == 'writeClassificationContext'
    assert args == ({'c': 1},)


def test_every_data_lane_has_a_dispatch_test():
    # Guard: if a lane is added to the table, make sure it is not left untested here.
    tested = {
        'text',
        'table',
        'tags',
        'json',
        'audio',
        'video',
        'image',
        'questions',
        'answers',
        'documents',
        'classifications',
        'classificationContext',
    }
    assert set(lanes.LANES) - {'words'} == tested
