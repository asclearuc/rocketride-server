# Design: Virtual Environments for RocketRide Pipelines

**Status:** Draft (design round — no implementation yet)
**Scope:** Engine (C++ + embedded Python), `depends.py`, the `remote` sub-pipeline mechanism,
the pipeline canvas (shared-ui / VS Code), and the test/CLI harnesses.
**Audience:** Engine + tooling engineers. This is an *internal* design document, not user-facing
documentation — it deliberately lives in `packages/server/design/`, **not** `packages/server/docs/`
(which `docs:gather` publishes to the public site under *Protocols › WebSocket*).

---

## 1. Problem

When a pipeline runs, **all** of its nodes execute inside **one** `engine.exe` process (an embedded
CPython interpreter). Every node module is imported into that single interpreter, sharing **one**
`lib/site-packages` pinned by **one** `cache/constraints.txt`.

That constraints file is built by globbing **all** node and `ai/` requirements
(`REQUIREMENTS_GLOBS = ['requirement*.txt', 'nodes/**/requirement*.txt', 'ai/**/requirement*.txt']`,
`depends.py:58`), concatenating them (`_combine_requirements`), and running **`uv pip compile` over
the union** (`ensure_constraints`). So if two nodes need incompatible versions (e.g. `torch==2.0` vs
`torch==2.1`), the unified compile **fails at engine startup** (`_compile_constraints` →
*"Failed to compile constraints"*), before any pipeline runs.

**Corollary:** today *all shipped nodes must be mutually dependency-compatible*, and a pipeline that
uses nothing audio-related still drags in `whisper`; nothing NER-related still drags in `gliner`.

**Goal:** let a user partition a pipeline into named **virtual environments** — groups of nodes that
run in their own OS process with their own isolated `site-packages` — so conflicting dependencies no
longer collide. Nodes in different environments exchange lane data over secured local IPC.

### Goals
- Allow nodes with mutually-incompatible Python dependencies to coexist in one pipeline.
- Scope dependency installation to **only the nodes a pipeline actually uses** (per environment) —
  faster, smaller, and conflict-surfacing at *compile* time rather than at *runtime*.
- Reuse existing engine machinery (the `remote` sub-pipeline, `depends.py`, the canvas group node)
  rather than inventing parallel stacks; **no/minimal C++ engine changes**.

### Non-goals
- **Not a security sandbox.** Virtual environments isolate *dependencies*, not untrusted code — a
  node in a venv can still touch the filesystem and network. This is not tenant isolation.
- Not a general distributed-execution feature. Inter-venv transport is local (loopback) in v1.

---

## 2. Background (verified)

### 2.1 Canvas groups are inert in the engine
The canvas (`packages/shared-ui/src/components/canvas`, ReactFlow/xyflow) already has a **group node**
(`INodeType.Group`). Nodes dropped into a group get `parentId`; on save,
`graph.ts:getProjectComponents()` **nests the group's children into `config.pipeline.components`**.

But a group has **no engine-side meaning today**: there is no `group` provider, nothing flattens or
recurses into `config.pipeline.components`, and `stack.cpp` (`generatePipelineStack`/`buildConnections`)
iterates only top-level `components[]`. So a group is purely a UI container; the nested blob in the
saved document is inert. **The new partitioner is where group structure gets runtime meaning.**

### 2.2 The `remote` sub-pipeline mechanism (the reuse foundation)
The repo has a **live** remote-sub-pipeline feature under `nodes/src/nodes/remote/`:
`remote` / `remote_server` nodes + `prepare_pipeline.py` + a WebSocket lane bridge + HTTP endpoints in
`packages/ai/src/ai/modules/remote/`. It already:

- takes a nested `config.pipeline` (the **same shape** as canvas groups);
- **rewrites the graph** two ways — `prepareLocalPipeline` *inlines* a sub-pipeline (= "flatten");
  `prepareRemotePipeline` *inserts bridge/stub nodes + reroutes lanes* (= cut a boundary);
- bridges lane data over a **token-authed WebSocket** (Bearer token, `~1 MB` chunking);
- gates members by a **`REMOTING`** capability (on by default; cleared by `noremote`, `services.cpp:1737`).

Two facts that shape the design (both **verified** against the code):

- **Lane coverage is 3, not 5.** Server-side `callLocal` (`remote/base/IInstance.py`) handles only
  `writeTag`/`writeText`/`writeDocuments` (+ `open`/`closing`/`close`) → **text/tags/documents**;
  everything else `raise TypeError`. `image`/`video`/`audio`/questions/answers/classifications are
  missing. (The client also *sends* `words` but the server has **no `writeWords` handler** — a latent
  network-remote bug to fix separately.)
- **The transport is not cleanly separable.** WebSocket `_send`/`_recv`/`connect`/`disconnect` are
  embedded **directly** in `remote/base/IInstance.py`, not behind a transport seam.
- `remote_source_stub` is **not** a real node — it is *synthesized at runtime* by `prepare_pipeline.py`.
- `REMOTING`/`noremote` is a **network**-remoting gate; the `noremote` set is local-resource nodes
  (local filesystem `core` source, DB nodes, `text_output`). Venvs are *same-host*, so this gate may
  be too restrictive — see §7.

### 2.3 Embedded Python & dependency machinery
- `init.cpp` initializes CPython with an **isolated `PyConfig`** (`Py_InitializeFromConfig`), home set
  to the executable dir. **`PYTHONPATH` is ignored** — `sys.path` is mutated at runtime instead
  (`init.cpp:setPaths`, and `depends.py:_ensure_site_packages` does `sys.path.append(...)`).
- Linux/macOS statically link CPython into `engine.exe`; Windows ships `python3XX.dll` + the MSVC
  `vcruntime` next to it, loaded at C++ init **before** `sys.path` is touched.
- `depends.py` resolves all paths relative to `dirname(sys.executable)`: `engine_cache_dir()` =
  `<exe>/cache`, `model_cache_dir(name)` = `<exe>/cache/models/<name>`, base site-packages =
  `<exe>/lib/site-packages`. It already has a `FileLock`/`install.lock` + progress sidecar for
  concurrent installs.
- The dev-mode debug shim copies `engine.exe → python.exe` **in the same directory** (so
  `dirname(sys.executable)` is unchanged). `engtest` (the engine-lib Catch2 binary) asserts
  `sys.prefix == sys.executable dir == rootDir` — an invariant this design preserves.

### 2.4 `project_id` and multi-source pipelines
- A pipeline's stable id lives at **`config.pipeline.project_id`** (a GUID; the top-level
  `project_id` is null). `task_server.py` reuses it and only generates one if absent; it survives
  edit/save/rename. Unsaved ad-hoc editor runs may get a fresh UUID each run.
- A pipeline may have **multiple source nodes** (e.g. `dropper_1`, `dropper_2`) = multiple execution
  lanes. The engine runs **one source per task** (separate `task-*.json` + `taskId` per source), but
  **every per-source task file carries the FULL `components[]` and the same `project_id`** (verified
  on `examples/task-0d4f3caa.dropper_1…json` / `task-575adb74.dropper_2…json`).

---

## 3. Design overview

**A virtual environment = a "local remote".** Each isolated group becomes a flat sub-pipeline run by
a child `engine.exe` process (TARGET mode, as the engine runs any pipeline today), with its own
isolated `site-packages` overlay. Boundary edges are bridged over a token-authed WebSocket on
loopback; inter-venv traffic is routed by **main's engine graph** — an environment feeding another
becomes a plain edge between their two bridge nodes (§4.6, step 8.3), so every socket stays
main↔child and no frame is ever relayed by the orchestrator. The C++ engine is unchanged.

Three pillars:

1. **Per-environment requirement scoping** (the actual conflict fix). Stop globbing all node/`ai`
   requirements into one resolution; instead each environment compiles + installs **only the nodes it
   uses**, into its own overlay. This even helps no-venv pipelines (scoped installs). *Foundation —
   shippable on its own.*
2. **The venv runtime.** A new first-class canvas **Virtual Environment** container; a Python
   **partitioner** that turns isolated groups into venv sub-pipelines + bridge nodes; a **`venv`
   bridge node** (sharing a base with `remote`) carrying all lanes; **local spawn + routing through
   main's graph**; orchestration (lifecycle, merge-back, observability).
3. **Polish & scale.** Cross-cut debug/observability, pre-warm, and v2 optimizations (direct
   venv↔venv mesh, shared-memory for large buffers, the local-IPC transport seam).

---

## 4. Detailed design

### 4.1 The Virtual Environment container (UI)
The **only new visible/placeable** canvas element is a first-class **Virtual Environment** container.
It reuses group *mechanics* (`parentId` nesting, children → `config.pipeline.components`,
`onNodeDragStop` drop-into) but is distinct from a plain organizational group: it carries
`config.environment = { name, isolated: true }` and an "isolated" visual treatment (badge/border).

**Split representation (IMPLEMENTED).** It is a distinct node type **on the canvas**
(`INodeType.VirtualEnv`) and a `group` **in the document** — `getComponentFromNode` writes
`ui.nodeType: 'group'`, and the loader promotes a `group` carrying `config.environment` back to the
container. That buys both halves: the container gets its own component, its own
`.react-flow__node-virtualenv` treatment and type-level validation, while the document stays exactly
the shape in §4.2, so an editor that predates the container still renders it — and, crucially, still
**nests its members on save** instead of scattering them to the top level. Containment is asked via
`isContainerType()` rather than compared against a single type, because it is checked in three
separate places (drop-into, serialization, auto-layout) and two-out-of-three is the classic failure.

**Naming.** "Environment" is already taken in the extension by the server-connection page
(`EnvironmentProvider`/`EnvironmentView`: development/deployment slots, SaaS vs OSS). The container
keeps the full words **Virtual Environment** in the UI and a `VirtualEnv` prefix in code so the two
do not read as one concept.

**No creation entry yet (deliberate).** The container's members are nested into
`config.pipeline.components`, which the engine ignores until the partitioner lands (§4.3, §5.3). A
creation button before that would let a user build a pipeline whose members silently vanish at run
time, so the mechanics ship first and the entry point opens with the partitioner. Placing one today
is possible only by authoring the document directly — which is what the acceptance fixtures do.

A **cog** will expose a **Purge environment** action (§4.10) once the engine command exists; a menu
item that cannot do anything is worse than its absence.

The **bridge/`remote`/`venv` nodes stay internal** — synthesized/inserted by the partitioner, **never
in the node palette**, never user-placed. (Like the `remote` nodes today, which are not canvas-exposed.)

VS Code host wiring (`apps/vscode/.../ProjectWebview.tsx`) follows the extension rules
(`Callout.call`, `AppError`, `logger.*`). The schema field is added to
`packages/client-typescript/src/client/types/pipeline.ts`.

### 4.2 Pipeline document format — two formats

**Authoring format** (what the canvas saves into `.pipe`): a venv is the existing group node with the
**only additive change** `config.environment = { name, isolated: true }`. Member nodes keep their
`input`/`control`; a member's `input.from` may reference a node *outside* the group (lane edges cross
groups today) — those are the boundary edges. The canvas renders it as its own node type but writes
this shape (§4.1), so the document is additive and readable both ways. The two structural keys are
now named in the SDK schema — `PipelineEnvironment` and `NestedPipeline` on
`PipelineComponentConfig` (`packages/client-typescript/src/client/types/pipeline.ts`) — so a
container's shape is no longer a matter of convention.

**Runtime format** (what each `engine.exe` receives): the engine reads only **top-level**
`components[]` and ignores `config.pipeline`. The **partitioner** (Python, pre-launch, modeled on
`prepare_pipeline.py`) expands the authoring doc into N **flat** sub-documents — `main` + one per venv
— inserting bridge nodes at each boundary edge and rewriting the downstream `input.from`.

```jsonc
// authoring: a "vision" venv with one member (detect), fed by parse(main), feeding response(main)
{ "id": "venv_vision", "config": {
    "environment": { "name": "vision", "isolated": true },
    "pipeline": { "components": [
      { "id": "detect_1", "provider": "detect", "ui": { "parentId": "venv_vision" },
        "input": [ { "lane": "image", "from": "parse_1" } ] }   // crosses INTO the venv
    ] } },
  "ui": { "nodeType": "group" } }
```

### 4.3 Partitioner
A pure Python transform (`pipeline.partition_pipeline`, sibling of `resolve_pipeline_env`) called from
the task-start path in `task_engine.py`. Modeled on / generalizing `prepare_pipeline.py`:

- **Non-isolated** groups → **flattened** (children lifted to top level, like `prepareLocalPipeline`).
- **Isolated** groups → a separate flat sub-pipeline per venv; each boundary **data-lane** edge gets a
  bridge-node pair + a `channelId` recorded in a routing table.
- Builds the **env-quotient graph** to detect cross-env cycles.
- Reads the **full `components[]`**, not the executing source's reachable subgraph (see §4.13).

**Placement — corrected against the code: BEFORE `_check_pipeline`, not after.** That check looks for
the run's source among **top-level** components only, so a source inside a plain group is not found
until the members have been lifted. Partitioning first also means every later step sees the document
the engine will actually run.

**Increment 1 — flattening + validation (IMPLEMENTED).** Container members are lifted to the top level
and the container itself disappears; nesting collapses to one level and an empty container simply goes
away. Membership carries no runtime meaning by itself — members keep their ids and connections, so an
edge that crossed the boundary needs no rewriting. A pipeline with no containers is returned unchanged,
by identity.

This alone fixes the standing bug in §5.3: until now **every** grouped component was silently dropped
from the run, and the pipeline still reported success. Verified on a document the editor actually
wrote: `[dropper, parse, venv_vision, venv_audio, response_outside]` →
`[dropper, parse, response_1, response_outside]`.

