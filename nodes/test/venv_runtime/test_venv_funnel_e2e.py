# =============================================================================
# MIT License — Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""End-to-end acceptance for the `funnel` node on a **media** lane.

A venv boundary carries at most one producer per lane (§4.6), so two parallel nodes in one
environment cannot both reach out. `funnel` is the answer: it becomes the single producer.
Three runs, and the middle one is the only one a unit test could not have made:

* **no funnel** — the partitioner refuses the shape, naming the lane;
* **funnel, atomic producers** — two whole images arrive, distinct, in one object;
* **funnel, non-atomic producers** — the guard fires instead of splicing them.

The media lane is the one that matters. `writeImage` is `BEGIN`/`WRITE`/`END` with no
stream id, so an interleave is unrecoverable, and the funnel is the only thing standing
between "the partitioner refused it" and a silently corrupt payload. The `text` lane needs
none of this: one call carries one whole value.

Engine-spawning and slow, like its neighbour `test_venv_conflict_e2e.py`; unlike the other
files here it needs no package index, because the fixtures pin nothing.
"""

from __future__ import annotations

import base64
import json

import pytest


pytestmark = [pytest.mark.asyncio, pytest.mark.xdist_group('venv_funnel_e2e')]

_MIME = 'image/png'


def _find_streams(node):
    """Pull the decoded media entries out of a result, wherever the SDK nests them.

    Searched rather than indexed on purpose: the shape of a `send` result is the client's
    business and has moved before. What this test is about is *how many* streams came out
    and whether they stayed apart, so it looks for the entries and ignores the wrapper.
    """
    found = []
    if isinstance(node, dict):
        if 'stream_index' in (node.get('metadata') or {}):
            found.append(node)
        for value in node.values():
            found.extend(_find_streams(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_find_streams(value))
    return found


def _member(node_id: str, provider: str, inputs, y: int):
    return {
        'id': node_id,
        'provider': provider,
        'config': {},
        'input': [{'lane': lane, 'from': src} for lane, src in inputs],
        'ui': {'position': {'x': 60, 'y': y}, 'nodeType': 'default', 'parentId': 'v1'},
    }


def _document(project_id: str, provider: str, *, funnel: bool):
    """`webhook(main) -> [v1: two producers (+ funnel)] -> response_image(main)`.

    Source and response stay in main for the same reason as the conflict acceptance: a
    document whose nodes all sit in venvs is either rejected or collapses to one process,
    and either failure would read as a funnel bug.
    """
    members = [
        _member('img_1', provider, [('text', 'webhook_1')], 90),
        _member('img_2', provider, [('text', 'webhook_1')], 220),
    ]
    if funnel:
        members.append(_member('funnel_1', 'funnel', [('image', 'img_1'), ('image', 'img_2')], 350))
        sink_inputs = [{'lane': 'image', 'from': 'funnel_1'}]
    else:
        sink_inputs = [{'lane': 'image', 'from': 'img_1'}, {'lane': 'image', 'from': 'img_2'}]

    return {
        'project_id': project_id,
        'source': 'webhook_1',
        'components': [
            {'id': 'webhook_1', 'provider': 'webhook', 'config': {'mode': 'Source'}},
            {
                'id': 'v1',
                'provider': 'default',
                'config': {
                    'environment': {'name': 'shared', 'isolated': True},
                    'pipeline': {'components': members},
                },
            },
            {'id': 'response_image_1', 'provider': 'response_image', 'config': {}, 'input': sink_inputs},
        ],
    }


async def _run(client, document):
    """Start the document, push one text object through it, and return the result.

    `use_existing` plus terminate-in-finally, because the project id is fixed and its token
    is derived from it: a resident task would refuse the next run.
    """
    started = await client.use(pipeline=document, ttl=0, use_existing=True)
    token = started['token'] if isinstance(started, dict) else started
    try:
        return await client.send(token, 'hello', mimetype='text/plain')
    finally:
        try:
            await client.terminate(token)
        except Exception:
            pass  # best effort: a lingering task must not mask the assertion


async def test_two_parallel_producers_need_a_funnel(client):
    """Without one the shape does not even start, and the refusal names the lane."""
    document = _document('nodes-test-funnel-control', 'vtest_image', funnel=False)
    with pytest.raises(Exception) as excinfo:
        await _run(client, document)
    message = str(excinfo.value)
    assert 'image' in message, message
    assert 'more than one producer' in message, message


async def test_a_funnel_carries_both_streams_out_whole(client):
    """The headline: two producers, one boundary, two images that stayed apart."""
    document = _document('nodes-test-funnel-atomic', 'vtest_image', funnel=True)
    result = await _run(client, document)

    images = _find_streams(result)
    assert len(images) == 2, json.dumps(result, default=str)
    # Distinct streams, not one spliced buffer -- the engine numbers them as it decodes.
    assert sorted(img['metadata']['stream_index'] for img in images) == [0, 1]
    for img in images:
        assert base64.b64decode(img['image']) == b'IMG[hello]'
        assert img['mime_type'] == _MIME


async def test_an_interleaved_stream_is_refused_not_spliced(client):
    """The guard, against a producer that really does span callbacks.

    `vtest_image_split` opens on `writeText` and closes at `closing`, so two of them
    interleave for real. Without the guard the two buffers would merge into one unreadable
    image and the run would report success -- the one outcome worse than failing.
    """
    document = _document('nodes-test-funnel-split', 'vtest_image_split', funnel=True)

    # The guard raises inside the pipe, so it reaches the caller as a failed write rather
    # than as a result body -- the same way any node exception does.
    with pytest.raises(Exception) as excinfo:
        await _run(client, document)
    assert 'a second "image" stream began before the first ended' in str(excinfo.value)
