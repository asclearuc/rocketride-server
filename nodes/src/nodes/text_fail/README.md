# text_fail

A test fixture: a text filter that always fails at `closing`, giving the virtual-environment merge-back a deterministic in-venv failure to carry back to the client.

## What it does

It swallows every `writeText` and raises `APERR(Ec.InvalidDocument, ...)` at `closing`. The
code is deliberately distinctive: the engine keeps an `APERR`'s own `ec` on the error it
records and collapses everything else to `Ec.Exception`, so a named code is what makes "the
child's real code reached the client" an observable assertion.

Used by `nodes/test/venv_runtime/test_venv_merge_back.py`.

Not meant for real pipelines: it is marked `internal`, so the canvas palette never offers it.

## Lanes

| Lane in | Lane out | Description |
|---------|----------|-------------|
| `text` | nothing | Consumed; the node never emits |

## Configuration

None.