**Isolated containers flatten the same way on the legacy path.** Under `scoped=False` (the permanent
compatibility mode `ROCKETRIDE_SERVER_USE_VENV=0`, §4.15, and today's default call site) an isolated
container behaves as an organizational one — flattened into one process. The cut below only runs on the
`scoped=True` path, which the orchestrator flips in step 8.

**Increment 2 — the cut (IMPLEMENTED).** `partition_pipeline(pipeline, source=None, scoped=True)` returns
a `PartitionResult` (`pipeline.py`): `environments` (an `OrderedDict` of one flat sub-document per env,
`'main'` first then each isolated group in document order), `routing` (one entry per boundary channel),
and `groups` (each venv's `config.environment` block, for step-7/8 logging). The transform:

- **Leaf-bucketing by transitive env.** `_env_of` maps each component to its **nearest isolated-container
  ancestor** (else `'main'`); every leaf is bucketed by that env and every container dissolves. This one
  rule subsumes non-isolated flattening (no separate phase) and handles every nesting — a plain group
  inside a venv, or a venv nested inside a plain group (which becomes a top-level env), fall out of it.
  The transitive map also fixes two latent increment-1 bugs for a member of a plain group nested in a
  venv (a cross-boundary invoke into it was missed; an intra-venv invoke into it was wrongly rejected).
- **Boundary channels, one forward and one return per environment** (step 8.1 Arch-1; re-keyed in
  step 8.3). Channels are keyed `(direction, env)`, not by environment pair: everything entering a
  venv shares its forward channel and everything leaving it shares its return channel, whatever the
  other end is. A venv→venv edge is therefore cut **twice**, once at each boundary it crosses, and
  reaches its consumer as an ordinary main-graph edge between the two bridge nodes (§4.6).
  `sourceEnv`/`targetEnv` keep naming the **socket peers** (main and the child), never the data's
  true origin — the child selects a `venv_server` node's role from `sourceEnv == 'main'`, and the
  spawn injection picks the child env the same way. `channelId` (`main->{env}` / `{env}->main`) and
  the sanitized `venv_egress--…` / `venv_ingress--…` node ids are unchanged. A child runs one
  pipe stack, so a whole boundary is **one** connection: a single bridge node carries every lane of
  that boundary over one socket, and the frame's `lane` header demuxes to the consumers. Each lane in
  a channel has exactly **one** producer — a node's `write*` carries no producer identity, so two
  same-lane producers on one boundary cannot be told apart downstream and are rejected with a named
  cause; multiple consumers of a lane (in-venv fan-out) are fine. `channelId = '{srcEnv}->{dstEnv}'`
  (the routing key; collision is only possible if an env id itself contains `->`, which is rejected),
  plus role-based, sanitized `venv_egress--…` / `venv_ingress--…` node ids (unique **per document**).
- **Bridge placement — the round-trip splice (`remote` model; see step 7 for why).** A venv boundary is a
  request/response splice of one object, so its forward and return channels are **paired** per env and the
  return rides back over the forward socket. `_pair_boundaries` pairs each env's forward channel with its
  optional return channel — by construction, since the key is `(direction, env)`. Forward: **one**
  round-trip `venv` node in main reads each lane's **main-side source** — the producer itself when it
  lives in main, otherwise the bridge node of the venv that produced it — and a `venv_server` ingress
  (from the child stub) applies the forward stream in the child; the child's forward consumers repoint to
  that ingress. Return: a `venv_server` egress (from the venv producers) ships the return over the **same**
  socket, and main's return consumers repoint to the paired round-trip node (its `deliverNode`) — there is
  **no** separate main ingress node, so the spliced object is never re-opened. Each child sub-document is
  seeded with a synthesized `venv_source_stub` resident source that the ingress links to for reachability;
  unlike `remote`, child docs are NOT nested under the client config — they are separate `environments[]`
  entries the orchestrator (step 7) spawns from. Multi-lane fan-in/out, `venv→venv` chains and diamonds
  are all supported. What remains rejected, with named causes, is the same lane from two producers on one
  boundary (the boundary is now the whole environment, so this bites more often than it did with
  pair-keyed channels; the workaround is a merge/router node inside the venv), an environment entered
  more than once around a base component, and quotient cycles.
- **Env-cycle detection over the quotient graph, main excluded.** Edges are `env_of(producer) →
  env_of(consumer)`, so venv→venv is a direct edge and routing through main is invisible; dropping main
  catches venv↔venv deadlocks without flagging main-terminated chains, which are legal and are exactly
  what step 8.3 enables. The adjacency is accumulated by `_collect_channels` rather than read off the
  channels: under `(direction, env)` keying every channel has main on one side, so the venv→venv relation
  lives inside a channel's lanes, not in its key. **A second, narrower cycle check** runs on main's
  assembled document: a venv collapses to exactly one bridge node, so entering it twice around a base
  component folds into `MV → m → MV`. That is a DAG as authored and runs fine flattened under `=0`, and
  the author never wrote the node names in the cycle, so it is named at cut time rather than left to the
  engine's own (post-#1669) Kahn check, which reports a lifecycle root index at pipeline open. It fires
  only for cycles passing through a bridge node, so the partitioner never becomes stricter than the engine
  on shapes unrelated to venvs.

  *Ordering trap when writing fixtures:* the same-lane conflict check sits **upstream** of both cycle
  checks. A cycle needs an environment entered twice, and if both entries carry the same lane the
  conflict fires first — so a cycle fixture must use distinct lanes per direction.
- **Extra scoped rejections:** a boundary edge on the non-bridgeable `words` lane; an implied
  (`Source`-mode) source or the document `source` field inside a venv; a base environment left with no
  components while a venv exists; a group whose id is literally `main`; an environment that emits across
  its boundary but is fed by nothing (nothing dials it, so its egress would have no socket). The
  `scoped=False` path and the 19 increment-1 tests are unchanged; the cut adds `test_partition_cut.py`
  (39 tests after step 8.3). Deferred: §4.13's
  "all nodes in ONE venv → collapse" (a runnable all-in-one-venv doc cannot exist while source-in-venv is
  rejected, and honoring it only when scoped would make `=1` accept what `=0` rejects). The bridge nodes'
  live child URL/token are written at spawn (step 7).

**Validations, enforced now** (structural errors the editor should have prevented, failed with a named
cause rather than silently normalised): a virtual environment nested inside another; the source inside
a virtual environment (a plain group is fine — a group is layout, not an execution boundary); an
invoke/control edge crossing an environment boundary, in either direction and between two
environments; and a lane edge that takes input from a container, which produces no data. Increment 1
also rejects a control edge whose source is a container (it dangles once the container flattens away).
Env-cycle detection is now implemented on the `scoped=True` cut (see Increment 2 above).

Covered cases: lane fan-out across envs, multiple lanes on one boundary, A→B→main chains and
diamonds, a venv feeding two venvs, source/sink
placement (§4.11). **Invoke/control edges never cross a boundary** (the editor's `isValidConnection`
requires equal `parentId` for invoke handles; data lanes cross freely) — so cross-env tool-call RPC is
out of v1 *by construction*. **This is editor-only — C++ does not check `parentId` on invoke edges
(verified)** — so **the partitioner must enforce it as a hard validation** (reject cross-boundary
invoke/control edges), not assume the document is well-formed.

### 4.4 Bridge nodes — shared base + a new `venv` node
The transport is *not* cleanly separable (§2.2), so rather than editing `remote` in place or rebuilding
a parallel stack:

- **Extract the common bridge base** (lane dispatch/serialization in `callLocal` + the transform) into
  shared code.
- Add a **new `venv` / `venv_server`** node pair inheriting it, alongside `remote` / `remote_server`.
- The `venv` node implements **all data lanes** — the 15 `Binder::MethodNames` minus the
  `open`/`closing`/`close` framing: `tags, text, table, words, audio, video, questions, answers,
  image, classifications, classificationContext, documents` (today's `callLocal` covers only 3 data
  lanes — text/tags/documents), critically `image`/`video`/`audio` (vision is image-heavy). Derive lane
  handlers from **one table** so a new engine lane forces a wire-format bump, never a silent gap. Reuse
  the serialization vocabulary in `data_conn.py` (`_determine_lane`, `_begin`/`_write`/`_end`).

This **reuses the fiddly logic** (shared base) while **decoupling** venv work from the live `remote`
feature — no regression risk to network-remote, and its `words` gap stays its own problem. It is *not*
the rejected `venvEgress`/`venvIngress` reinvention; it is a sibling of `remote` over one base.

**Step-6 decisions (verified against the code, resolving the §4.4 forks).**

- **Lane count is 13, not 12 — the list above dropped `json`.** `binder.hpp::MethodNames` (16 total)
  is `open, tags, text, table, words, json, audio, video, questions, answers, image, classifications,
  classificationContext, documents, closing, close`. Minus the three framing lanes that leaves **13**
  data lanes, and `json` is a real one (there is an `instance.writeJson(IJson)` and a `data_conn._write`
  handler). Omitting it is exactly the silent gap this section warns against, so the `venv` table
  **includes `json`**.
- **`words` is not bridgeable and is recorded as such, not silently skipped.** There is **no
  `writeWords` on the `rocketlib` instance/pipe surface at all** (verified in `filters.py`/`__init__.pyi`);
  `remote/client` *sends* it but nothing can land it — that is the latent `remote` bug. The `venv` table
  therefore carries `words` as an **explicit "not bridgeable" entry that raises a clear error**, so the
  gap is loud (a bump), not a quiet drop. The one-table/`binder.hpp` cross-check test asserts coverage of
  every `MethodNames` entry, `words` included as the explicit-unsupported case.
- **Seam = `venv`-only base; `remote` is left byte-for-byte untouched (chose A2 over A1).** The shared
  bridge base (WS `_send`/`_recv`/`connect`/`disconnect` + the generic `callRemote` loop + `listChunks` +
  the table-driven `callLocal`) is introduced **under `venv` only**; `remote/base` is not moved onto it.
  This costs a little transport duplication but removes all regression risk to network-remote. The true
  DRY refactor — moving `remote/base` onto the shared base with its current 3-lane `callLocal` preserved
  byte-identically (**A1**) — is **deferred to 2C**, alongside the transport seam that §4.5 already
  defers there.
- **AV multi-arg framing = metadata in the header, body is raw bytes (B1).** `image`/`audio`/`video`
  carry `(action:int, mime:str, buffer:bytes)`, which the single-scalar `_send` cannot express. The
  `venv` transport puts `action`/`mime` on the already-sent JSON header and ships the buffer as **raw
  bytes** (no base64), so multi-MB frames keep their throughput — base64-in-JSON (**B2**) was rejected
  for the ~33% inflation §4.5 warns about. The bridge does **not** synthesize `_begin`/`_end` framing for
  AV: the engine already calls the egress node's `writeImage(action, …)` with the action embedded, so
  egress forwards it verbatim and ingress replays it — the reused `data_conn` vocabulary is the
  **type serialization** (Question/Answer/Doc/classifications/`IJson`), not the framing.
- **Bridge nodes are `internal`, not merely `nosaas` (verified — the two are different bits).**
  `INTERNAL` (`PROTOCOL_CAPS` BIT 6, `Url.hpp`) means *not returned in `services.json` at all* — the
  UI never sees the node, so it can't be shown, placed, or referenced. `nosaas` (BIT 13) is weaker: the
  node **is** in `services.json` but the UI Add-Node inventory/quick-add filter it out
  (`shared-ui/.../helpers.tsx`, `QuickAddPopup.tsx`) — and nothing in the C++ engine or the Python
  server gates *execution* on `nosaas` (it is only parsed into `def.capabilities` at
  `services.cpp:1779`; there is no engine "saas mode"). That is why `remote_server` carries
  `["internal", "nosaas"]` while the user-placeable `remote` client carries only `["nosaas"]`. Both
  `venv` and `venv_server` are **synthesized by the partitioner and never user-placed**, so **both take
  `internal`** (mirroring `remote_server`, not the `remote` client); `nosaas` on top is redundant but
  harmless for symmetry.
- **`data_conn.py` reuse = mirror the vocabulary in the `venv` table, do not import (D2).** `data_conn`
  covers ~11 of the 13 lanes (it lacks `table`, `classificationContext`, and — like everything — `words`),
  and its `_write`/`_begin`/`_end` are methods bound to a `DataConn`/`pipe`, not standalone. For step 6 the
  `venv` table mirrors the serialization vocabulary with `data_conn` as the reference, adding `table`/
  `classificationContext` itself. Extracting a shared `write_lane` dispatch in `packages/ai` that both
  `data_conn` and `venv_server` call (**D1**) is the real-DRY move and is **deferred to 2C together with
  the A1 base unification** — both are behavior-preserving refactors best done under the green test
  baseline rather than mixed into the feature.
- **The 12 data `write*` egress overrides live in the shared base, inherited by BOTH nodes (verified).**
  `remote/server/IInstance` *also* overrides `writeText`/`writeDocuments` → `callRemote(...)`: that is the
  **return path** (data produced inside the venv flows back through the server node to main). So the egress
  surface is symmetric — the client sends the forward stream, the server the return stream, the same 12
  methods over the same table — and both are placed **once in the base**, not duplicated per node as
  `remote` does. Client and server subclasses then carry only their lifecycle: the client adds `connect`
  plus the framing overrides, the server adds the `handleWebSocket` accept-loop.
- **A bridge never calls `pipe.closing()`; the framing pair on the wire is `open` + `close`.** The
  engine's `pipe.close()` runs the closing pass and *then* the close pass
  (`pipe.instance.cpp`: `Parent::closing()` followed by `Parent::close()`) — which is exactly how the
  client drives a pipe (`data_conn.close_sync` calls `pipe.close()` and never `pipe.closing()`).
  Forwarding a `closing` frame as well makes every node inside the child flush **twice**; the child
  side therefore refuses that lane with a named cause rather than honouring it silently.
  **Measured, not predicted:** restoring the two-frame shape on a post-#1667 engine does not merely
  duplicate the output — the boundary **deadlocks** and the run hangs. The second closing pass emits
  into a nested round-trip that main is no longer reading, which breaks the strictly-nested
  synchronous invariant the whole transport rests on. A/B on the same build: collapsed →
  `text: ["olleh\n\n"]`; two-frame → timeout.
  *This was latent, not theoretical.* Python `instance.closing` used to be bound to `cb_close`, so
  `pipe.closing()` performed both passes and the follow-up `pipe.close()` was inert — the bridge was
  accidentally correct. Engine **#1667** rebound it to `cb_closing`, which is right in itself and
  turns the second frame into a real double flush.
  **The one `close` frame is sent from the main node's `closing()`, not its `close()`**, so the venv's
  return data reaches main's downstream consumers before *their* `closing()`: framing is bound per
  edge (`endpoint.pipes.cpp`) and `IPythonInstanceBase::closing()` runs a node's own Python `closing()`
  before `Parent::closing()` hands off to its consumers, so a producer always closes ahead of
  everything it feeds. Main's `close()` is consequently inert; the engine still calls `Parent::close()`
  after it, so main's own framing propagates unchanged.
  **Dead-socket guard.** A child that dies mid-*data* tears the socket down before main's closing pass
  runs, so that lone frame would raise into a pass the engine aborts at the first error, costing the
  downstream nodes their flush — even though the child's error already crossed and failed the object.
  `closing()` therefore swallows a connection-closed send **only when the object already failed**, and
  re-raises on a clean object (a dead socket with nothing reported is a child crash that must not
  complete as a success).
- **`callLocal` is bidirectional.** `venv_server` uses it for the forward path (main→venv); the egress
  client's `callRemote` receive-loop uses it for return lanes (venv→main). That is precisely why the
  full-lane table belongs in the shared base rather than only in `venv_server`.
- **AV wire framing (concrete B1).** `image`/`audio`/`video` put `action`/`mime` on the JSON header and
  ship the buffer as **raw bytes** (no base64). The buffer is **optional** — the stream is
  `write*(BEGIN, mime)` → `write*(WRITE, mime, buffer)` → `write*(END, mime)`, and `data_conn` calls the
  2-arg form for BEGIN/END — so a bufferless frame crosses as a `none` payload and the far side replays
  the 2-arg call. The bridge is a transparent pass-through of already-framed calls; it does **not**
  synthesize `_begin`/`_end`. Raising the ~1 MB WS ceiling for large AV buffers stays a step-7 item.
- **Bridge nodes get no per-node pipeline transform.** C++ invokes the remote transform by a **hard-coded**
  `py::module::import("nodes.remote.client")` + `.attr("preparePipeline")` (`pipeline_config.cpp`), so it
  is remote-specific — nothing would call a `venv`-side `preparePipeline` even if one existed. venv graph
  rewriting is the partitioner's job (§4.3 increment 2); `venv/client/__init__.py` re-exports only
  `IGlobal`/`IInstance`.
- **`services.*.json` follow the minimal `remote_server` template** (no `preconfig`/`shape`/`fields`):
  both bridge nodes are `internal` + synthesized, so there is no editor config form — the partitioner
  writes their `config` (child URL/token, step 7) directly. And `internal` (BIT 6) hides a node from the
  **client catalog** but does **not** un-register the provider — the engine keeps it in its ProviderIndex,
  so a synthesized `provider: venv`/`venv_server` still instantiates, exactly as `remote_server` does.
- **Bridge (de)serialization is per-type, not uniform (reuses the `data_conn` vocabulary, D2).** All
  VERIFIED against the shipped `rocketlib`: `Doc` → `toDict()`/`fromDict()`; `Question`/`Answer` are
  pydantic → **`model_dump(mode='json')`** (plain `model_dump()` leaves enums like `QuestionType` on the
  wire, which are not JSON-serializable) / `model_validate()`; `IJson` egress is **`json.loads(str(ijson))`**
  (the `writeJson` arg is an `IJson` *instance*, and `IJson.toDict` is a staticmethod that only accepts a
  plain dict, not an instance) and ingress is `IJson(dict)`; `TAG` egress is `tag.asBytes` and ingress is
  `instance.writeTag(bytes)` — the bridge never reconstructs a `TAG` (there is no runtime `TAG.fromBytes`,
  despite the `.pyi` stub).

### 4.5 IPC transport & security
**v1 reuses the existing WebSocket lane bridge bound to loopback, unchanged** — it already carries a
Bearer token. Bind to `127.0.0.1`; reject unauthenticated connections; deliver the token via inherited
env/handle, never argv.

- **Known limit (v1 acceptance gate, not a v2 "confirm"):** WS frames chunk at `~1 MB`
  (`remote/base/IInstance.py`); large image/video relies on chunking. **Measure a representative
  image/video crossing against a target throughput ceiling as a v1 acceptance criterion**; raise the
  chunk ceiling for AV if it misses.
- **The merge-back `entry` frame shares that ceiling, and is *not* chunked.** `callRemote` chunks only
  top-level *lists*, so the frame's dict crosses whole: a response above `~1 MB` — realistically a
  base64 media blob written by a `response` node **inside** a venv (§4.12) — fails. Same cause as the
  un-chunked AV buffers above, and accepted on the same terms; lifting one should lift the other.
- **Cloud-store direction shrinks the *payload*, not the *lane set*.** In the planned model AV bytes
  live in **cloud storage** (`ai..account.store`); the bulk bytes are fetched from the store, not
  streamed node-to-node. **But AV metadata still travels on the `writeVideo`/`writeAudio`/`writeImage`
  lanes** — so the bridge must **still implement every AV lane** (§4.4 "all data lanes" is *not*
  reducible), and those lanes still cross the venv boundary. What changes is the **payload size**: a
  small metadata/URL frame instead of multi-MB buffers, which is what drops the throughput risk (the
  ~1 MB chunking is a non-issue for small frames). The interim caveat still holds — before the store
  migration, raw AV crosses these lanes and the throughput gate above applies. (The child's store fetch
  needs account/store context — ties into secrets/`ROCKETRIDE_CLIENT_ID` propagation, §6.)
- **Hardening (deferred to 2C):** OS-access-controlled local IPC so the kernel rejects other-user
  processes *before* any token check — **named pipe + user-SID ACL** (Windows), **Unix domain socket**
  `0700` + `SO_PEERCRED`/`LOCAL_PEERCRED` (Linux/macOS). Matters for **multi-tenant** hosts (Linux
  cloud especially); single-tenant is fine on loopback + token (same-user child). This is **not
  Windows-specific** — but it requires the same `IInstance.py` transport refactor that makes the
  transport not-separable, so it is deferred, not a v1 item.

### 4.6 Inter-venv routing — graph serialization through main
All venv children connect **only to main**, and each has exactly one socket. An environment that
feeds another is **not** relayed frame-by-frame: the partitioner rewrites the venv quotient into
ordinary edges of main's engine graph, between the bridge nodes that already represent each child
there (step 8.3). A venv's outputs come back into main through its own bridge node; handing them to
the next venv is then just that bridge node being the next one's input.

```
dropper(main) → parse(venv1) → detect(venv2) → return_image(main)
main graph:  dropper ──▶ [venv1 bridge] ──▶ [venv2 bridge] ──▶ return_image
sockets:     bridge ↔ child venv1, bridge ↔ child venv2   (no venv1↔venv2 link)
```

Why it is correct in one breath: each child has exactly **one** connection, so a double-open is
impossible by construction; `open`/`closing`/`close` ordering is delegated to **main's engine**; and
main's engine thread is only ever inside one bridge node's call, so a return always arrives on the
socket being read. The two calls simply nest — one logical thread, strictly nested round-trips.

**Main still needs no codecs or heavy deps for the data crossing it.** A hop does decode into main's
engine objects and re-encode, but `nodes/venv/base/lanes.py` is dependency-free: AV crosses as an
`action`/`mime` header plus a raw byte buffer, so a venv-`torch` image passes through main without
main having `torch`. The §4.6 promise holds; what changed is *who* forwards, not what main must
understand.

Two properties fall out of the serialization and are not obvious from the rewrite rule: a chain of N
venvs is N **nested blocking** round-trips on main's single engine thread (latency composes, and an
inner child's stall blocks every outer bridge), and a venv is entered **once per run** — re-entering
one around a base-environment component collapses to a cycle on its single bridge node and is
rejected at cut time (§4.3).

Rationale for one-socket-per-child over a mesh: N connections (not N²), central
lifecycle/token/routing/observability, each child authenticates with **only main**. Cost: a venv→venv
edge is still 2 transfers. **v2 optimization:** direct venv↔venv peering for hot large-buffer edges
(+ shared-memory for AV).

*Superseded:* this section previously specified an orchestrator-level **byte router** — main's Python
process reading a frame off one child's socket and forwarding it to another's by `channelId`. Graph
serialization replaced it at step 8.3: no new transport, no new endpoint, no frame tags, and diamonds
(a venv fed from two environments) fall out as an ordinary multi-input join on the bridge node, which
the byte router never solved.

### 4.7 Per-environment requirement scoping (the conflict fix)
Today `_find_requirement_files()` globs **all** `nodes/**` + `ai/**` requirements (pipeline-blind);
the unified `uv pip compile` fails the moment two nodes conflict. Instead:

**Uniform per-environment scoping (main included; one code path).** Each environment compiles +
installs **only the nodes it uses**, into its own node-set-keyed overlay. The partitioner already knows
each env's node set, so:

- map the env's components → `provider` → `nodes/src/nodes/<provider>/requirements*.txt`;
- **AST-discover** the `ai/**` submodules each node imports (§4.8) → include only those
  `requirements*.txt`;
- `ensure_constraints()` takes an **explicit requirement-file set + env dir** (not the global glob).

**Node set = the WHOLE document (all sources/lanes), not the executing source's subgraph** — because
the on-disk env is keyed by `project_id` and shared across all per-source runs. Compiling per-lane would
each succeed but **conflict at runtime** (dropper_1 lane `torch 2.0` + dropper_2 lane `torch 2.1`);
full-set compilation surfaces it at **compile time**, and both lanes reuse the **same** venv.

**Consequence / blast radius (must gate).** The base `lib/site-packages` becomes **engine-runtime-only**;
**all** node deps move into per-environment overlays — even main's, **even for pipelines with no venvs**.
When enabled, this changes dependency resolution for the *whole* pipeline (main included), so it is
gated by the **`ROCKETRIDE_SERVER_USE_VENV` master switch (§4.15)** with a **permanent** fallback to
today's global-glob path (`=0`). The default (auto) only scopes per-env when the pipeline opts in via an
isolated group — a no-venv pipeline under the default keeps today's global-glob behavior unchanged; the
legacy path is a supported mode, not a transitional flag.

**Upside:** eliminates "all shipped nodes must be compatible" entirely (conflicts only *within* an env →
resolved by the venv split); shrinks the overlay-can't-hide-a-base-package limit to only packages the
**engine runtime itself** needs.

### 4.8 AST discovery of `ai/**` requirements
Run **once per pipeline init**, cached by node-set hash.

**Input contract.** The partitioner (the only component that reads the `.pipe`) resolves each env's
components → `provider` → the node's **entry-module path** (resolution rule below) and hands
`depends.py` an explicit **per-env node-set descriptor**: a list of
`(provider, entry_module_path)` + the node-set hash + the env dir. **`depends.py` never parses the
`.pipe`** — a single AST-discovery entry point there walks each entry-module path, follows imports
into `ai.*` to find the `ai/common/models/<x>` submodules reached, and returns those submodules'
`requirements*.txt` to fold into the env's requirement-file set. So a pipeline with no audio never
pulls in `whisper`. This reconciles §4.7 (partitioner resolves the node set) with §9 (the AST walk
lives in `depends.py`): **the partitioner passes paths; `depends.py` parses them.**

**The single door for torch (VERIFIED) — this is what decides whether a node may pin its own.**
`torch==2.10.0+cu128` lives in exactly one file, `ai/common/torch/requirements.txt`, and it enters
an environment only through an import of `ai.common.torch` — which comes from
`ai/common/models/base.py::_ensure_dependencies` (and therefore also via
`gpu_guard.py` → `.base`). The `ai/common/models/` directory itself holds **no** `requirement*.txt`,
so reaching the package costs nothing by itself; the weight comes from that one file plus the
specific model family. Two consequences worth stating plainly:

- a node that does **not** reach `ai.common.models` resolves its own torch freely — its env compiles
  and installs that version into its overlay, and the overlay wins at import even when base holds a
  different one (`uv --target` does not treat base as satisfying — VERIFIED against the shipped uv);
- a node that **does** reach it inherits `ai`'s pin **legitimately**, so a version disagreement
  there is a real conflict to surface, not a check to relax. The ways out are model-server mode
  (the facades take the `ModelClient` branch and never import torch) or splitting the work across
  environments — not loosening the resolution.

Note also that `ai/node.py` reaches `gpu_guard`, but **no node imports `ai.node`** (verified across
`nodes/src`), so the launcher does not leak the pin into every environment.

**`-r` includes inside requirement files (IMPLEMENTED).** A requirement file may pull in another
with `-r other.txt`, and `uv` resolves that path **relative to the file holding the line**. Combining
moves those bytes into `combined.txt` in a different directory, so a relative include would be
looked for next to the combined file and the compile would fail there instead of at the node
(VERIFIED: `failed to read from file …cache\other.txt`). The combiner therefore rewrites include
targets to absolute paths — with **forward slashes**, because a requirement file treats `\` as an
escape and `-r C:\x\y.txt` reaches uv as `C:xy.txt` (also verified) — and `resolve_includes()` folds
the referenced files into the environment's set so they reach the drift hash; editing an included
file must be able to invalidate the resolution. A missing include is refused up front, naming the
referring file.

**Resolution rule (verified — NOT `nodes.<provider>`).** The entry module is **not**
`nodes/src/nodes/<provider>/` by string; that naive rule holds for ~91 of ~133 providers and **breaks
for ~42**. The authoritative mapping is: `provider` = the `logicalType` (protocol scheme with `://`
stripped, e.g. `detect://` → `detect`) → its matched `services*.json` → that file's **`path`
(`nodePath`) field** → dots-to-slashes under `nodes/src/` → a **package dir with `__init__.py`** that
re-exports `IInstance`/`IGlobal` (and `IEndpoint` for endpoint nodes); the node logic to AST-parse is
`IInstance.py`/`IGlobal.py` there. Traps the partitioner must handle: **aliases** (many providers →
one dir: `chat`,`dropper` → `webhook`; all `response_*` → `response`), **sub-package paths** (`remote`
→ `nodes/remote/client`, whose own `__init__.py` re-exports nothing — you *must* follow `path`),
**name ≠ dir** (`text-output` → `text_output`, `db_supabase` → `db_postgres`), and **native providers
with no `path`** (`parse`, `filesys`, `hash`, …) which have **no Python module and are skipped**. The
engine loader itself does exactly this (`python-global.cpp` uses `serviceDef.nodePath`, not the
provider string), so the partitioner must read `path` from `services*.json`, never synthesize it.

**Known risk — the AST premise is weaker than it looks (VERIFIED against `detect`, `pose_estimation`,
`audio_transcribe`, `ner`, `anonymize`).** The heavy pip packages are **not** declared per node and are
**not** chosen by per-variant `depends()` calls: no `requirements_pose`/`requirements_detection` files
exist, and `detect`/`pose_estimation` have **no `requirements.txt` and call `depends()` zero times**.
Instead —
- a node's `IGlobal.py` typically **defers** the `ai.common.models.*` import into `beginGlobal`
  (config-gated), and the actual package is a **lazy, config-selected import inside `ai`** — e.g.
  `from rfdetr import RFDETRBase` inside `ai/common/models/vision/detection.py::_build_backend`, picked
  by the `engine` config, **two hops from the node file**;
- config (`profile`/`engine`/`model`) selects a **runtime model backend, never a requirements file**.

Consequences a static node-file AST walk must confront:
- it **systematically under-includes** — the true deps sit behind deferred/config-driven imports inside
  `ai`, invisible to a walk of `IGlobal.py`/`IInstance.py`;
- the `depends()` **backstop does not rescue these nodes** — `detect`/`pose` never call `depends()`, so
  nothing installs mid-run; today their deps come from the **pre-populated shared `ai`/model-server
  environment**.

So per-env `ai/**` inclusion **cannot rely on node-file AST alone.** The walk must be **transitive
through the `ai` package** (follow deferred + config-branch imports inside `ai.common.models.*`, not
just the node's top-level imports); where a config branch selects among mutually-exclusive backends,
**include all reachable backends' `requirements*.txt`** (then verify they don't mutually conflict)
rather than trusting a runtime `depends()` that never fires. Nodes that *do* call `depends()` /
`load_depends()` (e.g. `detect_segment`, some TTS) install a **single fixed** `requirements.txt`, not a
variant — so the backstop is a narrow safety net, not the primary mechanism.

**Prototype result (VERIFIED — throwaway static-AST walk run against `detect`, `audio_transcribe`,
`anonymize`, the three hardest nodes).**
- **Correctness holds — static AST is feasible.** With two walker requirements — (1) collect
  **nested/in-function imports** (not just module-level), (2) resolve **relative imports** correctly
  (`__init__.py` package vs regular-module package) — the walk reached **every** ground-truth
  requirement file (`requirements_detection.txt`+`requirements_vision.txt`+`torch` for `detect`;
  `requirements_whisper.txt`+`torch`; `requirements_gliner.txt`+`torch`) with **zero under-includes and
  zero dynamic `importlib` calls**. The earlier worry that config-selected backends hide from AST was
  **wrong**: `from rfdetr import RFDETRBase` inside `_build_backend` is a *literal nested* import the
  walk sees.
- **The residual problem is PRECISION, not correctness — the `ai.common.models` barrel `__init__`.**
  `detect` imports by **full submodule path** (`from ai.common.models.vision.detection import …`) → the
  walk stays tight (only detection+vision+torch). But `audio_transcribe`/`anonymize` import via the
  **`ai.common.models` package `__init__`, which statically re-exports EVERY submodule** — so the walk
  transitively reaches the **entire model universe** and over-includes `rfdetr`, `rtmlib`, `gliner`, all
  OCR, `transformers` for an *audio* node. That reintroduces the very torch-conflict + bloat venvs exist
  to remove.
- **Prerequisite for precise scoping — DONE (Option A applied).** Either nodes import `ai` model
  submodules **by full path** (as `detect` already does), or the `ai.common.models` barrel `__init__` is
  made **lazy (PEP 562)**. **Chose Option A** (the 4 barrel importers now use full-path imports —
  `.gliner`/`.audio`/`.transformers`/`.ocr`); Option B was rejected because it does **not** help the AST
  walk without a matching walker change (`ast.walk` still traverses the `TYPE_CHECKING` re-export block,
  or under-includes if that block is removed). Measured effect: `audio_transcribe` **24 → 7** files,
  `anonymize` **23 → 5**, zero cross-family leaks.
- **Residual: within-family over-inclusion (DEFERRED decision — revisit after the engine call-site).**
  Cross-family isolation is exact — verified on the **real engine**: the `audio_transcribe` overlay
  dropped **183 → 114** packages, no `rfdetr`/`gliner`/`easyocr`/`surya`/`timm`. But a node still pulls
  **siblings within its own model family**, because the walk co-locates every `requirements*.txt` in a
  reached `ai/` directory — e.g. `audio_transcribe` pulls `kokoro` (TTS, `audio` family), `detect` pulls
  `rtmlib` (pose, `vision` family). Sound over-approximation, never under-includes; cosmetic (siblings
  are small). To tighten later, pick one:
  - **Option 1 (node-local):** the node imports the *specific* submodule
    (`from ai.common.models.audio.whisper import Whisper`) instead of the family `__init__` → drops the
    sibling for that node.
  - **Option 2 (walker):** in `ast_deps`, for `ai/` model dirs collect only files named by
    `_REQUIREMENTS_FILE` constants instead of blanket directory co-location → drops all siblings
    globally, but must re-verify it never under-includes.
- **Blast radius + generalization (whole node-tree sweeps, VERIFIED).** The barrel fix is **small and
  bounded: exactly 4 nodes** import via the barrel — `anonymize`, `audio_transcribe`,
  `embedding_transformer`, `ocr` — vs **9 already on full path** (`detect`, `ner`, `pose_estimation`,
  `caption`, `depth_estimate`, `audio_tts`, `background_removal`, `detect_segment`, `embedding_image`).
  So the prerequisite is a **4-node change** (or one lazy-barrel change), not a refactor. And the
  "no dynamic imports" result **generalizes**: a sweep of **all 481** node+ai-model files found **exactly
  one** dynamic import — `preprocessor_code/code.py`'s `importlib.import_module(modmap[lang_key])`, a
  **static lang→module dict** whose targets the walk can enumerate (or the runtime `depends()` backstop
  covers). Static AST is sound across the tree, modulo that one enumerable case.

**The backstop is not free** — an under-include means a possibly multi-GB `depends()` install happens
*mid-run* inside the venv child. Specify timing/failure: prefer resolving all reachable variants
**before readiness**; if a late install is unavoidable, block that lane (surfacing progress via the
heartbeat) and **fail cleanly** (do not hang) if it errors. AST is the pre-resolution + early-conflict
layer; runtime `depends()` is the safety net.

**Backstop contract.** (a) Prefer resolving every statically-reachable variant **before readiness**.
(b) When a dynamic `depends()` fires mid-run, block **only that lane**, surface install progress via
the existing heartbeat/sidecar (`updateProgress`), and on install failure **fail the lane with a clear
error** rather than hanging. (c) Bound the mid-run install with a timeout. Canonical cases: the
config-selected `rfdetr` import inside `ai` for the under-include path (backstop can't fire — `detect`
calls no `depends()`); `detect_segment`'s `load_depends(__file__)` for the single-fixed-file
`depends()` path.

**Model-server dimension (`--modelserver`) — a second axis the requirement set depends on (VERIFIED).**
Every `ai.common.models.*` facade branches on `get_model_server_address()` (`base.py`): with a model
server set it constructs a thin `ModelClient` (WebSocket RPC) and **never imports torch/rfdetr/whisper**
(`gpu_guard.py` installs a `sys.meta_path` blocker that makes `import torch` *raise* in this mode);
without one, `*Loader._ensure_dependencies` (`base.py`) installs + imports the heavy stack. The heavy
imports live **exclusively in the local (no-model-server) branch**. Implications for scoping:
- **Sound baseline (flag-agnostic):** both branches are statically present in the same file, so the
  transitive `ai` walk **over-approximates** — include the heavy `ai/**` requirements regardless of the
  flag. Always correct, but fat.
- **Pruning = the payoff:** make scoping **model-server-aware** — a proxied node contributes only
  wrapper/networking deps, not the `ai/**` heavy files. This shrinks venvs sharply and dissolves most
  cross-node torch-version conflicts. **Prerequisite:** node `requirements.txt` are **model-server-blind
  today** — `audio_transcribe` (faster-whisper) and `anonymize` (gliner) install heavy deps even when
  proxied, whereas `ner` (empty) / `detect_segment` (client-side only) already gate correctly. Pruning
  requires fixing the blind ones (or the scoper overriding them).
- **Does NOT sidestep the compile-time conflict.** The torch-2.0-vs-2.1 failure is at **constraints
  *compile* time** — a union over the `nodes/**` + `ai/**` globs taken **irrespective of `--modelserver`**
  (the flag changes only runtime install/import). So per-env scoping stays necessary in model-server
  mode; the flag is a **footprint optimization, not a conflict fix**.

### 4.9 Directory layout, identity & keying
- `<exe>/lib/site-packages` — **base = engine runtime only** (engLib + bundled deps); **no node deps**.
- `<exe>/venvs/<project_id>/<env_id>/` — **per-environment overlay** for EVERY env, a **top-level
  `venvs/` dir** (sibling of `lib/`, `cache/` — **not** under `cache/`). `env_id ∈ { main, <group_id> }`
  → main lives at `venvs/<project_id>/main`. Each holds `site-packages/` + its own scoped `combined.txt`,
  `constraints.txt`, `requirements.hash`, lock. The path helpers
  `_get_combined_path`/`_get_constraints_path`/hash are parameterized by the env dir. **The refactor
  surface is wider than the path helpers** — and by the code it is **three** problems, not one
  (IMPLEMENTED):
  - *Per environment:* the lock, the constraints file, the `uv --target` destination and the
    `_processed` record must switch **together**. They are now one object, `venv_env.EnvContext`,
    held in a process registry and applied through `depends.use_env(ctx)`, which restores the
    previous environment on exit **including on exception**. Registry entries live for the process,
    so re-activating an environment restores what it already installed.
  - *Per install operation (NOT per environment):* the progress sidecar and the heartbeat belong to
    one `uv` run, not to an environment — a nested install clearing them would take down the outer
    run's heartbeat, which is what keeps the task-startup timeout alive during long silent `uv`
    work. They are now an `_InstallProgress` instance per held lock, kept on a stack, and the
    heartbeat thread is bound to **its own** instance rather than to the top of the stack.
  - *Reentrancy:* per-env locks make `FileLock`'s non-reentrancy reachable. Byte-range locks are
    per file description, so re-acquiring a path this process already holds is refused exactly like
    a foreign holder, and the wait loop then polls forever against itself — a hang, not an error.
    The lock now counts depth in-process; cross-process semantics are unchanged.
- `<exe>/cache/models/<name>` — **shared** model weights via `model_cache_dir`, resolved relative to
  `sys.executable`. The venv child runs the **same `engine.exe`, unmoved**, so it resolves the same
  `cache/models` automatically. Models are weights, not packages → not isolated per venv.

**Constraints strategy: per-env, compiled independently of the global (VERIFIED live).** The single
biggest lever venvs pull is *not sharing one constraint resolution*.

- `<exe>/cache/constraints.txt` (**global**) governs **only** the base runtime and the legacy path
  (`ROCKETRIDE_SERVER_USE_VENV=0` / auto-without-venv). Under `=1` the **`nodes/**` glob is dropped from
  this startup compile** (`_SCOPED_EXCLUDED_GLOBS`) and it is compiled from `ai/**` + the root
  requirements alone; node dependencies then arrive exclusively through per-env scoped installs.
  **This gating is load-bearing, not an optimization (VERIFIED live).** While `nodes/**` stayed in the
  glob, every node in the installation had to be mutually satisfiable: two nodes pinning incompatible
  versions made `ensure_constraints()` fail at import of `ai/__init__.py`, so **the engine could not
  start at all** — before any pipeline, endpoint, or per-env logic ran. Per-env scoping cannot deliver
  its headline benefit while the startup compile still unions the whole node universe.
- `venvs/<project_id>/<env_id>/constraints.txt` (**per-env**) is compiled from **that env's
  `combined.txt` alone — no global base** — so it resolves versions solely from the requirement files its
  nodes reach. The scoped install **and** the runtime `depends()` calls active in that env both resolve
  against the **env** constraints (never the global), so the overlay is internally consistent and no
  global pin leaks in.
- **Granularity is per-env, not per-project.** `venvs/<proj>/main`, `venvs/<proj>/<group>` each get their
  own `constraints.txt`. This is exactly what lets an env with `torch 2.0` and another with `torch 2.1`
  coexist — a shared per-project constraints file would collapse them into one resolution and defeat the
  isolation venvs exist for.
- **Why `ai` makes this essential:** `ai/**` modules carry their own pins (`torch/requirements.txt` →
  `torch==2.10.0+cu128`, `requirements_detection.txt` → `rfdetr`, …). Under one global compile every pin
  meets every other; per-env, only the pins of the `ai` modules an env's nodes actually reach (via the
  AST walk) enter that env's constraints → fewer pins, fewer false conflicts, real conflicts isolated to
  their env.
