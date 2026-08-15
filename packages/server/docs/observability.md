---
title: Observability
---

# Observability

RocketRide exposes runtime observability (task lifecycle, periodic status,
resource metrics, and per-component flow traces) as a **live event stream over
the same [WebSocket](/protocols/websocket) the engine already speaks**. You open a
socket, subscribe to the event types you care about, and the engine pushes events
as a run unfolds. There is no separate metrics endpoint to scrape and no history
database to query: it is **not** OpenTelemetry, Prometheus, Sentry, or webhooks. To
keep history, connect, subscribe, and persist the events as they arrive.

The [TypeScript](/develop/typescript) and [Python](/develop/python) SDKs frame all
of this for you (`getTaskStatus()`, `onEvent`, `setEvents()` / `add_monitor`), so
you rarely touch the wire directly. This page documents the protocol surface so you
can debug it, build a dashboard, or write your own ingester.

## Subscribing: `rrext_monitor`

Subscriptions are managed with the `rrext_monitor` [DAP
request](/protocols/websocket#requests). The engine keeps a per-connection registry
of which event types you want and which tasks they cover. Send it once the
connection is authenticated (the initial `auth` handshake described on the
[WebSocket](/protocols/websocket#connection) page):

```json
{
	"type": "request",
	"seq": 2,
	"command": "rrext_monitor",
	"token": "*",
	"arguments": {
		"types": ["TASK", "SUMMARY", "FLOW", "OUTPUT", "SSE"]
	}
}
```

`token: "*"` subscribes to every task your API token owns: the recommended scope
for an ingestion service. Subscriptions are per-connection and not durable: on
reconnect, resubscribe.

### Event types

`types` accepts case-insensitive `EVENT_TYPE` strings (or the equivalent integer
bitmask, e.g. `36` = `SUMMARY | TASK`):

| String      | Bit  | What you get                                                         |
| ----------- | ---- | -------------------------------------------------------------------- |
| `NONE`      | 0    | Unsubscribe (clears the registry entry)                              |
| `DEBUGGER`  | 1    | DAP debug-protocol passthrough (stopped, threads, …)                 |
| `DETAIL`    | 2    | Real-time per-object processing updates                              |
| `SUMMARY`   | 4    | Periodic full `TASK_STATUS` snapshots, best for dashboards           |
| `OUTPUT`    | 8    | Engine log / output lines                                            |
| `FLOW`      | 16   | Pipeline component flow events (requires a trace level, see below)   |
| `TASK`      | 32   | Lifecycle: `running`, `begin`, `end`, `restart`                      |
| `SSE`       | 64   | Custom node-to-UI messages emitted by nodes via `monitorSSE()`       |
| `DASHBOARD` | 128  | Server-level events (connections, monitor changes)                   |
| `BILLING`   | 256  | Billing ledger events (credits/debits), org-scoped                   |
| `DEPLOY`    | 512  | Deployment-change invalidations (`apaevt_deploy`: pointer, state, schedule, and run mutations), org-scoped — re-fetch on receipt, the body carries identity only |
| `ALL`       | 1023 | Everything above                                                     |

### Subscription scope

Replace `token: "*"` to narrow or widen what you receive. The scope IS the
kind: adding `teamId` addresses the team's DEPLOYED run of the pipeline;
omitting it addresses your own dev run (there is no run-kind argument).

| Scope                      | Set with                                    | Receives                                       |
| -------------------------- | ------------------------------------------- | ---------------------------------------------- |
| One running task           | `token`                                     | Events for that task only                      |
| Your dev run (any restart) | `projectId` + `source`                      | Your own dev run of that pipeline              |
| A team's deployed run      | `teamId` + `projectId` + `source`           | That team's deploy run of the pipeline         |
| One pipe within a pipeline | `projectId` + `source` + `pipeId`           | That one pipe                                  |
| All sources in a project   | `projectId` + `source: "*"` (+ `teamId`)    | Project-wide within the chosen scope           |
| All your tasks             | `token: "*"`                                | Everything your token owns                     |

A project/source subscription only ever receives YOUR OWN dev runs —
another user's dev run of the same pipeline is watchable only via its task
token.

### Seeded on subscribe

You don't poll for the initial state: subscribing seeds it. Turning on `TASK`
triggers an immediate `apaevt_task` with `action: "running"` listing the active
tasks; turning on `SUMMARY` triggers an immediate `apaevt_status_update` with the
current status (or an empty "not running" placeholder).

### Flow traces need a trace level

`apaevt_flow` events fire only when the task was **started** with a
`pipelineTraceLevel`. If you don't control the executor, flow is silent for that
run. When you start the pipeline (`use()` / `execute`), pass:

| Level            | Captured                          |
| ---------------- | --------------------------------- |
| `none` (default) | No flow traces                    |
| `metadata`       | Component / lane structure only   |
| `summary`        | Lane writes and final results     |
| `full`           | Every lane write and invoke call  |

`summary` is the practical default: inputs and outputs without per-call noise.

## Events

The engine pushes [events](/protocols/websocket#events) whose `event` field is the
type discriminator and whose `body` carries the payload. Authoritative type
definitions live in the SDK type modules
(`client-typescript/src/client/types/events.ts`,
`client-python/src/rocketride/types/events.py`, and the matching `task` modules).

| Event                  | Subscribe to | Fires on                                          |
| ---------------------- | ------------ | ------------------------------------------------- |
| `apaevt_task`          | `TASK`       | Lifecycle: `running` / `begin` / `end` / `restart`|
| `apaevt_status_update` | `SUMMARY`    | Periodic full `TASK_STATUS` snapshot              |
| `apaevt_flow`          | `FLOW`       | Component entry / exit, per pipe, per op          |
| `apaevt_venv_trace`    | `FLOW`       | The same, from inside an isolated environment     |
| `output`              | `OUTPUT`     | Engine stdout/stderr-style log lines              |
| `apaevt_sse`           | `SSE`        | Node-emitted custom messages (`monitorSSE()`)     |
| `apaevt_status_upload` | `SUMMARY`    | File-upload progress                              |
| `apaevt_dashboard`     | `DASHBOARD`  | Server-level connection / monitor-change events   |

### `apaevt_task`: lifecycle

`body.action` is one of `running`, `begin`, `end`, or `restart`. The `running`
snapshot lists active tasks with their `id`; `begin` / `end` / `restart` carry
`name`, `projectId`, and `source` but **no per-event id**: correlate them by
`projectId` + `source`, using the `running` snapshot for the id ↔ project+source
map.

```json
{ "action": "running", "tasks": [{ "id": "…", "projectId": "…", "source": "…" }] }
{ "action": "begin", "name": "…", "projectId": "…", "source": "…" }
```

### `apaevt_status_update`: full status

`body` is a `TASK_STATUS` snapshot: the same shape the SDKs return from
`getTaskStatus()`. Key groups:

- **Identity / lifecycle:** `name`, `project_id`, `source`, `state`
  (`0` NONE · `1` STARTING · `2` INITIALIZING · `3` RUNNING · `4` STOPPING ·
  `5` COMPLETED · `6` CANCELLED), `completed`, `startTime`, `endTime`.
- **Activity:** `status`, `currentObject`, `currentSize`.
- **Counts:** `totalCount` / `completedCount` / `failedCount`, the matching
  `*Size` fields, `wordsCount` / `wordsSize`.
- **Rates:** `rateCount`, `rateSize` (instantaneous).
- **History:** `errors`, `warnings`, `notes`, each **capped at the last 50
  entries**, so persist them as they arrive or older ones are lost on long runs.
- **Termination:** `exitCode`, `exitMessage`.
- **Pipeline flow:** `pipeflow.{totalPipes, byPipe}`, where `byPipe` maps each pipe
  id to its currently-active component stack (a live snapshot of what is running).
- **Resource metrics:** `metrics.{cpu_percent, cpu_memory_mb, gpu_memory_mb}` plus
  `peak_*` and `avg_*` variants of each. These cover the whole run, including any
  isolated environments: each runs in its own process, and all of them are sampled
  and billed together. Under a flat pipeline the same work runs in one process and
  is billed there, so isolating a group changes where the work happens, not what it
  costs. One consequence to read correctly: an environment's dependency **install**
  is not sampled, so `peak_cpu_memory_mb` is a peak over the run, not over the life
  of every process it used.
- **Billing tokens:** `tokens.{cpu_utilization, cpu_memory, gpu_memory, total}`
  (100 tokens = $1).

### `apaevt_flow`: execution trace

The data that lets you reconstruct *why* a pipeline produced what it did: each
component's entry and exit with its lane data and any error.

```ts
{
  id: number,                              // pipe index within the pipeline
  op: "begin" | "enter" | "leave" | "end",
  pipes: string[],                         // current component stack for this pipe
  component?: string,                      // component this op refers to (on "leave", the leaving one) — pair enter/leave by identity, not stack position
  trace: { lane?: string, data?: object, result?: string, error?: string },
  result?: PIPELINE_RESULT,                // on op === "end", level >= summary
  project_id: string,
  source: string
}
```

`trace` is free-form and varies by node and trace level, store it as JSON, don't
flatten it.

The lifecycle lanes (`open`, `closing`, `close`) are dispatched from the pipe head in
dependency order — `closing`/`close` upstream-first, `open` downstream-first — so their
`enter`/`leave` frames appear as siblings under the head rather than nested per branch.
`enter`/`leave` pairs still balance; pair them by `component` identity, not by nesting
depth. Data-lane writes emitted while a component flushes still nest inside that
component's `closing` frame.

A control node's inline sub-pipeline (for example `tool_pipe`) is dispatched the same way,
but from that control node rather than the pipe head: the sub-pipeline's lifecycle frames
appear as siblings under the control node, once per invocation. To confirm each node runs
its lifecycle exactly once per pass, count `open`/`closing`/`close` `enter` frames per
`component` within one dispatch pass — each appears once (a control node's sub-pipeline
repeats once per invocation, each invocation being its own pass).

### `apaevt_venv_trace`: traces from inside a virtual environment

A pipeline group marked `isolated` runs in its own engine process. Traces from nodes
inside it arrive under this event rather than `apaevt_flow`, with the same body plus
an `env` tag:

```ts
{
  ...TASK_EVENT_FLOW,                      // identical shape to apaevt_flow
  env: { id: string, name: string }        // the environment that emitted it
}
```

It is a separate event on purpose. `id` is a **pipe index private to the emitting
process**, so a child's ids collide with the main engine's. If you key open-flow state
by `id` — as the reference decoders do — keep one keyspace per `env` and do not merge
these frames into the main pipeline's reconstruction: two processes' `enter`/`leave`
pairs interleaved under one key reconstruct to the wrong tree.

Subscribe via `FLOW`, the same subscription as `apaevt_flow`, and note it obeys the
same trace-level gate. Clients that do not know the event simply ignore it; a run with
no isolated group never emits it.

### `apaevt_sse`: node-to-UI messages

Nodes call `monitorSSE(pipe_id, type, data)` to broadcast custom updates
("thinking", "tool_call", progress, …). The body is `{ pipe_id, type, data }`; the
schema is intentionally open: interpret it per node type.

### `output`: log lines

The engine's DAP `output` events are re-emitted to subscribers. The body carries an
`output` string plus the DAP-standard output fields (`category`, …).

### `apaevt_status_upload`: upload progress

`{ action: "begin" | "write" | "complete" | "error", filepath, bytes_sent?, file_size? }`.

### `apaevt_dashboard`: admin events

Connection lifecycle and monitor-change audit events, useful if you want to record
*who* subscribed to which monitors.

## Related commands

Besides `rrext_monitor`, sent the same way over the socket:

| Command                 | Uses                                  | Returns       | Purpose                                          |
| ----------------------- | ------------------------------------- | ------------- | ------------------------------------------------ |
| `rrext_get_task_status` | `token`                               | `TASK_STATUS` | Fetch current status synchronously               |
| `rrext_get_token`       | `projectId` + `source` (+ `teamId`)   | `{ token }`   | Resolve a running task's token — `teamId` addresses the team's deployed run, omitted = your dev run |
| `execute`               | `{ pipeline, pipelineTraceLevel?, … }`| `{ token }`   | Start a pipeline; sets the trace level that gates `FLOW` |
| `rrext_venv`            | `subcommand` + ids below (+ `teamId`) | varies        | Reclaim per-environment overlays — see below |

### `rrext_venv`

Enumerates and reclaims the per-environment `site-packages` overlays under `<exe>/venvs/`.

| `subcommand`     | Arguments                              | Permission     | Returns                    |
| ---------------- | -------------------------------------- | -------------- | -------------------------- |
| `list`           | `projectId?`, `sizes?`                 | `task.monitor` | `{ environments: [...] }`  |
| `purge`          | `projectId`, `envId`                   | `task.control` | `{ purged: bool }`         |
| `delete_env`     | `projectId`, `envId`                   | `task.control` | `{ deleted: bool }`        |
| `delete_project` | `projectId`                            | `task.control` | `{ deletedEnvironments: n }` |
| `gc`             | `projectId`, `maxAgeDays?`, `dryRun?`  | `task.control` | a report — see below       |

- **`purge`** empties an environment's `site-packages` and keeps `combined.txt` /
  `constraints.txt`. It drops `requirements.hash` first, so the next run reinstalls from a
  full recompile rather than importing from a half-emptied overlay.
- **Ids resolve literal-first.** A name returned by `list` is what is on disk (already
  shortened); a raw project id from a pipeline document also resolves. Both address the same
  directory, so `list → purge` round-trips.
- **`envId` is rejected, not ignored**, by `list` and `delete_project` — it means nothing
  there, and a silently ignored filter would make a one-row answer read as "that is the only
  environment there is". A missing `projectId` is refused for every destructive subcommand
  rather than resolving to the shared `default` bucket.
- **`teamId` is optional.** Present, the permission resolves against that team; absent, against
  the caller's default context. It is a caller-asserted scope check — overlays are machine-local
  disk state and are **not** team-owned.
- **`purge`, `delete_env` and `delete_project` are gated on "no active run for this project"**,
  matched against both the raw and the shortened id form. The gate is per *project*, not per
  environment. Two residuals, accepted for v1: the check-then-act race between the gate and the
  wipe, and — the one that will look like a bug report — a `ttl`-resident engine that still holds
  an overlay's `.pyd`/`.dll` open makes the wipe fail with a named busy error on Windows.
  Completion is not the same as "the process is gone"; stop the engines first.
- **`gc` reads that gate differently: it reports rather than refuses.** A project in use comes
  back as a `skipped` row and the call succeeds, because the meaning of `gc` is "collect what is
  safely collectable". It also uses the wider predicate — *any* registry entry for the project,
  complete or not — since a finished-but-resident engine still holds its overlay open. On Windows
  a busy overlay surfaces as a `failed` row carrying the engine's message; on Linux the same
  situation cannot be detected at all, because unlinking a file another process holds open
  succeeds there. The gate, not the error, is what protects a running engine.
- **`gc` answers with a report, not a boolean:** `{ dryRun, maxAgeSeconds, scanned, collected[],
  skipped[], failed[] }`. `maxAgeSeconds` is the threshold **after** the server's minimum-age
  floor, which is how asking for `maxAgeDays: 0` and getting nothing back explains itself. A
  `failed` row carries `envId` only when the failure was environment-scoped — an unreadable
  project directory has none to name. `projectId` is **required**: the unscoped, whole-machine
  form exists only in the server's own background pass, because a caller-facing version of it
  would let anyone holding `task.control` reclaim every other tenant's overlays without naming
  one.
- **An overlay is collected on age, not on ownership.** The signal is a `last_used` file in the
  environment directory, written every time a run activates that overlay; where it is absent
  (overlays built before this existed) the newest of `requirements.hash` and `install.lock`
  stands in. "Last used" therefore means "last activated" — a long-running resident engine writes
  it once at startup, which is why the registry gate rather than the timestamp is what keeps its
  overlay safe.
- **The server collects on its own, too.** A background pass starts 15 minutes after boot and
  repeats every 6 hours, over every project rather than one. Start the server with
  `--venv-gc-disabled` to switch it off, and set `ROCKETRIDE_VENV_GC_MAX_AGE_DAYS` to override
  the 30-day threshold for both the pass and the `gc` subcommand. The pass logs a line only when
  something happened; visibility depends on the engine's debug level. Note it walks the overlay
  root of *its own* executable, so a server started from outside its `dist` quietly finds nothing.
- **You rarely need the wire.** Both SDKs wrap this command as `client.venv`
  ([TypeScript](/develop/typescript/methods/venv) · [Python](/develop/python/venv)), both CLIs
  expose it as `rocketride venv …`, and the editors put purge and delete on the container
  itself. The wire surface here is for debugging and for writing your own tooling.

## Notes

- **No global run id.** There is no `event_id` or global ordering key. Correlate a
  run by `(project_id, source, startTime)`, and order within a connection by the
  DAP envelope `seq` (per-connection monotonic).
- **No dead-letter queue.** If your consumer is offline it misses that window; the
  next `running` snapshot is the only crash-recovery handle.
- **Tenant scoped.** You only receive events for tasks started with your own API
  token.

## Related

- [WebSocket](/protocols/websocket): the protocol this stream rides on.
- [TypeScript SDK](/develop/typescript) · [Python SDK](/develop/python): clients
  that wrap subscriptions behind `getTaskStatus()`, `onEvent`, and `setEvents()` /
  `add_monitor`.
- Virtual environments: [TypeScript](/develop/typescript/methods/venv) ·
  [Python](/develop/python/venv) — the `client.venv` namespace over `rrext_venv`.
- [Execution model](/concepts/execution-model): how a run streams once started.
