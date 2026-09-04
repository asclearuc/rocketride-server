# Funnel

Collects several producers on the same lane into one, so their output can leave a virtual
environment.

## Why it exists

A virtual environment's boundary carries **at most one producer per lane**. That is not a
policy — a `write*` call carries no producer identity, so the bridge has nothing to route
by, and the partitioner refuses the shape up front:

```
The boundary out of virtual environment "v1" carries lane "text" from more than one
producer ("a" and "b"); one egress node cannot tell same-lane producers apart.
```

Two parallel nodes in one environment, both producing text, hit this. Neither is wrong,
and neither can be changed to fix it. Without a funnel the only way out is an environment
per producer — which duplicates the whole baseline each time, and is exactly what sharing
an environment was for.

A funnel takes both, and becomes the single producer the boundary requires:

```
┌─ venv ─────────────────────────┐
│  source ─┬─► node A ─┐         │
│          └─► node B ─┴─► funnel│──► downstream
└────────────────────────────────┘
```

## What it does, and what it does not

It **passes writes through** in arrival order. It does not transform, combine, or reorder
them, and it holds nothing back.

It is **not a merge**: two texts arrive downstream as two texts, not as one. There is no
combining policy because none would generalise — two images cannot become one image.

**Producer identity is lost.** Everything leaves on one edge, so downstream cannot tell
which node produced what. Use a funnel when the consumer does not need to know. When it
does, give each producer its own environment instead.

**No parallel execution is gained or lost.** Nodes inside an environment run on one engine
thread either way; the funnel changes what is expressible, not what is concurrent.

## Media lanes

`audio`, `video` and `image` arrive as streams — `BEGIN`, `WRITE` frames, `END` — and the
call carries no stream id. Two producers whose streams overlap would splice into one
unreadable object, and nothing downstream could separate them again.

The funnel refuses that: a second `BEGIN` on a lane whose stream is still open raises,
naming the lane. Without a funnel this shape is rejected at partition time, so the guard
keeps the failure loud instead of trading a rejected pipeline for a corrupt payload.

Producers that emit a whole stream inside one callback — the usual shape — never overlap,
and pass through untouched.

## Lanes

`tags`, `text`, `table`, `json`, `audio`, `video`, `image`, `questions`, `answers`,
`documents` — each passed through unchanged.

That is the bridgeable set in `nodes/venv/base/lanes.py`, less three. `words` is excluded by
the bridge itself: it has no landing method anywhere in `rocketlib`. `classifications` and
`classificationContext` are excluded because **no node in the catalog declares either as a
lane** — a funnel offering them would draw two ports on the canvas that nothing can connect
to. Add them here the moment a node does.

## Configuration

None. The node's whole behaviour is its position in the graph.