- **Runtime `depends()` integration (implemented, live-verified):** while an overlay is active,
  `depends()` installs via `uv --target <overlay>` and `-c <env constraints>` (not `-c cache/…`), so
  node model-loads and the AST-miss backstop land **in the overlay at the env-resolved versions** (no
  version churn), keeping base untouched.
**Residual: base is not yet runtime-only — DEFERRED, with the reasoning recorded so the decision is
re-openable rather than re-derived.** Constraints are already fully per-env; base *today* still
receives `ai/**` at startup bootstrap. Two shrinks are possible and they cost very different things:

- **Half shrink — rejected.** Drop the pins from the startup compile while base is still allowed to
  *install* those packages (which is what happens whenever a model loads with no overlay active).
  Findings 2 and 3 below are the price of exactly this state, and it is not worth paying.
- **Full shrink — the goal.** Under `=1`, **nothing installs into base except the engine runtime
  set, because every model install happens inside some environment.** The compile then shrinks as a
  *consequence* rather than as a rule; findings 2 and 3 dissolve (base installs no torch, so it
  needs neither its index URL nor its pin); and **no conflict check is relaxed** — a conflict inside
  the real runtime set must still fail loudly, there is simply nothing left in that set to conflict.

**Ordering, not the glob, is the work item:** the non-pipeline entry points must get environments
**first** — the saas model server above all — or enabling the shrink lands the system in the
half-shrink state. Two constraints on computing a base set, should someone reach for the AST walk:
`nodes` has an authoritative `provider → path` manifest (`services*.json`) that makes the walk sound
and **base has no equivalent** (its live entry points depend on argv: `eaas.py`, `--modelserver`,
`engtest`, `depends.py` CLI, plus modules C++ loads by name), so the seed list would be
hand-maintained — the thing the glob avoided; and `_FIRST_PARTY` is `('nodes', 'ai')`, so
`extension.*` is invisible to the walk until the roots become configurable, making it a
cross-repository change. In favour of the effort: **no** module of the OSS base process (`ai/web`,
`ai/modules`, `ai/account`, `ai/eaas.py`) imports `ai.common.models`, `ai.common.torch` or the
image/avi/opencv helpers (VERIFIED), so the true runtime set really is small.

