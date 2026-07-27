# =============================================================================
# MIT License
# Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""
Tests for venv response/failure merge-back (design §4.12).

A ``response``/``end`` node that lands *inside* a venv writes into the **child's** entry, which no
client ever reads -- ``data_conn.close_sync`` only ever reads main's root entry. Merge-back ships
the child's entry home over an ``entry`` frame and folds it into main's object.

Two tiers, split the way the lane tests already are:

- the **merge algebra** is pure and loads from ``merge.py`` by path, so it runs under a bare
  interpreter with no engine;
- the **wiring** (frame handling, the stash, the decoration, the fallback, the child-side send)
  needs the bridge, so it runs under the engine interpreter -- against the **sources**, with a
  guard that says so.
"""

import importlib.util
import inspect
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_NODES_SRC = _REPO_ROOT / 'nodes' / 'src'
_MERGE_PATH = _NODES_SRC / 'nodes' / 'venv' / 'base' / 'merge.py'
_BINDER_HPP = _REPO_ROOT / 'packages' / 'server' / 'engine-lib' / 'engLib' / 'store' / 'headers' / 'binder.hpp'

_spec = importlib.util.spec_from_file_location('venv_merge_under_test', _MERGE_PATH)
merge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(merge)


# ---------------------------------------------------------------------------
# Tier 1 -- the merge algebra (bare, no engine)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'main, child, expected, why',
    [
        ({}, {'text': ['a']}, {'text': ['a']}, 'empty main takes the child wholesale'),
        ({'text': ['a']}, {}, {'text': ['a']}, 'empty child leaves main untouched'),
        (
            {'text': ['a']},
            {'text': ['b']},
            {'text': ['a', 'b']},
            'lists concatenate with main first, they do not replace',
        ),
        (
            {'result_types': {'text': 'text'}},
            {'result_types': {'json': 'json'}},
            {'result_types': {'text': 'text', 'json': 'json'}},
            'dicts merge deep, so result_types unions without special-casing',
        ),
        (
            {'metadata': {'a': 1, 'shared': 'main'}},
            {'metadata': {'b': 2, 'shared': 'child'}},
            {'metadata': {'a': 1, 'b': 2, 'shared': 'child'}},
            'nested scalars are child-wins',
        ),
        ({'name': 'main'}, {'name': 'child'}, {'name': 'child'}, 'top-level scalars are child-wins'),
        (
            {'text': ['a'], 'only_main': 1},
            {'text': ['b'], 'only_child': 2},
            {'text': ['a', 'b'], 'only_main': 1, 'only_child': 2},
            'keys unique to either side survive',
        ),
        ({'x': ['a']}, {'x': 'scalar'}, {'x': 'scalar'}, 'mismatched types fall back to child-wins'),
    ],
)
def test_merge_response_rules(main, child, expected, why):
    assert merge.merge_response(main, child) == expected, why


def test_merge_response_does_not_mutate_its_inputs():
    main = {'text': ['a'], 'nested': {'k': 'v'}}
    child = {'text': ['b'], 'nested': {'k2': 'v2'}}

    merge.merge_response(main, child)

    assert main == {'text': ['a'], 'nested': {'k': 'v'}}
    assert child == {'text': ['b'], 'nested': {'k2': 'v2'}}


def test_entry_is_not_an_engine_lane():
    # `entry` is our own frame. If it ever collided with a real binder lane, the framing branch
    # would silently swallow that lane's data.
    text = _BINDER_HPP.read_text(encoding='utf-8', errors='ignore')
    match = re.search(r'MethodNames\s*=\s*\{([^}]*)\}', text)
    assert match, 'could not find Binder::MethodNames in binder.hpp'

    assert 'entry' not in re.findall(r'"([^"]+)"', match.group(1))


# ---------------------------------------------------------------------------
# Tier 2 -- the wiring (engine interpreter, against the sources)
# ---------------------------------------------------------------------------

pytest.importorskip('rocketlib', reason='engine-interpreter only (the bridge pulls engLib)')

if str(_NODES_SRC) not in sys.path:
    sys.path.insert(0, str(_NODES_SRC))

from rocketlib import APERR, Ec  # noqa: E402

from nodes.venv.base import IInstanceBase  # noqa: E402
from nodes.venv.client import IInstance as VenvClient  # noqa: E402
from nodes.venv.server import IInstance as VenvServer  # noqa: E402

_CHILD_ERROR = {
    'message': 'boom',
    'code': int(Ec.InvalidDocument),
    'file': 'nodes/text_fail/IInstance.py',
    'line': 42,
    'function': 'closing',
}


class _Response(dict):
    """Stands in for the live ``IJson`` response proxy: per-key writes, ``toDict`` reads."""

    def toDict(self):
        return dict(self)


class _Entry:
    def __init__(self, response=None, failed=False):
        self.response = _Response(response or {})
        self.objectFailed = failed
        self.completionCodeCalls = []

    def completionCode(self, ec, message):
        self.completionCodeCalls.append((ec, message))
        self.objectFailed = True


def _bridge(cls, entry=None):
    node = cls()
    node.instance = SimpleNamespace(currentObject=entry)
    return node


def _entry_frame(response=None, failed=False, error=None):
    # Shaped like the real frame: the child's whole `entry.toDict()` plus the failure pair, which
    # `toDict` deliberately excludes.
    return {
        'name': 'x.txt',
        'objectId': 'child-object-id',
        'response': response or {},
        'objectFailed': failed,
        'completionError': error,
    }


def test_the_classes_under_test_are_the_sources_not_the_dist_copy():
    for cls in (IInstanceBase, VenvClient, VenvServer):
        origin = inspect.getfile(cls)
        assert str(_NODES_SRC) in origin, f'{cls.__name__} came from {origin}, not {_NODES_SRC}'


def test_entry_frame_merges_the_child_response_into_the_open_object():
    entry = _Entry({'text': ['main']})
    node = _bridge(IInstanceBase, entry)

    node.callLocal('entry', _entry_frame({'text': ['child'], 'result_types': {'text': 'text'}}))

    assert entry.response.toDict() == {'text': ['main', 'child'], 'result_types': {'text': 'text'}}


def test_entry_frame_never_overwrites_this_side_identity():
    entry = _Entry()
    node = _bridge(IInstanceBase, entry)

    node.callLocal('entry', _entry_frame({'text': ['child']}))

    # The frame carries the child's whole entry, but only the response is applied.
    assert 'objectId' not in entry.response.toDict()
    assert 'name' not in entry.response.toDict()


def test_entry_frame_stashes_the_failure_rather_than_applying_it():
    entry = _Entry()
    node = _bridge(IInstanceBase, entry)

    node.callLocal('entry', _entry_frame(failed=True, error=_CHILD_ERROR))

    assert entry.completionCodeCalls == [], 'the failure rides the exception, not the merge'
    assert node._childError == _CHILD_ERROR


def test_entry_frame_survives_having_no_open_object():
    node = _bridge(IInstanceBase, None)

    node.callLocal('entry', _entry_frame({'text': ['child']}))  # must not raise


def test_entry_frame_never_raises_on_a_malformed_payload():
    node = _bridge(IInstanceBase, _Entry())

    node.callLocal('entry', 'not-a-dict')  # must not raise: the caller still has to ack


# --- failure propagation: decorate first, merge as the safety net -----------


def test_boundary_error_is_decorated_with_the_child_provenance():
    node = _bridge(IInstanceBase, _Entry())
    node._childError = dict(_CHILD_ERROR)
    ccode = APERR(Ec.RemoteException, 'wrapped by the boundary')

    node._decorateWithChildFailure(ccode)

    # `hasattr(e, '__formatted')` is exactly what the engine probes -- a name-mangled attribute
    # would pass a naive test but fail there.
    assert hasattr(ccode, '__formatted')
    assert getattr(ccode, 'code') == int(Ec.InvalidDocument)
    assert getattr(ccode, 'message') == 'boom'
    assert getattr(ccode, 'filename') == 'nodes/text_fail/IInstance.py'  # completionError says `file`
    assert getattr(ccode, 'line') == 42
    assert getattr(ccode, 'function') == 'closing'
    assert node._childError is None, 'the stash must be consumed exactly once'


def test_decoration_is_skipped_when_the_child_error_is_incomplete():
    # `call.hpp` reads all five attributes unconditionally; a partial decoration degrades the
    # error to a generic exception, which is worse than not decorating at all.
    node = _bridge(IInstanceBase, _Entry())
    node._childError = {'message': 'boom', 'code': int(Ec.InvalidDocument)}
    ccode = APERR(Ec.RemoteException, 'wrapped')

    node._decorateWithChildFailure(ccode)

    assert not hasattr(ccode, '__formatted')


def test_an_unrelated_boundary_error_is_left_undecorated():
    node = _bridge(IInstanceBase, _Entry())
    ccode = APERR(Ec.RemoteException, 'nothing to do with a child entry')

    node._decorateWithChildFailure(ccode)

    assert not hasattr(ccode, '__formatted')


def test_the_fallback_applies_the_child_code_when_nothing_raised():
    entry = _Entry()
    node = _bridge(IInstanceBase, entry)
    node._childError = dict(_CHILD_ERROR)

    node._applyStashedChildFailure()

    assert len(entry.completionCodeCalls) == 1
    ec, message = entry.completionCodeCalls[0]
    assert ec == Ec.InvalidDocument
    assert 'boom' in message
    assert 'nodes/text_fail/IInstance.py:42' in message, 'the fallback folds provenance into the text'
    assert node._childError is None


def test_the_fallback_does_not_fire_after_the_decoration_consumed_the_stash():
    entry = _Entry()
    node = _bridge(IInstanceBase, entry)
    node._childError = dict(_CHILD_ERROR)

    node._decorateWithChildFailure(APERR(Ec.RemoteException, 'wrapped'))
    node._applyStashedChildFailure()

    assert entry.completionCodeCalls == [], 'one run must not report two different errors'


def test_the_fallback_only_logs_when_this_side_already_failed():
    entry = _Entry(failed=True)
    node = _bridge(IInstanceBase, entry)
    node._childError = dict(_CHILD_ERROR)

    node._applyStashedChildFailure()

    assert entry.completionCodeCalls == []


@pytest.mark.parametrize(
    'code, expected',
    [
        (int(Ec.InvalidDocument), Ec.InvalidDocument),
        (999999, Ec.Failed),
        (-1, Ec.Failed),
        (None, Ec.Failed),
    ],
    ids=['known', 'unknown-positive', 'negative', 'missing'],
)
def test_the_fallback_error_code_survives_or_degrades_to_failed(code, expected):
    # An unknown *positive* value does not raise -- the engine hands back a nameless `Ec.???` --
    # so a test written as "expect an exception" would pass for the wrong reason.
    entry = _Entry()
    node = _bridge(IInstanceBase, entry)
    node._childError = dict(_CHILD_ERROR, code=code)

    node._applyStashedChildFailure()

    assert entry.completionCodeCalls[0][0] == expected


def _scripted_round_trip(node, terminator):
    """Drive ``callRemote`` against a scripted peer that answers with ``terminator``."""
    node._send = lambda lane, data=None, header_extra=None: None
    node._recv = lambda: ('error', terminator.toDict(), {})


def test_a_clean_terminator_leaves_the_stash_for_the_fallback():
    # `callRemote`'s error branch is also the *success* terminator. Decorating there
    # unconditionally would eat the stash on a clean ack and disable the fallback entirely.
    node = _bridge(IInstanceBase, _Entry())
    node._childError = dict(_CHILD_ERROR)
    _scripted_round_trip(node, APERR())

    node.callRemote('close')

    assert node._childError == _CHILD_ERROR


def test_a_failing_terminator_raises_the_decorated_child_error():
    node = _bridge(IInstanceBase, _Entry())
    node._childError = dict(_CHILD_ERROR)
    _scripted_round_trip(node, APERR(Ec.RemoteException, 'wrapped by the boundary'))

    with pytest.raises(APERR) as raised:
        node.callRemote('close')

    assert hasattr(raised.value, '__formatted')
    assert getattr(raised.value, 'code') == int(Ec.InvalidDocument)
    assert node._childError is None


def test_a_new_object_drops_a_stash_left_by_the_previous_one():
    node = _bridge(VenvClient, _Entry())
    node._childError = dict(_CHILD_ERROR)
    node.callRemote = lambda lane, data=None, header_extra=None: None

    node.open(SimpleNamespace(toDict=lambda: {}, url='file:///x.txt'))

    assert node._childError is None


# --- child side: what actually gets shipped --------------------------------


class _ChildEntry:
    def __init__(self, response=None, failed=False, error=None):
        self._response = response or {}
        self.objectFailed = failed
        self.completionError = error

    def toDict(self):
        # `toDict` always emits at least `name`, which is why the skip rule cannot be a payload
        # emptiness check.
        payload = {'name': 'x.txt'}
        if self._response:
            payload['response'] = self._response
        return payload


def _child(obj):
    node = VenvServer()
    node._obj = obj
    node.sent = []
    node.callRemote = lambda lane, data=None, header_extra=None: node.sent.append((lane, data))
    return node


def test_child_ships_the_entry_when_it_produced_a_response():
    node = _child(_ChildEntry({'text': ['olleh']}))

    node._mergeBackEntry()

    assert len(node.sent) == 1
    lane, payload = node.sent[0]
    assert lane == 'entry'
    assert payload['response'] == {'text': ['olleh']}
    assert payload['objectFailed'] is False


def test_child_ships_the_entry_when_it_failed_even_with_no_response():
    node = _child(_ChildEntry(failed=True, error=_CHILD_ERROR))

    node._mergeBackEntry()

    assert len(node.sent) == 1
    assert node.sent[0][1]['completionError'] == _CHILD_ERROR


def test_child_stays_off_the_wire_when_it_has_nothing_to_contribute():
    # The pre-merge-back shape: a venv with no `response` node that simply succeeds.
    node = _child(_ChildEntry())

    node._mergeBackEntry()

    assert node.sent == []


def test_child_ships_at_most_one_entry_frame_per_object():
    node = _child(_ChildEntry({'text': ['olleh']}))

    node._mergeBackEntry()
    node._mergeBackEntry()

    assert len(node.sent) == 1


def test_child_ships_nothing_when_no_object_was_opened():
    # A fresh or pooled-and-reused instance, and the return-egress instance which never opens one.
    node = _child(None)

    node._mergeBackEntry()

    assert node.sent == []


def test_child_merge_back_never_raises():
    # It runs on the failure path, where an exception here would replace the original one on its
    # way to the caller's error ack.
    node = _child(_ChildEntry({'text': ['olleh']}))

    def _boom(*args, **kwargs):
        raise RuntimeError('socket is gone')

    node.callRemote = _boom

    node._mergeBackEntry()
