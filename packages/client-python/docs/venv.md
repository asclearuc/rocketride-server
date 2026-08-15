---

title: Virtual Environments (client.venv)
---

# Virtual Environments — `client.venv`

A pipeline can put its components inside a **virtual environment container**,
and their dependencies are then installed into an isolated `site-packages`
tree of their own — an **overlay**, kept on disk under
`venvs/<projectId>/<envId>/`.

An overlay is a **cache, not authored data**. The requirements live in the
pipeline document; the next run regenerates and reinstalls whatever you
reclaim here. Deleting one costs that run's install time and nothing else.

The `client.venv` namespace is how you see those overlays and get the disk
back.

## Whose disk

**Every method here acts on the server you are connected to, not on the
machine running your code.** The server resolves the overlay root next to its
own executable. Against a local engine that is your own disk and the
distinction is invisible; against a remote or cloud engine it is emphatically
not.

`venv.list()` with no `project_id` enumerates **every overlay on that
machine** — not "yours". Overlays are keyed by project id and nothing ties one
to an account, so an unfiltered listing shows the pipelines you have forgotten
about, which is the whole point of asking. Pass `project_id` when you mean
"this pipeline's".

## Methods

| Method | Description |
| --- | --- |
| `venv.list(project_id=None, *, sizes=False)` | Overlays on the server; `project_id` filters, `sizes` adds byte counts |
| `venv.purge(project_id, env_id)` | Empty one environment's `site-packages`, keeping its compiled requirement files |
| `venv.delete_env(project_id, env_id)` | Remove one environment's overlay directory outright |
| `venv.delete_project(project_id)` | Remove the whole `venvs/<project_id>/` subtree; returns how many went |
| `venv.gc(project_id, *, max_age_days=None, dry_run=False)` | Reclaim that project's overlays nothing has used lately; returns a report |

`env_id` is the **container node's id** in the pipeline document. Pass both ids
raw — the server resolves them literal-first and shortens them itself, exactly
as it did when the overlay was created. Shortening client-side would address a
plausible, absent directory and report success.

`delete_project` removes the project's **overlay subtree**. It does not touch
the pipeline, the registry, or anything else the word "project" might suggest.

> **`sizes` is slow.** It walks every populated `site-packages` recursively —
> on the order of half a million `stat` calls on a real tree. Ask for it when
> you are hunting disk, not on a page that refreshes.

## Collecting by age

`gc` is the odd one out: it names no environment. It removes the overlays of
one project that nothing has activated for longer than a threshold, and leaves
the rest — the same collection the server runs across every project on its own
schedule, aimed at a project you choose.

```python
report = await client.venv.gc('proj-abc', max_age_days=30, dry_run=True)
for row in report['collected']:
    print(row['projectId'], row['envId'], row['ageSeconds'])
```

Read `maxAgeSeconds` back rather than assuming your own number: the server
enforces a minimum age, so `max_age_days=0` collects nothing recent and the
report shows you the floor it applied. `scanned` counts the overlays examined,
which excludes every environment of a project that was skipped.

`project_id` is required. Overlays are machine-local disk state that no team
owns, so naming the project is what stops one caller reclaiming another's — the
unscoped, whole-machine form exists only inside the server.

## Three outcomes, not two

This section describes the **boolean** methods. `gc` is shaped differently: it
returns a report and does not raise over a live project (see below).

`purge` and `delete_env` return a bool, and it is **not** pass/fail:

| Result | Meaning |
| --- | --- |
| `True` | An overlay was there and has been reclaimed |
| `False` | There was none — idempotent success, and the normal answer for a container that has never run |
| raises | The server refused; the message names the cause |

`delete_project` follows the same rule with a count: `0` means the project had
no overlays, not that anything failed.

## When the server refuses

- **A run of that project is live.** The gate is per **project**, not per
  environment: any running source of the pipeline holds its overlays open.
  Stop the run and retry.
- **A file is still held open.** On Windows an engine that is still resident
  can hold an overlay's `.pyd`/`.dll`, and the wipe fails naming the file.
  This is a known residual; the message tells you which process to stop.
- **Missing arguments or permissions.** `purge` and `delete_env` need both ids;
  `delete_project` and `gc` take no `env_id`. Listing needs `task.monitor`, the
  four destructive methods need `task.control`.

Failures arrive as a `RuntimeError` carrying the server's own text. Show it
unreworded — it names the cause.

**`gc` does not follow the first rule.** A live project does not make it raise;
it comes back as a `skipped` row and the call succeeds. Wrapping `gc` in a
`try`/`except RuntimeError` expecting the sibling behaviour catches nothing —
check `report['skipped']` instead. It still raises for the last rule, missing
arguments and permissions, and reports per-overlay problems as `failed` rows
rather than raising on the first one.

## Usage

```python
import asyncio
from rocketride import RocketRideClient


async def main():
    client = RocketRideClient({'uri': 'ws://localhost:5565'})
    await client.connect()

    # What is on the server's disk, biggest first
    overlays = await client.venv.list(sizes=True)
    for o in sorted(overlays, key=lambda x: x.get('bytes', 0), reverse=True):
        state = 'installed' if o.get('installed') else 'empty'
        print(f"{o['projectId']}/{o['envId']}  {state}  {o.get('bytes', 0):,} bytes")

    # Just this pipeline's
    mine = await client.venv.list('proj-abc')

    # Reclaim one environment's packages, keeping the container
    purged = await client.venv.purge('proj-abc', 'venv_1')
    print('Reclaimed' if purged else 'Nothing to reclaim — it was never installed')

    # Remove one environment entirely
    await client.venv.delete_env('proj-abc', 'venv_1')

    # Clean up after deleting a pipeline
    removed = await client.venv.delete_project('proj-abc')
    print(f'Removed {removed} environment(s)')

    await client.disconnect()


asyncio.run(main())
```

Honouring a refusal:

```python
try:
    await client.venv.purge('proj-abc', 'venv_1')
except RuntimeError as e:
    # The engine's message names the cause: an active run, or the file that
    # is still held open. Do not reword it.
    print(f'Error: {e}')
```

## CLI

```bash
rocketride venv list                        # every overlay on the server
rocketride venv list proj-abc               # one pipeline's
rocketride venv list --sizes                # with byte counts (slow)
rocketride venv purge proj-abc venv_1       # empty one environment
rocketride venv delete proj-abc venv_1      # remove one environment
rocketride venv delete-project proj-abc     # remove a pipeline's subtree
rocketride venv gc proj-abc --dry-run       # what age would reclaim, without doing it
rocketride venv gc proj-abc --max-age-days 30 --json
```

`venv list` and `venv gc` honour `--json` — both answer with something worth
diffing between runs, which the boolean commands do not. The destructive
commands print what they are about to do and report the count afterwards; there
is no confirmation prompt — these are scriptable by design. `gc`'s plain output
always lists the skipped and failed rows in full: those are the half that tells
you why an overlay survived.

## On the canvas

Both editor hosts wire the same operations onto the container itself:

- **Purge packages** in the container's overflow menu, guarded by a
  confirmation. The item is disabled while a run of the pipeline is live, and
  says so in its label.
- **Deleting the container** asks two questions: do its member nodes go with
  it, and does the environment go from the server's disk. Every delete route
  asks — the menu, the Delete key, a region select.
- **Deleting the pipeline** reclaims its overlays best-effort. A pipeline
  deleted outside the app is collected later by orphan cleanup instead.

## Related

- [Run Logs (`client.log`)](/develop/python/log) — the continuum, including a
  run's package-install output
- [Observability](/protocols/websocket/observability) — the `rrext_venv`
  protocol command this namespace wraps