Findings behind the cost estimate, to re-verify when the question is reopened:

1. **A local model server exists — in the saas repo** (`rocketride-saas/extension/src/extension/
   model_server/`, deployed to `dist/server/extension/`). It is a base process (no pipeline, no
   endpoint, no overlay) importing `ai.common.torch` at module level and `ai.common.models` in
   `model_manager.py`, i.e. it loads the whole model stack into base — it is precisely the process
   for which today's global union is the correct resolution. Its own requirements never enter the
   startup compile at all: `REQUIREMENTS_GLOBS` has no `extension/**` entry, and saas installs them
   with explicit `depends()` calls.
2. **Half-shrink only:** dropping `ai/common/torch/**` also drops `--extra-index-url`. The
   `https://download.pytorch.org/whl/cu128` line in `cache/constraints.txt` comes from that
   requirements file; base installs constrained by the global file would stop seeing `+cu128`
   wheels, and the `torch==2.10.0+cu128` pin that keeps a stray PyPI torch out of base goes with it.
3. **Half-shrink only:** excluding from the compile does not exclude from installation. Files are
   still installed by runtime `depends()`, merely unpinned by the union, which makes base installs
   order-dependent (`uv --dry-run` reports the first-installed version as satisfied).
4. Base loses early conflict detection: two conflicting model families fail loudly at startup today,
   quietly and late afterwards.
5. Checked and **not** an issue: `onnxruntime-gpu==1.20.1` is pinned **explicitly** in both
   `requirements_whisper.txt` and `requirements_pose.txt`, not inherited from the union.

**Key by stable IDs; name is metadata.**

- `<project_id>` (the pipe-id) is a stable GUID at `config.pipeline.project_id`. Shared across all
  per-source task runs of the same pipeline.
- `<group_id>` (the venv-id) = the group node's `id` (e.g. `group_1`) — stable, generated once, **never
  changes when the venv is renamed** (the display name lives in `config.environment.name`).
