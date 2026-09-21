# text_grandchild

A test fixture: spawns a long-running process of its own and emits that PID as its text, for the orphan-safe teardown check (`virtual-environments.md` §7 step 8.5B).

## What it does

The spawned process is a plain `subprocess.Popen` sleeping for 600 s, so unlike an engine
it carries no `--autoterm` stdin monitor and holds no pipe from the server — the class of
survivor that cooperative teardown cannot reach. Emitting the PID lets a check read it off
the pipeline result and then ask the OS directly whether the process outlived the run.

Not meant for real pipelines: it is marked `internal`, so the canvas palette never offers it.

## Lanes

| Lane in | Lane out | Description |
|---------|----------|-------------|
| `text` | `text` | The PID of the spawned process, in place of the input |

## Configuration

None.
