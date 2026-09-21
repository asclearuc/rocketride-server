# text_to_json

A test fixture: a text → json filter that emits during the data phase, for the virtual-environment diamond check (`virtual-environments.md` step 8.3).

## What it does

It writes `{"len": ..., "text": ...}` as soon as it receives text and swallows the text.
`text_revert` emits at `closing`, so a diamond built only from it would have every branch
arriving flush-time; this node keeps exactly one flush-time branch and makes a dropped
branch attributable. It also makes the second branch carry real data — `webhook` declares
the `json` lane but does not emit it for a `text/plain` send.

Not meant for real pipelines.

## Lanes

| Lane in | Lane out | Description |
|---------|----------|-------------|
| `text` | `json` | `{"len": <length>, "text": <input>}`, emitted on receipt |

## Configuration

None.