- **Consequence: NO rename logic needed** — renaming a venv changes only metadata, not the path.
- **Requirements drift** detected by a `requirements.hash` inside the env dir (reusing
  `depends.py`'s `_compute_hash`/`_load_stored_hash`/`_save_hash`); mismatch → update install in place.
- **MAX_PATH (decision, not a note):** a 36-char GUID nested above `site-packages` + deep torch/nvidia
  paths **will** exceed Windows 260, and long-path support is host-opt-in/unreliable → **default to a
  shortened id segment** (e.g. first 8 hex of the `project_id` GUID; likewise `group_id`). Point all
  venv installs at **one shared `uv` download cache** so common wheels aren't re-downloaded.

### 4.10 Lifecycle: per-run process, install lock, purge/GC
- **Venv process = the pipeline run.** A venv child is spawned when the run starts and exits when it
  ends — a **sibling** of the main `engine.exe`, mirroring today's process-per-run model. It handles all
  objects in that run but is **never reused across runs**. No warm pool. Two runs (same or different
  pipeline) → separate processes → no interference.
- **On-disk env reused across runs** (only the process is per-run): installed once, keyed by stable IDs,
  drift detected by `requirements.hash`.
- **Install timing:** lazy on first run + opt-in deploy-time pre-warm; reuse `depends.py`'s existing
  install-progress reporting verbatim (`updateProgress` / heartbeat / sidecar), tagged per env.
- **Concurrent-install lock (race fix):** process-per-run + a shared cached env dir + install-on-drift
  could let two concurrent runs both `uv install --target` into the same `site-packages` → corruption.
  `depends.py` **already** has the `FileLock`/`install.lock` mechanism — **scope it per env dir** (one
  lock per `venvs/<proj>/<env>/`, not the single global lock) and define the **second-run
  wait-on-readiness** vs. fail behavior.
- **Purge & delete (canvas-driven).** *Purge* = remove all installed packages, keeping standard Python
  (delete the contents of the venv's `site-packages`; the base/stdlib survives because it's shared).
  - **Operation A — Purge (cog):** wipes packages, keeps the container + nodes. Allowed only when no run
    uses that env (active-task registry, `task_server.py`); deleting files a live process holds fails on
    Windows, so the gate is mandatory. Exposed as an engine command over the protocol (local + cloud).
  - **Operation B — Delete the container:** asks (1) delete member nodes + connections? (no = ungroup,
    keep them); (2) also remove the venv? (yes = delete the entire `venvs/<project_id>/<group_id>/`).
  - **Operation C — Pipeline deleted:** delete the whole `venvs/<project_id>/` subtree.
  - **Lifecycle coupling:** the dir lives as long as its canvas entity; **orphan-GC reconciliation** is
    the safety net (pipelines/groups can be deleted out-of-band — e.g. the `.pipe` removed directly).
    LRU eviction under disk pressure is a separate, secondary mechanism for still-valid-but-stale envs.

### 4.11 Overlay mechanism (sys.path; never move the binary)
The venv child runs the **original `engine.exe`, unmoved**; the overlay's `site-packages` goes
**ahead of base** on `sys.path` for **overlay precedence** (venv `torch` wins; appending would let
base shadow it). **`PYTHONPATH` won't work** (isolated `PyConfig`); use the runtime insert.

**Correction (measured):** this section previously said "the bootstrap reads
`ROCKETRIDE_VENV_SITE`". It does not, and nothing else does either — searched both as the literal
string across every `.py`/`.cpp`/`.hpp`/`.ts` in the repo and as the constant `VENV_SITE_ENV` that
carries it; both return the same two hits, in `venv_spawn.py`, being the definition and the single
**write** in `build_child_env`. The overlay that actually gets applied is the one
`ensure_env_scoped` computes for itself and hands to `_apply_overlay_path` via `on_overlay`, so the
variable is write-only decoration. Retiring the write (rather than adding a reader) is part of the
per-environment-scoping fix; see the step-8 record.

**It is a swap, not an insert (IMPLEMENTED).** Inserting without removing means applying a second
environment in one process leaves **both** overlays in front of base: the newer wins for packages
they share, while everything unique to the older stays importable — the cross-environment leak
overlays exist to prevent, arriving as a wrong version rather than as a missing import. Applying an
environment therefore removes the previously inserted overlay first, then inserts, then invalidates
the import caches for both paths. Base is never removed: it is the floor, not an overlay. The insert
position stays behind an injected `ROCKETRIDE_MOCK` shim directory (test stubs must keep beating the
real SDKs) and ahead of everything else.

**Honest limit:** the swap governs **future** imports only. Whatever the process already imported
from the previous overlay stays in `sys.modules`, so this does **not** make one interpreter safely
multi-environment — which is exactly why each environment gets its own child process (§4.10).

**Two doors, deliberately separate.** `depends.use_env(ctx)` switches *installation targeting* only
— lock, constraints, `uv --target`, the installed record — and never touches `sys.path`.
`ensure_env_scoped()` is the one entry point that does both. A caller that switches the first
without the second resolves dependencies into one environment while importing from another.

Because `sys.executable` is unchanged, `model_cache_dir`/`engine_cache_dir`/base `lib/site-packages`
all resolve to the shared install dir — `cache/models` shared, base runtime preserved. **Do not copy
the engine into the venv dir:** that changes `sys.executable` → cache/site-packages resolve to the venv
dir (wrong; also loses the engine runtime, and on Windows would require copying the `python3XX.dll` +
`vcruntime`). The dev debug shim copies `python.exe` but **in the same dir**, so it's safe; venvs don't
copy.

Supporting `depends.py` changes: `uv pip install --target <venv_site>`; reuse the existing post-install
cache reset (`importlib.invalidate_caches()` + `sys.path_importer_cache.pop`) on the venv path. **Verify:**
the insert position (venv ahead of base site-packages but not breaking the engine framework dirs
`rocketlib`/`ai`/`nodes`), and `uv --target` + `--no-build-isolation` behavior (`.pth`, console scripts,
the pywin32 path hack).

### 4.12 Response & failure merge-back
A venv node's final response and any `objectFailed`/`completionError` must be shipped back and **merged
into the root entry** the client reads (`data_conn.py:_close`), or venv-produced results/failures
silently vanish. This is what allows an **end/return node to live in a venv** (§4.11 asymmetry).
**Implemented in step 8.1.1's successor, 8.2** — the shape below is what shipped.

- **The `entry` frame.** When a child object ends, `venv_server` ships `entry.toDict()` plus the two
  fields `toDict` deliberately excludes — `objectFailed` and `completionError` — as a lane named
  `entry`. It is **not** a `lanes.py` entry: this is the bridge's own frame, not an engine lane, and it
  must never become bindable. The frame is emitted **before** the ack, on `close` *and* on any lane whose
  dispatch raises — a node that fails during the data phase kills the child's accept loop, so a
  close-only hook would merge nothing. One frame per object, and none at all when the child produced no
  response and did not fail, so a venv without an in-venv `response` node stays exactly as it was.
  *The skip is gated on the response, never on the payload:* `toDict` always emits at least `name`.
- **Where the child reads it.** From the `Entry` the bridge base holds, not `instance.currentObject`: by
  close time `cb_close` has set `pyCurrentEntry` to `None` and cleared `currentEntry`. `cb_open` binds
  the engine to that held object by reference, so the child's `response` node wrote into it.
- **Merge rule** (`nodes/venv/base/merge.py`, kept engine-free so it is testable without one): dicts
  merge deep — which unions `result_types` without special-casing; lists **concatenate, main's first**;
  scalars are child-wins. The response node's own `deep_merge_dicts` *replaces* lists and must not be
  reused. Only the response is applied — identity (`objectId`/`instanceId`/`version`/`parentId`) never
  is, though the frame carries the whole entry so the whitelist can widen later without a wire change.
  Double-counting is impossible by construction: `Entry::__toJson` emits `response` but
  `Entry::__fromJson` never reads it back, so the `open` frame cannot seed the child with main's.
  *Ordering caveat:* "main first" orders only what main holds **at merge time**.
- **Failure: decorate primarily, merge as the safety net.** The `error` lane cannot carry the child's
  code — the child wraps whatever a node raised into `APERR(Ec.RemoteException, …)` — so the `entry`
  frame is its only carrier. Main *stashes* the child's `completionError` and, since that frame always
  precedes the boundary's terminator, decorates the exception the boundary already raises with
  `__formatted` + `code`/`message`/`filename`/`function`/`line`. `call.hpp` restores those verbatim
  (and tests `__formatted` *before* its `APERR` branch), so the engine writes the child's real code and
  Python source location onto main's entry with no reconstruction here. Three ways to get this silently
  wrong: `__formatted` written inside a class body is name-mangled and invisible to the engine's
  `hasattr`; `completionError` exposes `file` while the decoration must supply `filename`; and all five
  attributes must be set or none, because a partial set makes the cast throw and degrades the error to a
  generic exception. A child can also fail *without* raising, so an unconsumed stash is applied as a
  `completionCode` after the round-trip returns — with the provenance folded into the message, since
  that path's location would otherwise point at `bindings.cpp`. The stash is consumed exactly once,
  which is what stops one run reporting two different errors, and the decoration is skipped on a
  `NoErr` terminator — that branch is also the *success* terminator, and eating the stash there would
  disable the fallback entirely.
- **Verified live.** `webhook → [venv: text_revert → response]` returns `text: ["olleh\n\n"]`, which is
  impossible without merge-back. For the failure half, `text_fail` inside the venv was **diffed against
  the same pipeline under `=0`**: `code`, `message`, `file`, `line` and `function` come back identical,
  so crossing the process boundary costs nothing in error fidelity.
- The branch lives in the shared base because **both** roles reach it through `callLocal`, not because
  it recurses. Under graph serialization (§4.6) it never does: a bridge is never nested inside a child —
  every environment's bridge node lives in main — so in a chain (`main→v1→v2`) each child's entry merges
  **directly** into main's root entry, one level, and two children contributing to the same entry is an
  ordinary repeated merge rather than a nested one.

### 4.13 Edge cases
- **Membership is `parentId`, not canvas geometry.** A node belongs to exactly one venv. Two boxes that
  visually **intersect** still have unambiguous membership (the partitioner reads `parentId`); the UI
  should prevent/warn on overlap. **Nested** isolated groups (venv-in-venv) are **rejected in v1**.
- **Start/source node must stay in `main`** (rejected inside a venv in v1) — the client drives the root
  pipe in the process it connects to. The guard belongs in `resolve_implied_source` (which does the
  exactly-one-`Source` detection), not `_check_pipeline` (which only validates the named source exists).
- **End/return node MAY be in a venv** — via response merge-back (§4.12). Asymmetric with the source
  (input is driven in; output flows back).
- **How many distinct environments drives the outcome:** no isolated groups → 1 process (today); some
  in main + some in venvs → partition + hub; **all nodes in ONE venv** → **collapse** to a single
  process running that venv's overlay (no bridge); ≥2 venvs with nothing in base → uncommon corner
  (v1: require the source's env to be root, or a base-env source).
- **Multi-source pipelines** are one pipeline with multiple lanes; constraints span the **whole
  document** (§4.7). **Process-count note:** each source is its own task/process today, so a pipeline
  with N sources and M venvs spawns **N × (1 + M)** processes (children are per-run, never shared across
  source-tasks, §4.10). The per-env install lock covers the shared *disk* env; the process count itself
  is the accepted v1 cost — revisit (shared children per pipeline) only if real pipelines hit limits.

### 4.14 Non-pipeline entry points (engtest, CLI, ad-hoc, test harnesses)
These have **no `project_id`**, so the scoping must degrade gracefully: `ROCKETRIDE_VENV_SITE` unset →
overlay no-ops → use base; `depends.py` tolerates a missing `project_id`/`env_id` and falls back to a
**default env** (or base). Concrete cases:

- **`engtest`** (engine-lib Catch2 binary, links engLib → embeds Python) runs
  `loadModule("nodes.webhook")`. Its `python::config` test asserts `sys.prefix == sys.executable dir ==
  rootDir` — our **no-move-binary overlay preserves this** (the rejected copy-binary approach would
  fail it → a free regression guard). Node modules import from `nodes/src` on `sys.path` (dev), so
  module-load needs no install; only third-party deps need the env.
  **Why it creates no `venvs/default` under `=1` — RESOLVED, and structural rather than a bug.**
  `engtest` *does* open service endpoints (`linkages.cpp` calls `getTargetEndpoint(...)` then
  `beginEndpoint(OPEN_MODE::SCAN)`), so the hook in `endpoint.cpp` does fire — but its task fixture
  is a **legacy filter-chain config** (`config.service.filters`) with **no `config.pipeline` at
  all**. So `components()` is empty and `project_id` absent, the hook passes an empty provider list,
  the AST walk finds no requirement files, and `run_scoped_install` takes its "nothing to scope"
  early return without creating a directory. Graceful degradation is therefore **verified rather
  than assumed** — and `engtest` **cannot guard scoping as written**: that would need a fixture
  carrying `config.pipeline.components[]`, a deliberate choice to make, not a defect to fix. It
  leaves `builder nodes:test` as the only regression guard for scoping.
- **`builder nodes:test`** runs **many** nodes' tests in one env today (works only because all nodes are
  currently compatible). Once venvs allow incompatible nodes, a single pytest process (one
  `site-packages`) can't host `torch 2.0` and `torch 2.1` tests → **per-node-scoped test envs**, reusing
  the same `depends.py` env-dir primitive, with incompatible nodes in **separate worker processes**
  pinned via `ROCKETRIDE_VENV_SITE` (composing with the planned pytest-xdist work). Declarative node
  tests are already mini-pipelines (`nodes/test/framework/pipeline.py`) → run them through the same
  partitioner. **Must land before the first incompatible node ships**, else the suite breaks.
  **The env key must become stable, too (VERIFIED).** The harness builds `project_id` as
  `f'test_{node_name}_{uuid4().hex[:8]}'` (`pipeline.py`), a fresh id per build, and `short_id`
  hashes the *full* id — so under `=1` **every suite run keys a brand-new overlay set and installs
  from scratch**, and nothing reclaims the old ones (a measured run added 41 directories to an
  existing 41). Two consequences: per-node test environments need a stable key, not merely a scoped
  install; and a "warm" timing measured on `nodes:test` is not warm at all — it is a cold install
  with a warm `uv` download cache, which understates the reuse a real pipeline gets from its stable
  `project_id`.
- **The saas model server** (`extension/model_server`, saas repo) is a fourth non-pipeline entry
  point and the one that matters most for §4.9's base shrink: no pipeline, no endpoint, no overlay,
  and it imports `ai.common.torch` at module level, so it loads the whole model stack into base.
  Its own requirements never enter the startup compile (`REQUIREMENTS_GLOBS` has no `extension/**`
  entry); saas installs them with explicit `depends()` calls. Giving it an environment is the
  prerequisite for base becoming runtime-only.

### 4.15 Compatibility & the venv master switch (`ROCKETRIDE_SERVER_USE_VENV`)
The whole feature (venv runtime **and** per-environment scoping) is gated by one environment variable,
so downstream/open-source consumers can pin today's behavior. **This is a permanent supported mode, not
a migration flag.**

The variable is read by `venv_env.use_venv_mode()` at the moment dependencies are resolved. It is set
on the **server** process (launch config, systemd unit, container env); the task subprocess inherits it,
and the whole execution path — startup compile, endpoint hook, per-env install — is therefore
self-consistent. Clients need nothing: an SDK client does not import `rocketlib` (the import in
`client-python`'s `dap_base` is guarded by `except ImportError`) and so never enters dependency
resolution at all.

**Known gap.** `<exe>/.env` is loaded by `ai/web/server.py` inside `WebServer.__init__`, but
`ai/__init__.py` calls `depends()` at import — so a value placed in `.env` is read **after** the
resolution it would govern and silently has no effect. Putting the switch there therefore does not work
today. Closing this properly means moving the engine's `load_dotenv` ahead of dependency resolution, not
teaching `venv_env` to parse the file.

- **Unset (default) = auto.** *Target state:* the partitioner inspects the *resolved* pipeline — an
  `isolated` group present → venv runtime + per-env scoping; none present → today's single-process /
  global-glob behavior. **Today `auto` is byte-equivalent to `=0` for every pipeline**, isolated
  group or not: the only producer of the isolated-group signal is the partitioner, which arrives in
  2B, and the engine hook calls `ensure_env_scoped(projectId, "main", providers)` with three
  positional arguments, so `has_isolated_group` keeps its `False` default. **Only `=1` scopes
  anything at all right now** — the §8.3 measurement (auto = 130 sources = exactly the legacy set)
  follows from this, not merely from the test pipeline lacking a group.
- **`=0` = force off (legacy mode).** Never partition: any `isolated` group is **demoted to a plain
  organizational group** (flattened into one process), and dependencies resolve via the **global-glob
  `constraints.txt` path**. Byte-for-byte today's behavior; **never an error**, even if the document
  contains isolated groups. This is the escape hatch for downstream consumers.
- **`=1` = force on.** Enables the venv machinery and per-env scoping (still a no-op partition if the
  pipeline genuinely has no isolated groups, but per-env `main` scoping applies). It **also drops
  `nodes/**` from the global startup compile** (§4.9), which is what actually lets nodes with
  conflicting pins coexist in one installation.

**Known limit of `auto` (honest).** The startup compile happens at process init, before any pipeline is
known, so `auto` cannot decide the node-glob question per pipeline: it keeps the legacy union and
therefore keeps the conflicting-nodes failure. In Phase 2A **only `=1` delivers conflict isolation**.
Removing the limit means taking node dependencies out of the startup path entirely (resolving them
per-env on first use) — the same work as the base-runtime-only residual in §4.9.

A second consequence of `=1`: nodes whose imports the AST walk cannot resolve statically (flagged
`dynamic_imports`) no longer get their dependencies from the startup glob and fall back to the runtime
`depends()` backstop, which installs into the active overlay (§4.8).

Open-source/default posture: with the var unset, a consumer who never creates an isolated group gets
exactly today's engine; `=0` additionally guarantees legacy behavior even for documents authored
elsewhere that carry `environment`.

---

## 5. Open questions — resolved (with residual verification noted)
1. **Venv membership gate — RESOLVED.** `REMOTING`/`noremote` is a *network* gate; the `noremote` set
   (local filesystem `core` source, DB nodes, `text_output`) exists because those nodes can't run on a
   *remote host*. Venvs are **same-host**, so that reason doesn't apply — **do not reuse the network
   `noremote` set.** v1 rule: **venv membership is unrestricted except the source node** (already forced
   to `main`, §4.13); DB/filesystem/`text_output` nodes run fine in a venv. Define a venv-specific
   capability only if a concrete node proves it needs the client's direct fd; none found so far.
2. **AST variant discovery — RESOLVED via §4.8 correction (premise was wrong).** No
   `requirements_pose`/`requirements_detection` variant files exist; `detect`/`pose_estimation` declare
   no `requirements.txt` and never call `depends()`. Heavy deps are lazy, config-selected imports **two
   hops inside `ai`** (`detection.py::_build_backend`), so a node-file AST walk **under-includes** and
   the `depends()` backstop **doesn't fire** for them. Resolution: the walk must be **transitive through
   `ai`** and **include all reachable config-branch backends' `requirements*.txt`** (§4.8). **A
   throwaway prototype has now PROVEN this** on `detect`/`audio_transcribe`/`anonymize`: zero
   under-includes, zero dynamic imports (§4.8 Prototype result). The remaining 2A prerequisite is
   **precision** — the `ai.common.models` barrel `__init__` re-exports every submodule, so barrel-
   importing nodes over-include the whole ML stack until the barrel goes lazy or nodes import submodules
   by full path. Second axis: the walk (or a model-server-aware pruning of it) must account for
   `--modelserver` mode, where the heavy `ai/**` deps aren't imported at all (§4.8 Model-server
   dimension).
3. **Non-isolated grouped pipelines — RESOLVED: they do NOT run today (verified).** `getProjectComponents`
   nests group children into the group's `config.pipeline.components`; the engine (`stack.cpp`) reads
   only top-level `components[]`; **no flattening pass exists** in `prepare_pipeline.py`, `pipeline.py`,
   `task_engine.py`, or C++. Grouped children are silently dropped. → The partitioner **must** own
   "flatten non-isolated groups" (already Phase-2B step 5); this also fixes an existing latent bug.
4. **Partitioner + orchestration ownership — RESOLVED: both in `task_engine.py`; pure transform in
   `pipeline.py`.** The engine subprocess is spawned in `task_engine.py` (`create_subprocess_exec`,
   ~L1561; readiness ~L501-520; teardown `_terminated` ~L563). Put the partitioner as a **pure function
   in `pipeline.py`** (sibling to `resolve_pipeline_env`/`resolve_implied_source`), invoked from
   `Task._build_task` (~L355) before the task file is written. Venv-child spawn/lifecycle/channel
   **mirror the existing engine-spawn pattern in `task_engine.py`**. `task_server.py` keeps the
   active-task registry (`_task_control`/`TASK_CONTROL.project_id`), the **port broker**
   (`assign_port`/`release_port`, already cross-called from `task_engine` ~L1500/1513), and auth/WS/DAP;
   it gains only the minimal shared-resource entries a venv child needs (a registry row + a port).
5. **Multi-process debug UX — DIRECTION set; detailed UX deferred to 2C.** Each venv child gets its own
   `--debug_port` from the existing port broker (`assign_port`), exactly as the main engine does today.
   The client attaches to **main**, which **advertises/multiplexes the child DAP endpoints** (it already
   owns the per-child sockets as the hub, §4.6). Cross-cut single-stepping and unified breakpoint UX are
   the genuinely open part → 2C.

## 6. Risks & gating requirements
- 🟠 **Metrics/billing multi-PID rollup (money bug) — driver-feasibility DE-RISKED; residual is
  grouping.** **CPU/RAM** sum across the venv process tree (per-PID). For **GPU**: **concurrent pipelines
  are already billed correctly today**, and each running pipeline is its own `engine.exe` PID — so the
  billing path **already attributes GPU across multiple independent PIDs** on our real hardware. That is
  empirical proof the driver-level per-PID capability exists, so the earlier "feasibility spike" is **no
  longer needed**. Venvs therefore reduce to **bookkeeping**: sum a venv's child PIDs under the **parent
  pipeline's** billing identity (the orchestrator already tracks which children it spawned) rather than
  counting them as separate pipelines. Residual (still money-critical but **desk-checkable, no spike**):
  confirm children are **grouped into the parent's bill** — not dropped (under-bill) nor double-counted
  as standalone pipelines (over-bill).
- 🟠 **Phase 2A blast radius = only pipelines where the scoped path is enabled** (`=1`, or auto with
  isolated groups). Under the default (unset, no venvs) **nothing changes** — §4.15 semantics. The
  radius becomes "every pipeline" only if/when a later release flips auto to scoped-by-default.
- 🟢 **AST correctness PROVEN and precision prerequisite DONE (§4.8 Prototype result).**
  `ast_deps.py` (implemented, 13 tests) resolves providers and does the transitive walk; over the three
  hardest nodes it reached **every** ground-truth requirement file with **zero under-includes and zero
  dynamic imports**. The over-inclusion residual (the `ai.common.models` barrel `__init__`) is **fixed
  via Option A** — the 4 barrel importers (`anonymize`, `audio_transcribe`, `embedding_transformer`,
  `ocr`) now import by full path; measured `audio_transcribe` **24→7** files, `anonymize` **23→5**, no
  cross-family leaks. A whole-tree sweep (481 files) found only **1** dynamic import (`preprocessor_code`,
  enumerable). Was 🔴 → 🟠 (prototype) → 🟢 (barrel fix applied).
- **Scoping should be model-server-aware (footprint optimization, §4.8).** Under `--modelserver` a
  proxied node needs no `ai/**` heavy deps (facades take the `ModelClient` branch; `gpu_guard` blocks
  `import torch`); pruning them shrinks venvs and removes most conflicts. Prerequisite: node
  `requirements.txt` are model-server-blind today (`audio_transcribe`, `anonymize`). Note: model-server
  mode does **not** remove the *compile-time* conflict (that glob union is flag-independent), so venvs
  stay necessary.
- 🟠 **Concurrent install race** — per-env lock (§4.10).
- 🟠 **Large image/video crossings — payload shrinks with cloud store, but the AV lanes stay.** In the
  target architecture AV bytes live in **cloud storage** (`ai..account.store`); bulk bytes are fetched
  from the store rather than streamed node-to-node. **AV metadata still crosses on the
  `writeVideo`/`writeAudio`/`writeImage` lanes**, so the bridge must still implement every AV lane
  (§4.4 is *not* reducible) — what drops is the per-frame **payload size** (small metadata vs multi-MB
  buffers), which is what removes the ~1 MB-chunk throughput risk. **Interim** (pre-store): raw AV
  crosses these lanes and must meet a throughput target on the loopback WS bridge (2-hop hub multiplies
  buffer copies), with UI warning on a heavy-lane boundary and shared-memory zero-copy as the v2
  fallback. Store-fetch in the child needs account context (secrets/`ROCKETRIDE_CLIENT_ID` propagation).
- **Cross-env cyclic deadlock** — v1: detect env-cycles at partition and reject.
- **Secrets scoping (a win):** partition runs on the *resolved* pipeline, so each child sub-document
  carries only the secrets its own nodes reference (the `ocr` venv never sees the LLM key). Document how
  account context / `ROCKETRIDE_CLIENT_ID` reaches each child.
- **Backward compatibility:** old `.pipe` documents lack `environment` and must run unchanged (additive).
- **Free upside:** separate processes = separate GILs → CPU-bound nodes in different venvs run *genuinely*
  in parallel.

---

## 7. Phased implementation plan

**Phase 1 — this design document.** (Done; pauses for review.)

**Phase 2A — Foundation: per-environment requirement scoping (no venvs yet; independently shippable).**
*Behind a feature flag with fallback to today's global-glob path (blast radius = every pipeline).*
1. `depends.py` parameterization — **DONE except the base shrink.** `ensure_constraints()`/install take
   an **explicit requirement-file set + env dir** (no global glob); uniform per-env build (main
   included); `uv --target <venvs/<project_id>/<env_id>/site-packages>`; per-env
   constraints/lock/`requirements.hash`; the overlay hook (a **swap**, §4.11); default-env fallback when
   no `project_id`; the module-global install state resolved into `EnvContext` + `use_env()`, an
   install-operation progress stack, and a reentrant `FileLock` (§4.9). One install-argv builder serves
   both paths, and `-r` includes are handled when combining (§4.8).
   *Still open:* **base = engine runtime only**, deferred with its reasoning in §4.9 — it needs the
   non-pipeline entry points (saas model server first) to get environments, or it degrades into the
   rejected half shrink.
2. **AST `ai/**` discovery** — once per init, cached; config-driven-variant + dynamic-import handling;
   runtime `depends()` backstop with defined timing/failure.
3. **Non-pipeline entry points** — `engtest` fallback (**done**: verified to no-op by construction,
   §4.14); `builder nodes:test` per-node isolation (**open**).
   **Trigger, not a reminder: the first node in this tree that is incompatible with another blocks
   on this item.** Until such a node exists nothing breaks, because the suite runs in one
   environment and all nodes are mutually satisfiable; the day one lands, `nodes:test` stops working
   and the fix is per-node scoped test environments in separate worker processes pinned via
   `ROCKETRIDE_VENV_SITE` (§4.14). Staging the `vtest_*` fixtures into `dist/server/nodes` belongs to
   this item too — it is the same plumbing, and it is what unblocks the end-to-end acceptance in
   §8.3. Do not build it standalone beforehand: the shape follows from the isolation work.
   *Payoff (opt-in, per §4.15):* when the scoped path is enabled (`ROCKETRIDE_SERVER_USE_VENV=1`, or
   auto with an isolated group present), the pipeline runs in a node-scoped "main" env → faster/smaller,
   no gliner/whisper bloat. **If no venv is needed** — the pipeline contains no isolated groups and the
   variable is unset — **it runs exactly as today**: one process, global-glob resolution, no behavior
   change. De-risks the dependency-model change in isolation.
   *Strategic note:* in **model-server deployments** the model servers already isolate the heavy
   conflicting deps (each server owns its own env), so **most of the value lands in 2A alone**; the 2B
   venv *runtime* is primarily for **internal / no-model-server mode**, where conflicting nodes share one
   in-process interpreter. This sharpens sequencing: ship 2A broadly, prioritize 2B for internal-mode
   users.
**2A-4 — OCR opencv de-conflict (investigated; DEFERRED, sequenced after 2B).** Full written
analysis + verified fact base + change list + verification plan live in
`packages/server/design/INVESTIGATE-opencv-ocr-venv.md` (bilingual; entry prompt for the work chat:
`NEXT-STEP-2A-ocr-opencv-prompt.md`). Scope: split the `ocr` node into per-services components
(standard EasyOCR+DocTR+tables in `services.json`, Surya in `services.surya.json`, TrOCR
proxied-only), demote the `ai.common.opencv` shim to a pure re-export, pin engines honestly, and
add `--overrides` (+ a =1 contrib-last ordered opencv install) so each engine resolves its true
OpenCV instead of the silent shim-forced downgrade. **Why it can wait:** it is NOT on the critical
path — under =1 an OCR env still COMPILES today (unpinned engines backtrack silently, opencv stays
4.13, the shim `depends()` is a no-op), so the venv runtime runs OCR on the existing shim hack with
no crash. It is an independently-shippable Phase 2A quality/correctness item; the silent
surya→0.16.1 downgrade is a dormant issue (surya/trocr are `contract-check: disable`, and OCR is
proxied in model-server deployments). **Trigger to pull it forward:** a near-term product need for
local Surya/TrOCR usability, or evidence the silent downgrade is actually biting a local load.
Do 2B (partitioner cut → spawn → orchestrator) first.

- **Tests (§8.1–8.3):** AST-walk / resolution-rule / `depends`-parameterization / model-server-pruning
  **unit tests**; the `vtest_alpha`/`vtest_beta` **fixture nodes**; the **no-venv-conflict-fails** and
  **only-needed-installed (no-whisper)** acceptance tests; embedding-invariant regression.

**Phase 2B — Venv runtime (the isolation feature), on top of 2A.**
4. **Schema + UI — DONE except the creation entry.** The Virtual Environment container as a canvas
   node type stored as a `group` + `config.environment` (§4.1), `PipelineEnvironment` /
   `NestedPipeline` / `PipelineComponentConfig` in the SDK schema, `isContainerType()` across the
   three containment checks, the isolated treatment, the name/isolated config form, and the
   canvas-side rejection of a container dropped into a container. Round-trip tests
   (`canvas/util/graph.test.tsx`) pin the document mapping. Bridge nodes stay internal.
   *Deferred to step 5, deliberately:* the **creation entry** (a container that cannot execute yet
   would only produce pipelines that lose members), the **source-in-venv** guard, which belongs where
   `resolve_implied_source` runs, and **env-cycle** detection, which needs the quotient graph the
   partitioner builds.
5. **Partitioner:** generalize `prepare_pipeline.py` (flatten non-isolated; cut isolated; insert bridge
   nodes; routing table; full-document node set).
   *Increment 1 — **DONE**:* flattening and the structural validations (§4.3), hooked into
   `task_engine` before `_check_pipeline`, with 19 unit tests. This closes the §5.3 bug on its own:
   grouped components reach the engine instead of being dropped. It also unblocks the **creation
   entry** deferred from step 4 — a container now executes as an organizational group rather than
   losing its members.
   *Increment 2 — **DONE**:* the cut (`scoped=True` → `PartitionResult`). Per-env sub-documents
   (leaf-bucketing by transitive env), `venv`/`venv_server` bridge pairs at each boundary lane edge with
   `channelId`-keyed routing table, one bridge node per environment in main (step 8.3 makes a
   venv→venv lane an edge *between* two of them), and venv-only env-cycle detection over the quotient
   graph, plus the source-in-venv guard on the implied
   source. 39 unit tests in `test_partition_cut.py` after 8.3; the 19 increment-1 tests are unchanged.
   Wired at the call site since step 7 — `task_engine.py` calls `partition_pipeline`
   with the default `scoped=False`; the orchestrator flips the gate in step 8 via
   `scoping_enabled(use_venv_mode(), has_isolated_group(doc))` (helper exported from `pipeline.py`).
   Two step-7 flags recorded: a return-only (`venv→main`) client has `input: []` (does the engine
   instantiate an input-less filter?), and `venv_source_stub` is registered nowhere / not special-cased
   in C++ (child-engine acceptance is open — stub creation is isolated in one helper for an easy revision).
6. **Bridge: extract shared base + new `venv` node** (all 15 lanes; `image`/`video`/`audio`); network-
   remote untouched.
7. **Local spawn + transport (v1 = WS-over-loopback unchanged):** spawn the venv child (its overlay) and
   point the existing `remote` WS bridge at it over loopback (Bearer token; raise the ~1 MB AV ceiling);
   routing through main's engine graph (step 8.3), not a transport-layer hub. No layer-2 swap in v1.
   *Step 7 — **IMPLEMENTED, live round-trip verified**.* The child is a normal task
   subprocess (mirrors `task_engine`'s engine spawn) whose source is a **resident** `venv_source_stub`
   (`nodes/venv/source`, `classType: source`, `register: endpoint`): its `scanObjects` publishes
   `app.state.target`, mounts the new `ai/modules/venv` module (`/venv/pipe`) and blocks for the run.
   *Amended when #912 landed:* the stub no longer builds its own `WebServer`. Every subprocess now
   gets a shared one, bootstrapped by `ai/node.py` from `--data_port` **before** the engine runs, so a
   second listener on that port made the child die at startup on the bind. The stub takes
   `node.require_shared_web_server()` and mounts `/venv/pipe` on it — the migration `webhook` and
   `telegram` already received — and blocks on a `threading.Event` instead of `server.run()`. Two
   consequences worth keeping: the route is appended to an **already-serving** app (fine — Starlette
   resolves routing per request), and the child now also serves `/task/data`. *Correction: that last
   point previously read "which is what 8.4's metric fan-in wants" — it is not.* `/task/data` is the
   DAP channel for pushing objects **into** a pipeline (`_send_data`/`TaskData`) and carries no
   resource metrics; 8.4's fan-in is psutil over the child PIDs plus the child's own `>MET` frames
   over its stdio, neither of which needs that route. The child serving it is still useful, just not
   for this. A third is a narrowing: `probe_ready` completes a TCP handshake against the
   shared server, which binds at bootstrap, so it no longer proves `/venv/pipe` is mounted — the
   child wins that race comfortably today (it only has to finish its own engine init while the
   bridge's first dial waits on a whole main-engine startup), and the symptom if it ever loses is a
   refused dial rather than a hang. Gating (`task_engine.py`, `_venv_scoping_enabled` →
   `scoping_enabled(use_venv_mode(), has_isolated_group(doc))`, module-top `import venv_env`) branches to
   `_spawn_venv_children`: assign a port, write the child task file, build the child env
   (`ROCKETRIDE_CLIENT_ID` + per-run `ROCKETRIDE_VENV_TOKEN` + `ROCKETRIDE_VENV_SITE` via `venv_env` when
   the overlay exists), `create_subprocess_exec` with `--autoterm`, drain stdio, and TCP-probe readiness —
   all children up before the main engine, whose `venv` nodes dial them. The token rides the inherited env
   (§4.5), never the config. Teardown (`_terminated`, universal exit path): two-phase `terminate→kill→wait`
   per child + `release_port` + remove task file; children are resident and never self-stop.

   **Startup-failure handling (verified).** A main-engine error surfaces exactly as under `=0` (its spawn
   path is unchanged). A child that fails readiness fails the run synchronously (the `use()` call raises →
   the canvas shows it) with a message that **names the venv and quotes the child's own output** (a ring
   buffer of its last stdio lines), so the child engine's real error (a validate failure, a dependency
   conflict) is visible, not just an exit code. The failing child cleans up itself in `_spawn_one_venv_child`
   (kill+reap + cancel drains + drop its task file) — it is not yet registered for `_teardown_venv_children`,
   and a *hung* one would otherwise survive until server death (its stdin stays open, so `--autoterm` never
   fires); already-spawned siblings are killed by `_terminated`. Live-verified: a bogus-provider child left
   **no orphan `engine.exe`** and surfaced its `>ERR*InvalidParam…` to the caller. Richer per-child
   monitor/trace fan-in (beyond the stdio tail) is step 8.

   **Return-path architecture — the `remote` request/response model (decisive).** The venv boundary is a
   request/response *splice of one object*, and the engine allows exactly one open object per pipe stack,
   entered at the root (`pipe.instance.cpp`) — so an async re-injection of the return on a *separate*
   channel cannot open the already-open forward object, and the SDK's `send()` awaits the *forward* pipe's
   `close` (correlated by DAP `pipe_id`/`request_seq`, not `objectId`, `data_conn.py`), so a fresh return
   pipe cannot reach it either. The return therefore rides back over the **forward** socket and re-enters
   main through the **same** node that sent it — exactly how `remote` works. A linear `main→venv→main`
   boundary is spliced with **one** main-side round-trip `venv` node (`producer→venv→consumer`): `open`/
   `closing`/`close` are `callRemote`-forwarded and returned normally so the engine also propagates the
   framing to the downstream consumer (the object opens/closes once); forward `write*` are `callRemote`-ed
   and `preventDefault`-ed (the forward stream does not leak downstream); the venv's return arrives
   interleaved on `callRemote`'s ack channel and is applied downstream via `callLocal → self.instance.write*`
   on the already-open object. The child runs a forward `venv_server` ingress (`handleWebSocket` accept
   loop) and a return `venv_server` egress (engine-driven `write*` → `callRemote` back), which
   `/venv/pipe` binds to **one** socket (dialled `?channel=<forward>&return=<return>`); the egress's
   `callRemote` nests inside the ingress loop's `callLocal`, one thread, no second loop. This dissolves the
   earlier Blocker B (no input-less main ingress, no synthetic `control` edge) and Gap C (no async receive
   pump, no separate return socket). (Blocker D still applies: `venv_source_stub` is a registered resident
   source — a no-op source exits before WS data arrives — and both `venv_server` and the round-trip `venv`
   node carry a passthrough `lanes` map so the child ingress and the mid-chain round-trip node pass the
   task-file `validate()` lane-linking.) As shipped in step 7 this rejected, with named causes, venv→venv
   channels, multi-lane fan-in/out into one venv, and `main→v1→v2→main` chains; **step 8.1 lifted the
   fan-in/out restriction** (one bridge node per environment, all its lanes on one socket) and **step
   8.3 lifted venv→venv and chains** (graph serialization). What remains rejected is the same lane
   from two producers on one boundary — now the whole environment's boundary, so it bites more often
   than it did with pair-keyed channels.

   **Live proof (`ROCKETRIDE_SERVER_USE_VENV=1`, `webhook → [isolated group: text_revert] → response`):**
   sending `"hello"` spawns the resident child, dials one Bearer-authenticated socket, crosses
   main→child, `text_revert` reverses in the child, the return crosses child→main over the same socket, and
   the SDK `send()` result carries `text: ["olleh\n\n"]` on the forward object. Regression: `=0` flattens
   in-process (same `"olleh"`, **no child spawned**). `builder ai:test`: 1449 passed / 122 skipped.
   *That proof was obtained against a **pre-#1667** engine, where `pipe.closing()` still performed both
   passes; increment 8.1.1 collapsed the framing to the single `close` round-trip and re-verified the
   same shape on a rebuilt engine (§4.4).*
   Orphan-safe binding and N-child metric/monitor fan-in remain step 8; venv→venv routing landed as
   8.3 (graph serialization, not a hub) and merge-back for a `response` node *inside* a venv as 8.2.
8. **Orchestrator** (`task_engine.py`): N children/run (sibling lifetime), channel wiring, readiness,
   teardown-with-run, response/failure merge-back, monitor/trace/SSE fan-in, **metric aggregation across
   child PIDs**, orphan-safe binding (OS process-tree: Windows Job Objects / Unix process groups),
   install reporting, purge/delete + GC. Sequenced as independent increments.
   *Increment 8.1 — **DONE**, live-verified:* multi-lane fan-in via one bridge node per child
   (Arch-1). One bridge node carries every lane of a boundary over one socket and the header `lane`
   demuxes to the consumers; the same lane from two producers on one boundary is rejected, since
   `write*` carries no producer identity. (Channels were keyed by `(sourceEnv, targetEnv)` here; 8.3
   re-keyed them `(direction, env)` so one environment's whole boundary is one channel whatever the
   other end is.) Live: linear `hello`→`olleh`; the shape that previously deadlocked now fails fast
   with a named cause.
   *Increment 8.1.1 — **DONE**, live-verified:* collapse the boundary's two framing round-trips into
   one. Engine #1667 rebound Python `instance.closing` to `cb_closing`, so the bridge's
   `closing`+`close` frame pair began driving the child's closing pass twice. A bridge now ends the
   child object with a single `close` frame, sent from the main node's `closing()`, and refuses a
   received `closing` lane with a named cause (§4.4). Behaviour-preserving — it only ever calls
   `pipe.close()`, the one call #1667 did not change — and it drops the second, always-inert
   round-trip. A/B on one rebuilt engine: collapsed → `text: ["olleh\n\n"]` (the reversed token
   exactly once); the two-frame shape restored → the boundary **hangs**, so the regression is a
   deadlock rather than merely duplicated output.
   *Increment 8.2 — **DONE**, live-verified:* response/failure merge-back (§4.12). A `response`/`end`
   node inside a venv now reaches the client: `webhook → [venv: text_revert → response]` returns
   `text: ["olleh\n\n"]`. The failure half was diffed against the same pipeline under `=0` — `code`,
   `message`, `file`, `line` and `function` come back identical, so the boundary costs nothing in
   error fidelity.
   *Increment 8.3 — **DONE, live-verified**: graph serialization (chains + diamonds).* Channels are
   re-keyed `(direction, env)`: one forward and one return channel per environment, so a venv→venv
   edge is cut at **both** boundaries it crosses and reaches its consumer as an ordinary main-graph
   edge between the two bridge nodes (§4.6). The only new wiring is a forward lane's *main-side
   source* — the producer when it lives in main, otherwise the producing environment's bridge node.
   Children are untouched: each still sees the linear step-7 shape, which is why the whole
   behavioural change is `pipeline.py` plus deleting the duplicate spawn-time guard in
   `task_engine.py`. New rejections: an environment entered more than once around a base component
   (it collapses onto its single bridge node, `MV → m → MV`), and the same-lane-two-producers rule
   now applied to the whole merged boundary. 39 partitioner tests.
   *Live (chain, diamond, and a linear regression, each against the same pipeline under `=0`):*
   `main→v1→v2→main` with **three** reversals returns `olleh` — deliberately odd, so a pair of
   bridges that quietly passed text through could not produce it; the diamond (v2 fed `text` from v1
   and `json` from main, `response` inside v2) returns **both** lanes, `text: ["olleh\n\n"]` and
   `json: [{len: 5, text: "hello"}]`, merged home by 8.2's `entry` frame. Both match `=0` exactly.
   Two children were spawned and each reported ready (`%TEMP%/venv-child-v1.log`, `-v2.log`) — the
   first live run with more than one child. This settles the one assumption the design had rated
   "high confidence, unmeasured": **bridge-to-bridge nesting inside main works** — `MV1.callRemote`
   holds the stack while `MV2.callRemote` runs on a second socket.
   *Merge-back with two children (§4.12 for N > 1).* The chain exercises no merge-back and the
   diamond exactly one, so a `response` was placed inside **both** venvs of a chain, with main
   holding none: the root entry comes back `text: ["olleh\n\n", "hello\n\n"]` — one contribution per
   child, v1's first. That is the closing order (`MV1` closes before `MV2`, so its entry merges
   first and the list concatenation preserves it), and `=0` returns the same two values in the same
   order, so crossing the boundary changes neither the set nor the ordering.
   *Failure paths, both matching `=0` on `code`/`message`/`file`/`line`/`function`:* `text_fail`
   inside a venv, and — new to the chain — `text_fail` in the **upstream** venv, where `MV1.closing()`
   raises and aborts the remaining flush walk so `MV2` never closes. The cause still arrives intact
   (the failure stash is per bridge instance, so the downstream bridge cannot swallow it) and nothing
   hangs.
   *Fixture added:* `nodes/src/nodes/text_to_json/` (text → json, emitting during the **data phase**).
   `webhook` declares the `json` lane but does not emit it for a `text/plain` send, so a diamond
   wired straight off the source runs vacuously on that side — measured, and the reason the fixture
   exists rather than a convenience.
   *Increment 8.4A — **DONE, live-verified**: the child event fan-in.* A child's stdout was drained
   into a ring buffer and a `%TEMP%` log and nowhere else, so anything a node inside a venv emitted
   died at the boundary. It now runs through a DAP stdio pump (`Task.VenvChildStdio`), the same
   parser the main engine's stdio already uses, and each parsed event is routed by a **pure**
   `classify_child_event` in `venv_spawn.py` returning `(channel, side_effects, rename_to)` —
   side effects a *set*, because one event can forward, feed the tail and set the status at once.
   Four rows are decisions rather than mappings. `apaevt_trace` (`>DBG`) is forwarded on the FLOW
   channel under its **own** name `apaevt_venv_trace`, tagged `body.env`, **not** derived into
   `apaevt_flow`: a child's pipe indices are its own, so merging them corrupts main's
   `pipeflow.byPipe`, and emitting them as `apaevt_flow` corrupts the *client's* reconstruction,
   since the TS log codec keys open-flow stacks by `body.id` — declining to merge fixes only the
   server, declining to derive fixes both. It stays gated on the run's trace level, or the boundary
   would deliver trace volume `=0` does not. `apaevt_status_state` (`>SVC`) drops to detail only:
   `Task.on_event` handles it *before* the `apaevt_status_` prefix branch, where it lifts
   `_billing_gated`, so a child could otherwise start billing a run that has not started.
   `apaevt_status_message` (`>JOB`, by far the loudest — 265 per child in one measured chain run)
   sets the run's status **only while no main engine exists yet**, env-prefixed; that window is
   child startup, which is the point, since it turns a silent 30-second death into
   `[v1] Downloading torch (2.7GiB)`. Its predicate is a per-run flag, **not**
   `_engine_process is None` — that attribute is assigned once at spawn and never nulled, so on a
   *restarted* task the window would never reopen. And unknown families are logged rather than
   forwarded to the DEBUGGER channel, which is keyed to main. Errors and warnings join the run's
   own, env-prefixed.
   The child also inherits the run's **effective** `--trace=` — the launch request's `args` first,
   `startup_args()` only as fallback, mirroring what main does; inheriting just the fallback
   recreates the asymmetry the moment a launch passes its own flag.
   *Two mechanics the pump does not give for free.* `TransportStdio.disconnect()` **cancels** its
   stream tasks rather than draining them, which would truncate exactly the last lines step 7's
   startup diagnostic quotes — so `VenvChildStdio.drain_pending(timeout)` awaits them to natural
   completion first. And `disconnect()` fires `on_disconnected` itself, so a per-child `stopping`
   flag gates the "child died" wording; without it every clean run ends by logging N spurious
   deaths. The `%TEMP%` mirror is now keyed `venv-child-<env>-<port>.log` and truncated at spawn:
   it was opened `'a'` and keyed by env name alone, so one file accumulated every run of every
   project that ever used that name (measured: 27577 B vs 3327 B for one run), while a plain
   truncate under the old key would let two concurrent runs sharing a name (`v1` is every test's
   favourite) truncate each other's live log.
   *What the new event name costs the run log, checked and accepted rather than discovered later.*
   `RunLogWriter.append` has **deliberately no type filter** ("every event delivered to clients is
   recorded"), so child traces become part of replay with no registration needed — intended, but a
   volume *and* content change to the persisted artifact, not just a wire change. The catch is one
   level down: the v2 codec's keyframe/delta encoding branches on the literal name
   (`run_log.py:840`, `if event == 'apaevt_flow'`), so `apaevt_venv_trace` skips it and child
   traces are stored **raw where main's are delta-compressed**. At `full` with two children that is
   a real multiplier, softened only by `truncate_event`'s payload cap. Accepted for v1 — the volume
   sits behind an opt-in trace level — and left in 2C rather than fixed here, because the fix is
   two-sided: `run_log.py`'s encoder and `log-codec.ts`'s decoder must learn the name **in
   lockstep**, or replay breaks in a way no test in this plan would catch.
   *Live, against a two-child chain at `pipelineTraceLevel='full'`:* 48 `apaevt_venv_trace`, **all**
   tagged, two distinct envs, and **zero** `apaevt_flow` carrying an env tag — the F7 collision does
   not occur. The step-7 bogus-provider regression still quotes the child's own error, and now
   quotes it *parsed* (`apaevt_status_error: InvalidParam*…*pipeline_config.cpp:212`) rather than as
   a raw `>ERR*` line. All six `venv_live.py` shapes return their known values.
   *Correction, measured while verifying:* `pipelineTraceLevel` and `--trace=` are **different
   knobs** — the first is a per-task option (`task/core/execute.cpp`), the second the engine's
   startup log level (`core/init.cpp`). A control run at `summary` returned identical trace-payload
   keys for child *and* main, so the payload proves nothing about the inherited flag; the child's
   command line does. Verified that way with `args=['--trace=debugOut']` — the path the VS Code
   extension uses — identifying this run's processes by diffing the engine pid set across `use()`,
   since engines from earlier runs linger and answer the same query: 3 of 3 carried the flag.
   *Adjacent defect, recorded not fixed:* the main-engine spawn inherits **two** flags from
   `startup_args()`, `--trace=` and `--node_path=`; the child spawn inherited neither. 8.4 fixes
   `--trace=` because its own promise depends on it. `--node_path=` is the same root: a developer
   pointing the engine at workspace-local nodes gets them resolved in main and **not** in any venv
   child, so a pipeline that runs flat fails once a group is isolated, naming a provider the child
   cannot find. Whoever hits that will otherwise debug the partitioner.
   *Increment 8.4B — **DONE, live-verified**: metrics.* `TaskMetrics` samples the main PID and its
   *recursive descendants*; venv children are spawned by the server, so they are **siblings** and
   the walk never reached them. `register_extra_pid(env_id, pid)` adds each child and its own
   subtree to both `_sample_cpu_memory` and `_sample_gpu`. Keyed by env id rather than appended to
   a list: a restarted task re-registers the same environment, and a duplicated handle would
   silently *double* that environment's billed CPU and memory — an overcharge, not a crash.
   Registration happens right after the `TaskMetrics` constructor, **not** at spawn, where
   `_task_metrics` is still `None` because children are spawned before the main engine. The cost of
   that ordering, accepted rather than engineered around: a child's startup — including a long
   overlay install — is not sampled, which matches the `serviceUp` billing gate (install time is
   not billed either) but does mean "peak memory" is a peak over the *run*, not over each
   process's life.
   *Child load is billed, not report-only:* under `=0` the same work runs inside the main engine
   and is billed there, so excluding it would be an unintended discount for using a venv.
   *A live defect fixed alongside, independent of the fan-in:* `merge_subprocess_metrics` kept one
   snapshot slot, and a snapshot **replaces**. Children already emit `>MET` today, so whichever
   engine reported last erased the others' timers and counters. Snapshots are now per source and
   summed; the `source='main'` default leaves every existing caller unchanged.
   *Defect found in 8.4A's own commit while starting this one:* the fan-in already called
   `merge_subprocess_metrics(..., source=...)` against a signature that had no such parameter — a
   `TypeError` on a route with no unit test, whose only live symptom was a child's `>MET` (one per
   run, arriving near the end) failing silently inside the stdout reader. The route now has a test,
   written with `create_autospec` rather than a bare `MagicMock`: a bare mock accepts any keyword,
   which is exactly why the original slipped through.
   *Live:* a two-child chain reports `peak_cpu_memory_mb` **546.1** against a measured
   178.0 + 178.6 + 187.2 = 543.8 for the three engines. Asserted as "greater than the largest
   single engine", since main is itself one of them — so main-tree-only sampling cannot pass it.
   *Remaining:* **8.5** readiness proof + orphan-safe teardown; **8.7** per-environment scoping,
   which §4.11's correction above shows reaches no child under `=1` and, under the default `auto`,
   no process at all; **8.6** purge/delete with active-run gates.
   *Observed while verifying 8.2, recorded for 8.5 rather than fixed here:* after an in-venv failure
   a **second `send()` on the same token fails fast** — the boundary socket closed cleanly (1000) and
   the SDK raises `PipeException` with its usual "pipeline isn't running" diagnostic. It does not hang
   and does not silently return a stale result, so the failure mode is acceptable as it stands. The
   child stays resident for the rest of the run and is reaped when the task ends normally.
   *Correction, measured before 8.5 was started:* this record previously said "**only an abruptly
   killed server leaves orphans**". It does not — `--autoterm` handles that case. With a `chain` run
   live under `=1`, the tree was server → main engine + two children; `taskkill /F /PID <server>`
   (no `/T`, so no tree kill, and no Python teardown ran) left **zero** `engine.exe` after 8 s. The
   stdin monitor in `engLib/core/init.cpp` fires as designed. What 8.5 actually closes is
   **grandchildren** — `subprocess.Popen`'d `ffmpeg` in `ai/common/avi/reader.py`, the audio
   loaders, `uv`, model servers — which have neither that monitor nor a pipe from the server, and so
   survive the server's death on every platform. Measured on Windows only; the Linux half is 8.5's
   own verification. Worth keeping from the original note: leaked processes that *do* survive keep
   holding their port and answer later runs with *their* pipeline's results, which reads as "the
   feature broke" when nothing did.
