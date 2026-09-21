# venv

The infrastructure nodes that carry pipeline lane data across the boundary of a virtual environment container. The task synthesizes them when a pipeline runs; nobody places one by hand.

## What it does

A component inside a virtual environment container runs in its own engine process — a venv child — against dependencies installed into that environment's overlay. Before the run, the task partitions the pipeline into one flat document per environment (`packages/ai/src/ai/modules/task/pipeline.py`) and splices every boundary with the nodes below; the spawn step (`venv_spawn.py`) then points them at the child over a loopback WebSocket.

The directory ships three services, all marked `internal`, so the service list a client receives — the add-node palette — never offers them:

| Service | Protocol | Runs in | Role |
|---------|----------|---------|------|
| **Virtual Environment** (`services.client.json`) | `venv://` | main | The round-trip bridge. Sends the forward stream to the child and, when the boundary has a paired return, delivers the child's return lanes downstream. |
| **Virtual Environment Server** (`services.server.json`) | `venv_server://` | child | Forward ingress (applies received lanes to the child pipeline) or return egress (ships the child's output back over the same socket). |
| **Virtual Environment Source** (`services.source.json`) | `venv_source_stub://` | child | Head of the child pipeline. Keeps the child resident for the run and mounts `/venv/pipe` on the child's shared web server; produces no objects itself. |

It is the `venv` sibling of `remote`: the same request/response model and list chunking, but to a child the task spawned rather than to another server, and every data lane is driven from one table (`base/lanes.py`) instead of being hand-written per node.

## Lanes

Both bridge nodes pass each lane through unchanged. A boundary carries only the lanes that actually cross it (the `lanes` key under Configuration).

| Lane in | Lane out | Description |
|---------|----------|-------------|
| `tags` | `tags` | Tag stream |
| `text` | `text` | Text |
| `table` | `table` | Tables |
| `json` | `json` | JSON payloads |
| `audio` | `audio` | `action` and `mime` in the frame header, the buffer as raw bytes |
| `video` | `video` | As `audio` |
| `image` | `image` | As `audio` |
| `questions` | `questions` | Questions |
| `answers` | `answers` | Answers |
| `classifications` | `classifications` | Classifications |
| `classificationContext` | `classificationContext` | Classification context |
| `documents` | `documents` | Documents |
| `_source` | all of the above | The child's source stub; the ingress bridge reads its lanes from it |

`words` is the one engine lane that does not cross: `rocketlib` has no `writeWords` landing method, so it is an explicit `LaneNotBridgeable` entry in the table rather than a silent gap.

## Configuration

No user-facing fields. The partitioner writes each bridge node's config (`_bridge_config` in `pipeline.py`) and the spawn step completes it:

| Key | Set by | Meaning |
|-----|--------|---------|
| `channelId` | partitioner | The boundary this node carries — one socket per forward channel |
| `sourceEnv`, `targetEnv` | partitioner | Direction: `main` on one side, the container's environment on the other |
| `lanes` | partitioner | The lanes crossing this boundary |
| `returnChannelId`, `returnLanes` | partitioner | Only on a round-trip `venv` node whose forward channel has a paired return |
| `urlProcess` | spawn | `ws://127.0.0.1:<child port>/venv/pipe?channel=<channelId>`, plus `&return=<returnChannelId>` when paired. The `venv` node refuses to start without it. |

The bearer token is not in the config. It travels in the `ROCKETRIDE_VENV_TOKEN` environment variable, never argv or disk, and the child's `/venv/pipe` route verifies it before accepting.

## Notes

### Transport

- One WebSocket per forward channel, dialed once in `beginInstance` and held for the node's lifetime.
- The frame ceiling is 250 MB on both ends (`MAX_FRAME_SIZE`), matching the child's uvicorn limit — the `websockets` 1 MiB default let an image cross to the child and then die on the way back. One `write*` is still one frame: media is not chunked. List payloads are split into messages under ~0.98 MiB.
- No keepalive ping. The child cannot answer one while it is inside a call, so the library's 20 s default killed the bridge of any node slower than that; liveness is the process.

### Readiness

The source reports `Venv child ready - listening for bridged lane data` only after `/venv/pipe` is mounted, and the spawning task matches that exact text (`VENV_READY_STATUS` in `venv_spawn.py`). A TCP handshake proves nothing here — the shared server binds at bootstrap, before the route exists. Reword one side and the other must change in the same commit.

### Merge-back

A `response` node inside a container writes the child's entry, which no client reads. `base/merge.py` folds it into main's: dicts merge deep, lists concatenate with main's items first, scalars are child-wins.

The design, including the partitioning rules and the process model, is `packages/server/design/virtual-environments.md`.
