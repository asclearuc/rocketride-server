# =============================================================================
# MIT License
# Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""
Engine-interpreter round-trip tests for the venv bridge lane table.

Unlike ``test_venv_lanes.py`` (bare, shallow stubs), this exercises the lane table
against the **real** ``rocketlib``/``ai`` types, so it only runs under the engine
interpreter (e.g. ``dist/server/python.exe -m pytest``). Under bare Python the
``importorskip`` below skips the whole module, because ``import rocketlib`` pulls
``engLib`` (the C++ binding).

Its job is the thing stubs cannot prove: that the wire payload each lane produces is
actually JSON-serializable, and that decode reconstructs the real type. This is what
caught two real bugs -- ``IJson`` extraction and pydantic ``model_dump`` leaving enums
on the wire -- so these assertions are the regression guard for them.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# Skip the whole module unless the real engine libs are importable.
pytest.importorskip('rocketlib', reason='engine-interpreter only (rocketlib pulls engLib)')
pytest.importorskip('ai.common.schema', reason='engine-interpreter only')

from rocketlib import IJson  # noqa: E402
from ai.common.schema import Answer, Doc, Question  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LANES_PATH = _REPO_ROOT / 'nodes' / 'src' / 'nodes' / 'venv' / 'base' / 'lanes.py'

# nodes/src on the path so lanes.py's `from ai.common.schema import ...` resolves.
if str(_REPO_ROOT / 'nodes' / 'src') not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / 'nodes' / 'src'))

_spec = importlib.util.spec_from_file_location('venv_lanes_engine_under_test', _LANES_PATH)
lanes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lanes)


class _Capture:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _record(*args):
            self.calls.append((name, args))

        return _record


def _roundtrip(lane, *write_args):
    header, payload = lanes.encode(lane, *write_args)
    inst = _Capture()
    lanes.decode(inst, lane, payload, header)
    assert len(inst.calls) == 1, f'{lane}: expected one write* call, got {inst.calls}'
    return header, payload, inst.calls[0]


def _assert_wire_json_safe(payload):
    # The bridge ships json-typed payloads through json.dumps -- an enum or other
    # non-serializable object here is exactly the bug class this guards against.
    json.dumps(payload)


def test_json_lane_real_ijson():
    original = {'k': 'v', 'n': [1, 2], 'nested': {'a': True}}
    header, payload, (called, args) = _roundtrip('json', IJson(original))
    _assert_wire_json_safe(payload)
    assert payload == original
    assert called == 'writeJson'
    reconstructed = args[0]
    assert isinstance(reconstructed, IJson)
    assert json.loads(str(reconstructed)) == original


def test_questions_lane_real_pydantic_is_json_safe():
    # Question().model_dump() (default) leaves QuestionType as an enum -> not JSON-safe.
    # The lane must use mode='json'; this asserts the payload survives json.dumps.
    header, payload, (called, args) = _roundtrip('questions', Question())
    _assert_wire_json_safe(payload)
    assert called == 'writeQuestions'
    assert isinstance(args[0], Question)


def test_answers_lane_real_pydantic_is_json_safe():
    header, payload, (called, args) = _roundtrip('answers', [Answer(), Answer()])
    _assert_wire_json_safe(payload)
    assert isinstance(payload, list) and len(payload) == 2
    assert called == 'writeAnswers'
    assert all(isinstance(a, Answer) for a in args[0])


def test_documents_lane_real_doc():
    header, payload, (called, args) = _roundtrip('documents', [Doc(), Doc()])
    _assert_wire_json_safe(payload)
    assert called == 'writeDocuments'
    reconstructed = args[0]
    assert all(isinstance(d, Doc) for d in reconstructed)
    # Doc -> toDict -> fromDict -> toDict is stable.
    assert [d.toDict() for d in reconstructed] == payload


def test_scalar_and_av_and_classification_lanes():
    # No engine types, but confirm they behave under the real module too.
    assert _roundtrip('text', 'hi')[2] == ('writeText', ('hi',))
    assert _roundtrip('table', 'a|b')[2] == ('writeTable', ('a|b',))
    assert _roundtrip('image', 3, 'image/png', b'bytes')[2] == ('writeImage', (3, 'image/png', b'bytes'))
    assert _roundtrip('audio', 1, 'audio/wav')[2] == ('writeAudio', (1, 'audio/wav'))  # BEGIN: no buffer
    _, ctx_payload, ctx_call = _roundtrip('classificationContext', {'c': 1})
    _assert_wire_json_safe(ctx_payload)
    assert ctx_call == ('writeClassificationContext', ({'c': 1},))
    _, cls_payload, cls_call = _roundtrip('classifications', {'c': 1}, {'p': 2}, {'r': 3})
    _assert_wire_json_safe(cls_payload)
    assert cls_call[0] == 'writeClassifications'


def test_words_still_not_bridgeable_under_real_types():
    with pytest.raises(lanes.LaneNotBridgeable):
        lanes.decode(_Capture(), 'words', None, {})