- **Prerequisites the 2A state uncovers rather than closes** — both cheap, both blocking the moment
  two environments live in one interpreter:
  - `BaseLoader._dependencies_loaded` (`ai/common/models/base.py`) is a **class-level bool**. With
    `processed` now per environment it becomes the *binding* constraint: it short-circuits before
    `depends()` is even called, so a model loader that ran in env A contributes nothing to env B's
    overlay. Convert it to a set of env keys.
  - Per-node test environments need a **stable** env key — today's harness id is regenerated per run
    (§4.14).
- **Tests (§8):** partitioner **unit tests**; the **two-venv conflict-coexists** acceptance
  (`vtest_alpha`/`vtest_beta` split across venvs); compat `=0` isolated-group **demotion**; purge /
  delete / GC **lifecycle** (blocked while a run is active).

**Phase 2C — Polish & scale.** Multi-process debug/observability across the cut; deploy-time pre-warm;
the local-IPC transport seam (UDS/named-pipe/shared-mem, §4.5); the **bridge-base + `write_lane`
unification** (move `remote/base` onto the shared bridge base = **A1**, and extract a shared lane-write
dispatch in `packages/ai` used by both `data_conn` and `venv_server` = **D1**, §4.4 Step-6 decisions) —
both behavior-preserving refactors deferred out of 2B to run under the green test baseline; v2
optimizations (direct venv↔venv mesh, shared-memory for AV). Added by 8.4: teaching the run-log
codec to delta-compress `apaevt_venv_trace` the way it already does `apaevt_flow` (a two-sided
change — `run_log.py`'s encoder and `log-codec.ts`'s decoder in lockstep — worth doing only if the
log volume actually bites), and a per-environment renderer for those traces, which the tagged
`body.env` now makes possible but which no client has yet.

