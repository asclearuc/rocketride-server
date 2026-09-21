# text_revert

A test fixture: a trivial text filter that emits the character-reversed text (`hello` → `olleh`), giving the virtual-environment round-trip a deterministic, verifiable payload.

## What it does

It buffers each object's text and writes the reversed result at `closing`, the
buffer-then-emit shape of `anonymize`. The live checks in `virtual-environments.md` run it
inside a container, for example `webhook → [venv: text_revert → response]`.

Not meant for real pipelines.

## Lanes

| Lane in | Lane out | Description |
|---------|----------|-------------|
| `text` | `text` | The input, character-reversed, emitted at `closing` |

## Configuration

None.
