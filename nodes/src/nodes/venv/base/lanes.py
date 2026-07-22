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
The venv bridge lane table -- the single source of truth for how every engine
data lane is serialized onto the bridge wire and reconstructed on the far side.

There is exactly one entry per engine data lane (the ``Binder::MethodNames`` list
in ``binder.hpp`` minus the ``open``/``closing``/``close`` framing lanes). Deriving
both the egress serialization and the ingress dispatch from this one table means a
new engine lane forces a visible change here -- never a silent gap. A unit test
cross-checks the table's keys against ``binder.hpp`` so that guarantee is enforced,
and ``words`` -- which has no ``writeWords`` landing method anywhere in ``rocketlib`` --
is carried as an explicit "not bridgeable" entry rather than dropped silently.

Each lane is bidirectional:

- ``encode(*write_args) -> (header_extra: dict, payload)`` runs on the egress side,
  turning the arguments of the node's ``write*`` call into a wire payload plus any
  extra header fields (audio/video/image put ``action``/``mime`` in the header so the
  buffer can travel as raw bytes).
- ``decode(instance, payload, header) -> None`` runs on the ingress side,
  deserializing the payload and calling the matching ``instance.write*`` method.

The serialization vocabulary mirrors ``ai/modules/data/data_conn.py`` (decision D2 in
the virtual-environments design doc) -- it is *not* imported, only mirrored -- and the
(de)serialization is deliberately per-type: ``Doc`` uses ``toDict``/``fromDict``,
``Question``/``Answer`` are pydantic (``model_dump``/``model_validate``), ``IJson`` uses
``IJson.toDict``/``IJson(dict)``, and ``TAG`` egress is ``tag.asBytes``.
"""

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, Tuple

from ai.common.schema import Answer, Doc, Question
from rocketlib import IJson


class LaneNotBridgeable(Exception):
    """Raised when a lane exists in the engine but cannot cross the bridge.

    Currently only ``words``: the engine binder lists a ``words`` lane, but there is
    no ``writeWords`` method on the ``rocketlib`` instance/pipe surface, so there is
    nothing to land it on. Kept as a loud, explicit failure so the gap is a wire-format
    bump rather than a silent hole.
    """


@dataclass(frozen=True)
class Lane:
    """One bridgeable engine data lane.

    Attributes:
        name: The engine (``binder.hpp``) lane name, e.g. ``'tags'``. This is the key
            used on the wire -- keying by the binder lane (rather than the write-method
            name, as ``remote`` does) keeps the ``binder.hpp`` cross-check trivial.
        method: The ``instance.write*`` method this lane maps to, e.g. ``'writeTag'``.
            The mapping is not mechanical (``tags`` -> ``writeTag`` is singular), so it
            is spelled out per lane.
        encode: ``(*write_args) -> (header_extra, payload)`` -- egress serialization.
        decode: ``(instance, payload, header) -> None`` -- ingress dispatch.
    """

    name: str
    method: str
    encode: Callable[..., Tuple[Dict[str, Any], Any]]
    decode: Callable[[Any, Any, Dict[str, Any]], None]


# ---------------------------------------------------------------------------
# Per-lane encode/decode helpers
# ---------------------------------------------------------------------------
# Egress helpers return (header_extra, payload); ingress helpers return None and
# call the matching instance.write* method.


def _encode_passthrough(value):
    """Encode a scalar (str/bytes) lane -- no header, payload is the value itself."""
    return {}, value


def _encode_tags(tag):
    # A TAG object crosses as its raw serialized bytes; the far side writes them back
    # verbatim via writeTag(bytes).
    return {}, tag.asBytes


def _decode_tags(instance, payload, header):
    instance.writeTag(payload)


def _decode_text(instance, payload, header):
    instance.writeText(payload)


def _decode_table(instance, payload, header):
    instance.writeTable(payload)


def _encode_json(ijson):
    # `writeJson` receives an IJson *instance*. `IJson.toDict` is a staticmethod that
    # does NOT accept an IJson instance (only a plain dict), so extract the value via
    # its JSON string form (VERIFIED against the shipped rocketlib).
    return {}, json.loads(str(ijson))


def _decode_json(instance, payload, header):
    instance.writeJson(IJson(payload))


def _make_av_encode():
    """Build an audio/video/image encoder.

    AV lanes are multi-arg: ``write*(action, mimeType, buffer=None)``. The metadata
    (``action``/``mime``) rides the JSON header so the buffer can travel as raw bytes
    (no base64). ``buffer`` is optional -- BEGIN/END frames carry no bytes, only WRITE
    does -- so a missing buffer becomes a ``None`` payload (wire type ``none``).

    NOTE (step 7): a >~1 MB AV buffer exceeds the WebSocket ``max_size`` and is not yet
    chunked -- raising the AV ceiling is part of the live-transport work in step 7.
    """

    def _encode(action, mimeType, buffer=None):
        return {'action': action, 'mime': mimeType}, buffer

    return _encode


def _make_av_decode(method_name):
    """Build an audio/video/image decoder for the given instance method name.

    Omits the buffer argument entirely when there is none (BEGIN/END), matching how
    the engine is actually driven, rather than passing an explicit ``None``.
    """

    def _decode(instance, payload, header):
        method = getattr(instance, method_name)
        action, mime = header['action'], header['mime']
        if payload is None:
            method(action, mime)
        else:
            method(action, mime, payload)

    return _decode


def _encode_questions(question):
    # mode='json' is required: the default model_dump() leaves enums (e.g. QuestionType)
    # as enum objects, which are not JSON-serializable for the wire (VERIFIED).
    return {}, question.model_dump(mode='json')


def _decode_questions(instance, payload, header):
    instance.writeQuestions(Question.model_validate(payload))


def _encode_answers(answers):
    # A list crosses as a JSON list; callRemote chunks large lists automatically.
    # mode='json' for the same enum-serialization reason as questions.
    return {}, [answer.model_dump(mode='json') for answer in answers]


def _decode_answers(instance, payload, header):
    instance.writeAnswers([Answer.model_validate(answer) for answer in payload])


def _encode_documents(documents):
    return {}, [doc.toDict() for doc in documents]


def _decode_documents(instance, payload, header):
    instance.writeDocuments([Doc.fromDict(doc) for doc in payload])


def _encode_classifications(classifications, classificationPolicy, classificationRules):
    return {}, {
        'classifications': classifications,
        'classificationPolicy': classificationPolicy,
        'classificationRules': classificationRules,
    }


def _decode_classifications(instance, payload, header):
    instance.writeClassifications(
        payload['classifications'],
        payload['classificationPolicy'],
        payload['classificationRules'],
    )


def _encode_classification_context(classifications):
    return {}, {'classifications': classifications}


def _decode_classification_context(instance, payload, header):
    instance.writeClassificationContext(payload['classifications'])


def _encode_words(*args):
    raise LaneNotBridgeable("lane 'words' is not bridgeable: no writeWords method exists")


def _decode_words(instance, payload, header):
    raise LaneNotBridgeable("lane 'words' is not bridgeable: no writeWords method exists")


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------

LANES: Dict[str, Lane] = {
    lane.name: lane
    for lane in (
        Lane('tags', 'writeTag', _encode_tags, _decode_tags),
        Lane('text', 'writeText', _encode_passthrough, _decode_text),
        Lane('table', 'writeTable', _encode_passthrough, _decode_table),
        Lane('words', 'writeWords', _encode_words, _decode_words),
        Lane('json', 'writeJson', _encode_json, _decode_json),
        Lane('audio', 'writeAudio', _make_av_encode(), _make_av_decode('writeAudio')),
        Lane('video', 'writeVideo', _make_av_encode(), _make_av_decode('writeVideo')),
        Lane('image', 'writeImage', _make_av_encode(), _make_av_decode('writeImage')),
        Lane('questions', 'writeQuestions', _encode_questions, _decode_questions),
        Lane('answers', 'writeAnswers', _encode_answers, _decode_answers),
        Lane('classifications', 'writeClassifications', _encode_classifications, _decode_classifications),
        Lane(
            'classificationContext',
            'writeClassificationContext',
            _encode_classification_context,
            _decode_classification_context,
        ),
        Lane('documents', 'writeDocuments', _encode_documents, _decode_documents),
    )
}

# The framing lanes are handled directly by the bridge base (they drive the object
# lifecycle through ``instance.pipe.*``, not ``instance.write*``), so they are NOT in
# this data-lane table. Named here only so the binder cross-check can subtract them.
FRAMING_LANES = frozenset({'open', 'closing', 'close'})


def encode(lane: str, *write_args) -> Tuple[Dict[str, Any], Any]:
    """Serialize a ``write*`` call for ``lane`` into ``(header_extra, payload)``."""
    return LANES[lane].encode(*write_args)


def decode(instance, lane: str, payload, header: Dict[str, Any]) -> None:
    """Deserialize a received ``lane`` payload and dispatch to ``instance.write*``."""
    LANES[lane].decode(instance, payload, header)


def method_for(lane: str) -> str:
    """The ``instance.write*`` method name a lane maps to."""
    return LANES[lane].method