---

## 8. Verification & testing
Three layers; each test is tagged with the phase that first makes it runnable (**[2A]** = scoping only,
**[2B]** = needs the venv runtime).

### 8.1 Unit tests
- **AST discovery walk** (`depends.py`) — promote the throwaway prototype (§4.8 Prototype result) to a
  real unit test with a golden requirement-set per fixture: `detect`→{detection,vision,torch},
  `audio_transcribe`→{whisper,torch}, `anonymize`→{gliner,torch}. Assert the two properties proven
  necessary: (a) **nested/in-function** imports are followed; (b) **relative imports** resolve correctly
  (`__init__` package vs module). Plus: `provider → path` resolution (aliases `chat`/`dropper`→`webhook`;
  sub-package `remote`→`remote/client`; name≠dir; native no-`path` skip); dynamic `importlib` flagged;
  **barrel-`__init__` over-inclusion guard** (full-path import stays tight, barrel import is detected). [2A]
- **`depends.py` per-env parameterization** — env-keyed paths / lock / `_processed` / progress;
  `requirements.hash` drift → reinstall; default-env fallback when no `project_id`; base =
  engine-runtime-only. [2A] **Implemented cases (`test_depends_scoping.py`, engine interpreter;
  `test_venv_env.py`, bare Python):** `use_env()` switches lock + constraints + `--target` +
  `processed` together and restores on exception; `processed` is per environment (the same file
  installs once *per env*, not once per process); an overlay never triggers the global compile;
  `FileLock` is reentrant in-process (asserted under a timeout so a regression fails instead of
  hanging) while different paths stay independent; a nested `_stop_heartbeat()` leaves the outer
  heartbeat running and stacked operations do not share the download aggregation; progress outside
  any lock writes no sidecar; the overlay is **swapped** (exactly one overlay entry on `sys.path`,
  base untouched, idempotent for the same env) and lands behind an injected `ROCKETRIDE_MOCK` shim;
  one argv builder serves base and overlay (base = overlay minus `--target`); `-r` includes are
  absolutized without backslashes, reach the drift hash, and a missing target is refused by name.
- **Compatibility switch** — `ROCKETRIDE_SERVER_USE_VENV` unset(auto) / `0`(force-off, isolated group
  demoted to a plain group, global-glob) / `1`(force-on). [2A scoping paths; 2B demotion path]
- **Model-server pruning** — a proxied node contributes only wrapper/networking deps, not `ai/**` heavy
  files (§4.8). [2A]
- **Partitioner** (`pipeline.py`) — **increment 1 DONE (19 tests, `test_partition.py`):** flatten
  containers to one level, drop the container node, empty container disappears, no-container pipeline
  returned by identity, members keep their connections; and the validations — nested environments,
  source-in-venv (a plain group is fine), invoke edge across an environment boundary, lane edge into a
  container, and a control edge whose source is a container. *Increment 2 **DONE**, extended by 8.3
  (39 tests, `test_partition_cut.py`):* `scoped=True` cuts isolated groups → per-venv sub-doc +
  `venv`/`venv_server` bridge pair + `channelId`-keyed routing table; one bridge node per environment,
  with a venv→venv lane wired as an edge between two of them (chain, diamond, and a venv feeding two
  venvs); venv-only env-cycle detection over the quotient graph **plus** the collapsed-cycle check for
  an environment entered twice around a base component — and its counterpart, a user cycle with no
  bridge node left to the engine; the scoped-path rejections (non-bridgeable `words`, implied/field
  source in a venv, empty base env, a group named `main`, an environment that emits but is fed by
  nothing, same lane from two producers on one merged boundary); id-collision suffixing; determinism;
  and the §4.6 golden authoring→sub-docs example. [2B]

### 8.2 Test-fixture nodes (purpose-built, lightweight, decoupled from `ai/**`)
Add a pair of **trivial pure-Python nodes** under the node-test tree (e.g.
`nodes/test/fixtures/nodes/vtest_alpha`, `.../vtest_beta` — the extra `nodes/` mirrors the prod
`nodes/src/nodes/` layout so `ProviderIndex` resolves them by the same rule). Each imports **only one tiny leaf package pinned to an
exact, mutually-incompatible version** — e.g. `vtest_alpha` → `tabulate==0.8.10`, `vtest_beta` →
`tabulate==0.9.0`. Pick a package **not used by the SDK or engine runtime** (`requests` would be a bad
choice — the SDK depends on it, so the pin would collide with runtime deps and muddy the test).
**They import nothing from `ai.*`**, so their requirements never touch `packages/ai`
and the conflict is isolated to the venv-scoping mechanism — fast, deterministic, no GPU/torch. These
**replace `torch 2.0/2.1`** as the conflict fixture. [created 2A; used by 8.3]

**Not staged by any build step (gap).** The fixtures are read straight from the source tree by the
automated acceptance — `rocketlib-python/tests/test_scoping_acceptance.py`, which points `ast_deps`
at `nodes/test/fixtures` and is gated on the engine interpreter, on `uv` being bootstrapped, and
skipped when offline — but nothing copies them into `dist/server/nodes`, so no *pipeline* can
reference them without a manual copy and `nodes:test` does not exercise them at all. Staging them
is the prerequisite for the end-to-end acceptance in §8.3; until then, `vtest_alpha` also has
nothing to report — it forwards text unchanged, so the version it actually imported is not
observable from outside the process.

### 8.3 Integration / acceptance

- **Conflict → isolated to its environment (the core proof) — VERIFIED live.** A pipeline with **both**
  `vtest_alpha` + `vtest_beta` under `=1`: the engine **starts normally**, the client connects, and the
  conflict surfaces only in that environment's compile — `venvs/<proj>/main/combined.txt` is written with
  both pins and `uv pip compile` reports them unsatisfiable. The run aborts with the pin names in the
  message; **the server stays up** and cleans the task up. Two properties asserted from artifacts: the
  env's combined file held **9 sources** (the AST-reachable set — the two fixtures, `nodes/webhook` via
  the `dropper` alias, and the `ai/**` modules reached) rather than the 101 node requirement files in the
  installation; and the **global** `cache/combined.txt` contained **no** `tabulate` at all, so the base
  runtime was never touched. [2A]
- **The same pipeline before the `nodes/**` gating (§4.9) — the counter-proof.** With node requirements
  still in the startup glob, the identical setup killed `ai/__init__`'s `depends()` at import: the engine
  **could not start at all**, in every process including the CLI client. This is what motivated the
  gating.
- **Conflict → split across venvs → all good.** The same two nodes in **two separate venvs** → each env
  compiles/install its single pin → the pipeline runs **end-to-end**, both `requests` versions
  coexisting. Assert each overlay's `site-packages` holds the expected version (alpha-env→`0.8.10`,
  beta-env→`0.9.0`) and the other is **absent**. [2B]
- **Only-needed-installed (scoping).** (a) A pipeline using `vtest_alpha` only → its env has
  `tabulate==0.8.10`, **not** the other pin and **not** `whisper`/`faster-whisper`/`torch`. (b) A
  **no-audio** pipeline → `whisper`/`faster-whisper` **absent** from every env's install set; an audio
  pipeline → **present** (the "if whisper isn't needed it isn't installed" check). (c) Assert the
  **requirement-file set processed equals exactly the AST-reachable set**, not the global glob. [2A]
- **Compatibility (`=0` and auto) — VERIFIED live.** Both modes ran a real pipeline end-to-end
  (`venv-detect`, objectId returned) with **no `venvs/` directory created**, every install carrying
  `-c cache/constraints.txt` and **no `--target`**. The switch is fully reversible, measured on the
  startup compile: `=1` → 29 sources, **0** of them node paths; `=0` and auto → **130** sources, **101**
  of them node paths, i.e. exactly the pre-change set. Auto matches legacy **for every pipeline**, not
  merely for this one: nothing produces the isolated-group signal until the partitioner lands (§4.15).
  Still owed for 2B: a pipeline that **does** contain an isolated group must run single-process under
  `=0`, no error — the permanent opt-out (§4.15).
- **A node's own pin beats the base — AUTOMATED at two levels, end-to-end still owed.** The other
  cases prove two nodes conflict *with each other*; this one proves a node gets **its** version
  whatever base holds. `tests/test_scoping_acceptance.py` (engine interpreter, real `uv`; skips
  rather than fails when the index is unreachable):
  - *Mechanism* — `uv pip install --target <empty dir> requests==<version base does not have>`
    plans the install instead of reporting the base copy as satisfying. This is the single property
    everything rests on, and the one that would break silently on a uv upgrade.
  - *Result* — the `vtest_alpha` fixture's requirement set is AST-discovered (exactly its own
    `requirements.txt`, not a glob), compiled, and installed into an overlay under the engine's own
    `venvs/`: the overlay ends up with `tabulate==0.8.10`, the base runtime's copy is unchanged, and
    the applied overlay sits ahead of base on `sys.path`. The overlay is removed afterwards.
  - Note the overlay must live beside the executable: the install passes `-c` **relative** to the
    executable directory (uv splits the value on whitespace, #1256), which cannot be expressed
    across drives — a `tmp_path` on another volume fails with `path is on mount 'C:'`.

  **Still owed — the end-to-end level**, blocked on the `vtest_*` fixtures not being staged into
  `dist/server/nodes` by any build step (and so not exercised by `nodes:test` either). Procedure
  once staged: (1) run a `vtest_beta` pipeline under `=0`, which puts `tabulate==0.9.0` into base
  *by legacy design* rather than by an ad-hoc `uv` call; (2) run a `vtest_alpha`-only pipeline under
  `=1`; (3) assert the node reports `tabulate.__version__ == 0.8.10` and a `__file__` under
  `venvs/<proj>/main/site-packages`, and that base still holds `0.9.0`. Step 3 needs the fixture to
  emit the version and path it imported — today it forwards text unchanged, so nothing observable
  crosses the boundary. [2A]
- **Lifecycle.** Purge, delete-with-nodes, and pipeline-delete reclaim the right `venvs/...` dirs and are
  **blocked while a run is active**. Image lanes cross a venv boundary (all-lane bridge). [2B]
- **Partitioner — VERIFIED on a live engine, not just in unit tests.** A container document driven
  through the SDK (`client.use` + `pipe`, the harness path) produced a task file whose top-level
  components were `[dropper_1, parse_1, response_1, response_outside]` with both containers gone —
  `response_1`, the nested member that used to vanish silently, reached the engine. The
  nested-environment document was rejected **over the wire** with its exact message (`Virtual
  environment "venv_audio" is nested inside "venv_vision"; nested environments are not supported`),
  not a generic failure, so an early validation's cause survives to the client. Two incidental notes:
  the CLI `start` command is unsuitable for an unbound run (its wrapper needs a token for the event
  subscription — use the SDK), and the editor does not write a top-level `source` field the engine
  requires. [2B]
- **Embedding invariant — VERIFIED.** `server:run-engtest` passes (23 cases, 490 assertions,
  including `python::config`): the no-move-binary overlay preserves
  `sys.prefix == exe dir == rootDir`. Under `=1` the same run creates **no `venvs/default`**, which
  is correct and now explained rather than open — its fixture carries no pipeline components, so the
  hook fires with an empty provider set and scoping no-ops (§4.14).
- **State refactor regression — VERIFIED.** `builder server:run-rocketlib-test` 71 passed under the
  engine interpreter (the `EnvContext`/`use_env`, overlay-swap, `FileLock`-reentrancy,
  progress-stack, argv-builder and `-r`-include cases above). `builder nodes:test` matches the
  pre-change baseline in **both** modes: **1980 passed, 49 skipped, 0 failed** with
  `ROCKETRIDE_SERVER_USE_VENV` unset (88.8 s) and with `=1` (636.9 s). The `=1` run created **41 new
  overlay directories**, the unset run created none — the switch still decides everything, and the
  per-environment state refactor changed no outcome. The ×7 wall-clock gap is **not** a warm-vs-cold
  comparison: the harness mints a fresh `project_id` per build, so every `=1` run installs from
  scratch (§4.14).
- **`-r` include handling — VERIFIED against the shipped uv.** Compiling a combined file that
  carried a relative include failed (`failed to read from file …cache\other.txt`); after the
  rewrite the same input resolves both files. A Windows absolute path with backslashes also fails
  (uv reads `C:\x\y.txt` as `C:xy.txt`), which is why the rewrite emits forward slashes.
- **`--target` does not treat base as satisfying — VERIFIED** and now pinned by a test (see the
  pin-beats-base entry above): `uv pip install --target <empty dir> requests==2.32.3` plans the full
  tree although base holds 2.34.2. [2A]

## 9. Critical files (for implementation)
- **Reuse foundation:** `nodes/src/nodes/remote/client/prepare_pipeline.py` (transform → share/generalize);
  `remote/base/IInstance.py` (extract the bridge base; the new `venv`/`venv_server` add all lanes; WS
  `_send`/`_recv` live here = transport not separable in place); `packages/ai/src/ai/modules/remote/`
  (WS transport, reuse over loopback). `REMOTING` in
  `packages/client-python/src/rocketride/types/service.py` / services.json `noremote`.
- `packages/ai/src/ai/modules/task/task_engine.py` — partitioner hook after `_check_pipeline`; spawn N
  children; readiness; teardown; metric/trace fan-in. **Merge-back is not here:** it lives entirely in
  the bridge nodes (`nodes/venv/{server,base,client}/IInstance.py` + `nodes/venv/base/merge.py`, §4.12)
  and the orchestrator never sees it.
- `packages/ai/src/ai/modules/task/task_server.py` — active-task registry (gate purge/GC); `project_id`.
- `packages/ai/src/ai/modules/task/pipeline.py` — `resolve_implied_source` (source-in-venv guard).
- `packages/ai/src/ai/modules/data/data_conn.py` — canonical lane serialization to reuse in the bridge.
- **Testing:** `nodes/test/framework/pipeline.py` (declarative node tests are mini-pipelines → run
  through the same partitioner); `builder nodes:test` (per-node isolation); `server:run-engtest`
  (embedding invariant). New: `nodes/test/fixtures/nodes/vtest_alpha`/`vtest_beta` (the conflict fixture,
  §8.2). **Implemented (2A increment 1):** `.../rocketlib-python/lib/ast_deps.py` (provider→module
  resolution + transitive AST walk) with `test_ast_deps.py` — the §4.8 prototype is now a passing unit
  test (13 tests green).
- `packages/server/engine-lib/rocketlib-python/lib/depends.py` — `ensure_constraints` /
  `_find_requirement_files` / `_get_combined_path` / `_get_constraints_path` / `_get_site_packages` /
  `model_cache_dir` / `FileLock`; the AST **walk** (over the entry-module paths the partitioner
  resolves — `depends.py` itself never reads the `.pipe`) + per-env parameterization + overlay hook
  land here.
- UI: `packages/shared-ui/src/components/canvas/util/graph.ts` (`getProjectComponents`),
  `.../context/FlowGraphContext.tsx` (`onNodeDragStop`, `isValidConnection`),
  `.../node/node-group/NodeGroup.tsx`, `packages/client-typescript/src/client/types/pipeline.ts`,
  `apps/vscode/src/providers/views/Project/ProjectWebview.tsx`.
- Reference only (no edits): `packages/server/engine-lib/engLib/store/stack.cpp`,
  `.../endpoint/endpoint.pipes.cpp` — the `(from,to,lane)`/`(from,to,classType)` semantics the
  partitioner must reproduce. `init.cpp` — embedded-Python init (isolated `PyConfig`; `setPaths`).
  `.../store/services/services.cpp` (parses `protocol`→`logicalType` and `path`→`nodePath`) and
  `.../store/python/python-global.cpp` (imports `serviceDef.nodePath`, not `nodes.<provider>`) — the
  authoritative `provider → entry-module path` mapping the partitioner's AST discovery must reproduce
  (§4.8 Resolution rule); `binder.hpp` — the `Binder::MethodNames` lane list (§4.4).
  `packages/ai/src/ai/common/models/base.py` (`get_model_server_address`, `_ensure_dependencies`,
  `ModelClient`) and `.../common/models/gpu_guard.py` (the `import torch` blocker) — the model-server
  proxy/local branch the AST walk / model-server-aware pruning must model (§4.8 Model-server dimension);
  `.../common/torch/__init__.py` and `ai/common/models/**/requirements_*.txt` — where the heavy `ai/**`
  stack actually lives.
