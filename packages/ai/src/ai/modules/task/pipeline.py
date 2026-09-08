"""Pipeline utility functions for source resolution, substitution and partitioning."""

import copy
import json
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, Any, Iterator, List, Optional, Tuple

# The admissibility gate below is the security boundary of the forced-requirements field, and it
# rests on a standard someone else maintains rather than on a regex we wrote. Declared in
# ai/requirements.txt for this. The engine's own requirement-line handling stays hand-rolled --
# it reads uv's compiled output and the tree's own override files, neither of which is text a
# tenant typed (§4.7.1, OQ-19).
from packaging.requirements import InvalidRequirement, Requirement

# Only environment variables with this prefix are permitted to resolve in pipelines.
# All other env vars are blocked to prevent exfiltration of secrets via ${VAR} expansion.
ALLOWED_ENV_PREFIX = 'ROCKETRIDE_'


def resolve_pipeline_env(pipeline: Dict[str, Any], env: Dict[str, str]) -> Dict[str, Any]:
    """Replace ``${KEY}`` placeholders in a pipeline dict with environment values.

    Only variables whose names start with :data:`ALLOWED_ENV_PREFIX` are
    resolved.  All other references are replaced with ``<REDACTED>`` to
    prevent secret exfiltration.

    Args:
        pipeline: Pipeline configuration dictionary.
        env: Merged environment dict (e.g. .env → org → team → user secrets).

    Returns:
        New dictionary with resolved environment variables.
    """
    pipeline_str = json.dumps(pipeline)

    def replacer(match: re.Match) -> str:
        env_var = match.group(1)
        if env_var.startswith(ALLOWED_ENV_PREFIX):
            value = env.get(env_var, match.group(0))
            if value == match.group(0):
                return value  # placeholder not found — keep as-is
            return json.dumps(value)[1:-1]  # escape but strip outer quotes
        return '<REDACTED>'

    resolved_str = re.sub(r'\$\{([^}]+)\}', replacer, pipeline_str)
    return json.loads(resolved_str)


# ---------------------------------------------------------------------------
# Containers (groups and virtual environments)
# ---------------------------------------------------------------------------


def _members(component: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Components nested inside ``component``; empty when it is not a container."""
    pipeline = component.get('config', {}).get('pipeline') or {}
    members = pipeline.get('components')
    return members if isinstance(members, list) else []


def _environment(component: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The component's virtual-environment block, if it declares one."""
    environment = component.get('config', {}).get('environment')
    return environment if isinstance(environment, dict) else None


def is_container(component: Dict[str, Any]) -> bool:
    """Whether ``component`` holds other components rather than processing data.

    A container declares an environment, holds members, or both: an empty virtual
    environment has no members yet, and a plain organizational group has no
    environment.
    """
    return bool(_members(component)) or _environment(component) is not None


def is_isolated(component: Dict[str, Any]) -> bool:
    """Whether ``component`` is a container whose members get their own environment."""
    environment = _environment(component)
    return bool(environment and environment.get('isolated'))


def walk_components(
    components: List[Dict[str, Any]], parent: Optional[Dict[str, Any]] = None
) -> Iterator[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]]:
    """Yield ``(component, container)`` for every component at every depth.

    ``container`` is the component's immediate container, or ``None`` at the top
    level. Depth-first, containers before their members.
    """
    for component in components:
        yield component, parent
        for nested in walk_components(_members(component), component):
            yield nested


# ---------------------------------------------------------------------------
# Partitioning
# ---------------------------------------------------------------------------

# The base environment: components that are not inside any isolated group run here.
MAIN_ENV = 'main'

# Data lanes the venv bridge cannot carry. Kept in sync by hand with the single
# source of truth, ``nodes/src/nodes/venv/base/lanes.py`` (``LaneNotBridgeable``);
# the ``ai`` package cannot import ``nodes.*`` under a bare pytest run, so a boundary
# edge on one of these is rejected at cut time rather than at runtime.
NON_BRIDGEABLE_LANES = frozenset({'words'})

# The source of every venv sub-document. It is a real registered resident source
# (``nodes/venv/source``): it makes the child ingress bridge reachable from the child's
# source AND hosts the child's ``/venv/pipe`` WebServer, blocking for the run so the
# child engine stays alive while lane data arrives over the wire.
VENV_SOURCE_STUB_PROVIDER = 'venv_source_stub'
VENV_SOURCE_STUB_ID = 'venv_source_stub'


@dataclass
class PartitionResult:
    """The output of the cut: one flat sub-document per environment plus routing.

    ``environments`` maps an env id to a runnable engine document (``'main'`` first,
    then each isolated group in document order). ``routing`` lists one entry per
    boundary channel (keyed by ``channelId``), a record of how each child was wired --
    inter-venv traffic is routed by main's engine graph, not by the orchestrator.
    ``groups`` carries each venv's original ``config.environment`` block (its display
    name; the container node itself is stripped from the documents) for step-7/8
    logging and metrics.
    """

    environments: 'OrderedDict[str, Dict[str, Any]]'
    routing: List[Dict[str, Any]]
    groups: Dict[str, Dict[str, Any]] = field(default_factory=dict)


def has_isolated_group(pipeline: Dict[str, Any]) -> bool:
    """Whether the pipeline contains at least one isolated (venv) container."""
    return any(is_isolated(component) for component, _ in walk_components(pipeline.get('components', [])))


def _env_of(components: List[Dict[str, Any]]) -> Dict[str, str]:
    """Map every component id to the environment it runs in.

    A component's environment is its **nearest isolated-container ancestor**, or
    :data:`MAIN_ENV` when it has none — transitive, not just the immediate parent, so a
    member of a plain group nested inside a venv is correctly attributed to that venv.
    A container is recorded under the environment it *sits in*; only leaves are read in
    practice.
    """
    env_of: Dict[str, str] = {}

    def walk(items: List[Dict[str, Any]], env: str) -> None:
        for component in items:
            env_of[component.get('id')] = env
            child_env = component.get('id') if is_isolated(component) else env
            walk(_members(component), child_env)

    walk(components, MAIN_ENV)
    return env_of


def _forced_text(component: Dict[str, Any]) -> str:
    """The container's forced-requirements text, or ``''`` when it has none."""
    environment = _environment(component) or {}
    forced = environment.get('forced')
    return forced.strip() if isinstance(forced, str) else ''


def _container_label(component: Dict[str, Any]) -> str:
    """How to name a container in a refusal: its display name, falling back to its id."""
    environment = _environment(component) or {}
    return environment.get('name') or component.get('id') or '<unnamed>'


def _validate_forced(component: Dict[str, Any], scoped: bool) -> None:
    """Refuse a container whose forced requirements cannot be honoured as written.

    Three refusals, and the first two are about the field having no environment to act on.
    **Isolation is checked before scoping** even though either can be the reason: a container that
    is not isolated is not an environment at all, and "tick the box" is an answer the user can act
    on, while "this run is not scoped" would send them looking at a server switch. Under ``auto``
    an un-isolated lone container produces *both* conditions, so the order decides which message
    they read.

    The third is admissibility, and it is the security boundary. ``-r`` would make a
    tenant-supplied string read a path off the server; an index flag is worse than it looks,
    because the compile already runs ``--index-strategy unsafe-best-match`` and ``--emit-index-url``
    writes the index into ``constraints.txt`` where later installs read it -- that is dependency
    confusion. Refusing here rather than at install time is what keeps either from reaching uv at
    all.

    Raises:
        ValueError: Named, carrying the container and (for a bad line) the line itself, so the
            canvas can show it. One more of the plain ``ValueError`` refusals this module already
            raises -- an exception hierarchy would be a second mechanism for one audience.
    """
    text = _forced_text(component)
    if not text:
        return

    label = _container_label(component)
    if not is_isolated(component):
        raise ValueError(
            f'Container "{label}" has forced Python requirements but is not isolated; '
            'they would not be applied. Enable isolated dependencies or clear the field'
        )
    if not scoped:
        raise ValueError(
            f'Container "{label}" has forced Python requirements but this run is not scoped, '
            'so no virtual environment is created and they would not be applied. '
            'Clear the field or enable virtual environments'
        )

    for raw in text.splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement as exc:
            raise ValueError(
                f'Container "{label}" has an inadmissible forced requirement: "{line}". '
                'Only PEP 508 requirement lines, comments and blank lines are allowed '
                '(no -r, -c, -e, index flags, paths or URLs)'
            ) from exc
        # The one shape PEP 508 accepts and this field must not: a direct reference names a
        # distribution to fetch from an arbitrary URL, which is the "forced never adds" boundary
        # in reverse.
        if requirement.url is not None:
            raise ValueError(
                f'Container "{label}" has an inadmissible forced requirement: "{line}". '
                'A direct URL reference is not allowed; forced requirements select versions '
                'of packages the environment already installs'
            )


def _validate_containers(pipeline: Dict[str, Any], source: Optional[str], scoped: bool = False) -> None:
    """Reject documents whose containers cannot be executed as written.

    These are structural errors the editor should have prevented, so they fail the
    run with a named cause rather than being silently normalised into something the
    author did not write. Enforced on both the legacy flatten path and the cut.
    """
    components = pipeline.get('components', [])
    env_of = _env_of(components)
    container_ids = set()

    for component, _ in walk_components(components):
        if is_container(component):
            container_ids.add(component.get('id'))
            _validate_forced(component, scoped)
        # An isolated group whose environment is not ``main`` sits inside another venv
        # (directly or through a plain group) — nested environments are not supported.
        if is_isolated(component) and env_of.get(component.get('id'), MAIN_ENV) != MAIN_ENV:
            raise ValueError(
                f'Virtual environment "{component.get("id")}" is nested inside "{env_of[component.get("id")]}"; nested environments are not supported'
            )

    if source is not None and env_of.get(source, MAIN_ENV) != MAIN_ENV:
        raise ValueError(
            f'Source component "{source}" is inside virtual environment "{env_of[source]}"; the source must stay outside'
        )

    for component, _ in walk_components(components):
        target_env = env_of.get(component.get('id'), MAIN_ENV)
        # Invoke/control edges cannot cross an environment boundary: the callee runs
        # in another process, and the call is synchronous with no lane to carry it.
        for control in component.get('control', []) or []:
            source_ref = control.get('from')
            # A container has no lanes/behaviour, so nothing can invoke through one; it
            # dangles the moment the container is flattened away.
            if source_ref in container_ids:
                raise ValueError(
                    f'Invoke connection from container "{source_ref}" to "{component.get("id")}", which produces no data'
                )
            if env_of.get(source_ref, MAIN_ENV) != target_env:
                raise ValueError(
                    f'Invoke connection from "{source_ref}" to "{component.get("id")}" crosses a virtual environment boundary'
                )
        # A container has no lanes, so nothing can connect to one.
        for lane in component.get('input', []) or []:
            if lane.get('from') in container_ids:
                raise ValueError(
                    f'Component "{component.get("id")}" takes input from container "{lane.get("from")}", which produces no data'
                )


def _flatten_members(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Lift every container's members to the top level, keeping author order."""
    flat: List[Dict[str, Any]] = []
    for component in items:
        if not is_container(component):
            flat.append(component)
            continue
        # Members take the container's place; the container itself has no runtime
        # behaviour and disappears with it.
        flat.extend(_flatten_members(_members(component)))
    return flat


def _prune_unreachable(components: List[Dict[str, Any]], source: Optional[str]) -> List[Dict[str, Any]]:
    """Drop everything the run's source cannot reach, the way the engine already does.

    **This exists to fix an ordering problem, not to add a rule.** The engine builds its
    pipe stack by walking forward from the source and simply never loads what it does not
    reach -- ``IServiceEndpoint::generatePipelineStack`` (``store/stack.cpp``) walks data
    edges in ``walkComponents`` and invoke edges in ``walkControl``, and
    ``buildConnections`` skips a component with the comment *"we were not included, nobody
    referenced us"*. A dead branch is therefore free in an ordinary pipeline.

    The cut, though, runs **before** any engine sees the document: it splits one pipeline
    into one document per environment, and each engine only prunes what it is handed. So
    without this step the boundary analysis reasons about branches the engine would have
    discarded -- and a virtual environment on such a branch produced
    *"produces output but nothing is routed into it"*, a refusal the same graph never
    earns outside a container. Pruning first makes the container path agree with the
    plain one instead of being stricter than it.

    **Both edge kinds, or the two implementations diverge in silence.** A node reached
    only by an invoke edge (an agent's tool, which commonly has no data input at all) is
    kept by ``walkControl``. Walking data edges alone would prune it here, delete the
    environment holding it, and leave the agent toolless with nothing on screen to say so.

    :param components: The document's components, containers still nested.
    :param source: The run's source component id. Falsy disables pruning entirely --
        without a root there is nothing to be reachable *from*, and dropping the whole
        document would be a spectacular way to be wrong.
    :returns: ``components`` filtered to the source plus everything it reaches, with the
        original nesting preserved; a container emptied by the walk is dropped with it.
    """
    if not source:
        return components

    leaves = _flatten_members(components)
    by_id = {leaf.get('id'): leaf for leaf in leaves}
    if source not in by_id:
        # The source is not a leaf of this document (an unknown id, or it sits in a
        # container). Both are somebody else's rejection -- _validate_source_placement
        # and the engine's own source check -- and pruning against a root we cannot see
        # would turn their named error into a silently empty pipeline.
        return components

    # Forward adjacency over BOTH edge kinds: producer -> consumers.
    consumers: Dict[str, List[str]] = {}
    for leaf in leaves:
        consumer = leaf.get('id')
        for edge in (leaf.get('input') or []) + (leaf.get('control') or []):
            producer = edge.get('from')
            if producer:
                consumers.setdefault(producer, []).append(consumer)

    reached = {source}
    pending = [source]
    while pending:
        for consumer in consumers.get(pending.pop(), []):
            if consumer not in reached:
                reached.add(consumer)
                pending.append(consumer)

    def keep(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        surviving = []
        for component in items:
            if not is_container(component):
                if component.get('id') in reached:
                    surviving.append(component)
                continue
            members = keep(_members(component))
            if not members:
                # Nothing live left inside. An empty container has no runtime behaviour,
                # and _assemble_documents already declines to emit a document for one.
                continue
            component['config']['pipeline']['components'] = members
            surviving.append(component)
        return surviving

    return keep(components)


def _sanitize_id_part(text: Any) -> str:
    """Reduce an id fragment to a component-id-safe token (no ``->``/``/``/``:``)."""
    return re.sub(r'[^0-9A-Za-z_]', '_', str(text))


def _unique_id(base: str, taken: set) -> str:
    """A deterministic id derived from ``base`` that is unique within ``taken``."""
    candidate = base
    suffix = 2
    while candidate in taken:
        candidate = f'{base}-{suffix}'
        suffix += 1
    taken.add(candidate)
    return candidate


def _detect_cycle(adjacency: Dict[str, List[str]]) -> Optional[List[str]]:
    """Return one directed cycle in ``adjacency`` (as a node path), or ``None``."""
    WHITE, GREY, BLACK = 0, 1, 2
    colour: Dict[str, int] = {node: WHITE for node in adjacency}
    stack: List[str] = []

    def visit(node: str) -> Optional[List[str]]:
        colour[node] = GREY
        stack.append(node)
        for nxt in adjacency.get(node, []):
            if colour.get(nxt, WHITE) == GREY:
                return stack[stack.index(nxt) :] + [nxt]
            if colour.get(nxt, WHITE) == WHITE:
                found = visit(nxt)
                if found is not None:
                    return found
        stack.pop()
        colour[node] = BLACK
        return None

    for node in adjacency:
        if colour[node] == WHITE:
            found = visit(node)
            if found is not None:
                return found
    return None


def _isolated_env_blocks(components: List[Dict[str, Any]]) -> 'OrderedDict[str, Dict[str, Any]]':
    """Each isolated group's ``config.environment`` block, keyed by id, in document order."""
    blocks: 'OrderedDict[str, Dict[str, Any]]' = OrderedDict()
    for component, _ in walk_components(components):
        if is_isolated(component):
            blocks[component.get('id')] = _environment(component)
    return blocks


def _bucket_leaves_by_env(
    components: List[Dict[str, Any]], env_of: Dict[str, str]
) -> 'OrderedDict[str, List[Dict[str, Any]]]':
    """Group every leaf under its environment, in document order; containers dissolve.

    This single rule subsumes non-isolated flattening (§4.3): a plain group inside a venv
    lands its leaves in that venv, and a venv nested in a plain group becomes its own bucket.
    """
    buckets: 'OrderedDict[str, List[Dict[str, Any]]]' = OrderedDict()
    for component, _ in walk_components(components):
        if is_container(component):
            continue
        buckets.setdefault(env_of.get(component.get('id'), MAIN_ENV), []).append(component)
    return buckets


def _validate_source_placement(
    components: List[Dict[str, Any]],
    env_of: Dict[str, str],
    source_field: Optional[str],
    buckets: 'OrderedDict[str, List[Dict[str, Any]]]',
) -> None:
    """The run's source/root must stay in main, and main must not be left empty.

    Guards the implied source (a ``Source``-mode component) and the document's ``source``
    field — the explicit ``source`` param is already checked in ``_validate_containers`` —
    and rejects a base environment emptied out while an isolated group exists.
    """
    for component, _ in walk_components(components):
        if is_container(component):
            continue
        if (
            component.get('config', {}).get('mode') == 'Source'
            and env_of.get(component.get('id'), MAIN_ENV) != MAIN_ENV
        ):
            raise ValueError(
                f'Source component "{component.get("id")}" is inside virtual environment "{env_of[component.get("id")]}"; the source must stay outside'
            )
    if source_field is not None and env_of.get(source_field, MAIN_ENV) != MAIN_ENV:
        raise ValueError(
            f'Source component "{source_field}" is inside virtual environment "{env_of[source_field]}"; the source must stay outside'
        )
    if any(env != MAIN_ENV for env in buckets) and MAIN_ENV not in buckets:
        raise ValueError('The pipeline has no components in the base environment; the source/root must stay in main')


FORWARD, RETURN = 'forward', 'return'


def _collect_channels(
    components: List[Dict[str, Any]], env_of: Dict[str, str]
) -> Tuple[
    'OrderedDict[Tuple[str, str], Dict[str, Any]]',
    List[Tuple[Dict[str, Any], Tuple[str, str]]],
    Dict[str, List[str]],
]:
    """Classify boundary edges into one **forward** and one **return** channel per venv.

    A single bridge node carries a whole boundary (all its lanes) over one socket -- the child
    runs one pipe stack, so a venv boundary is one connection, and the frame ``lane`` header
    demuxes the lanes to their consumers (§1.2, Arch-1). Channels are therefore keyed
    ``(direction, env)``, not by environment pair: everything entering a venv shares its forward
    channel and everything leaving it shares its return channel, whatever the other end is. That
    is what lets a venv feed another venv (§4.6, graph serialization) -- such an edge is cut
    **twice**, once at each boundary it crosses, and reaches its consumer as an ordinary
    main-graph edge between the two bridge nodes.

    ``sourceEnv``/``targetEnv`` keep naming the **socket peers** (main and the child), never the
    data's true origin: the child selects a ``venv_server`` node's role from ``sourceEnv ==
    'main'``, and the spawn injection picks the child env the same way.

    Each lane in a channel has exactly **one** producer -- a node's ``write*`` carries no producer
    identity, so two same-lane producers on one boundary cannot be told apart downstream. On the
    forward side the comparison is on the *main-side* identity (the producer itself when it lives
    in main, otherwise its environment, since a whole venv arrives through one bridge node); on
    the return side it is on the producing node. Multiple consumers of a lane are fine.

    Returns the channels in first-encounter order, the ``(edge, key)`` pairs to repoint once
    ingress ids exist (exactly one per edge, keyed by the **consumer-side** channel), and the
    venv-only quotient adjacency for cycle detection. An unknown ``from`` stays a dangling edge;
    a non-bridgeable lane is rejected.
    """

    def main_side_identity(node: str) -> str:
        """What a forward boundary can tell apart: a main node, or an entire venv."""
        env = env_of[node]
        return node if env == MAIN_ENV else env

    channels: 'OrderedDict[Tuple[str, str], Dict[str, Any]]' = OrderedDict()
    edge_rewrites: List[Tuple[Dict[str, Any], Tuple[str, str]]] = []
    quotient: Dict[str, List[str]] = {}

    def channel_for(direction: str, env: str) -> Dict[str, Any]:
        key = (direction, env)
        channel = channels.get(key)
        if channel is None:
            source_env, target_env = (MAIN_ENV, env) if direction == FORWARD else (env, MAIN_ENV)
            channel = {'sourceEnv': source_env, 'targetEnv': target_env, 'lanes': OrderedDict()}
            channels[key] = channel
        return channel

    def register(channel: Dict[str, Any], lane: str, producer: str, consumer: str, on_conflict) -> None:
        lane_info = channel['lanes'].get(lane)
        if lane_info is None:
            channel['lanes'][lane] = lane_info = {'producer': producer, 'consumers': []}
        else:
            on_conflict(lane_info['producer'], producer)
        if consumer not in lane_info['consumers']:
            lane_info['consumers'].append(consumer)

    for component, _ in walk_components(components):
        if is_container(component):
            continue
        consumer_id = component.get('id')
        target_env = env_of.get(consumer_id, MAIN_ENV)
        for edge in component.get('input', []) or []:
            producer = edge.get('from')
            if producer not in env_of:
                continue  # unknown reference — leave the dangling edge as today
            source_env = env_of[producer]
            if source_env == target_env:
                continue  # not a boundary
            lane = edge.get('lane')
            if lane in NON_BRIDGEABLE_LANES:
                raise ValueError(
                    f'Boundary edge on lane "{lane}" from "{producer}" to "{consumer_id}" crosses a virtual environment boundary, but "{lane}" is not bridgeable'
                )

            # The producer's side first, so that a shape which conflicts on both boundaries
            # reports the cause nearest the data's origin.
            if source_env != MAIN_ENV:

                def out_conflict(held: str, incoming: str, env: str = source_env, lane: str = lane) -> None:
                    if held != incoming:
                        raise ValueError(
                            f'The boundary out of virtual environment "{env}" carries lane "{lane}" '
                            f'from more than one producer ("{held}" and "{incoming}"); one egress '
                            'node cannot tell same-lane producers apart. Merge or route them inside '
                            'the environment instead.'
                        )

                register(channel_for(RETURN, source_env), lane, producer, consumer_id, out_conflict)

            if target_env != MAIN_ENV:

                def in_conflict(held: str, incoming: str, env: str = target_env, lane: str = lane) -> None:
                    if main_side_identity(held) != main_side_identity(incoming):
                        raise ValueError(
                            f'The boundary into virtual environment "{env}" carries lane "{lane}" '
                            f'from more than one producer ("{held}" and "{incoming}"); one bridge '
                            'node cannot tell same-lane producers apart. Merge or route them inside '
                            'the environment instead.'
                        )

                register(channel_for(FORWARD, target_env), lane, producer, consumer_id, in_conflict)

            if source_env != MAIN_ENV and target_env != MAIN_ENV:
                quotient.setdefault(source_env, [])
                quotient.setdefault(target_env, [])
                if target_env not in quotient[source_env]:
                    quotient[source_env].append(target_env)

            # One rewrite per edge: the consumer reads from its own side of the boundary.
            edge_rewrites.append((edge, (FORWARD, target_env) if target_env != MAIN_ENV else (RETURN, source_env)))
    return channels, edge_rewrites, quotient


def _reject_env_cycles(quotient: Dict[str, List[str]]) -> None:
    """Reject a directed cycle in the venv-only env-quotient graph.

    Main is excluded: routing through main is transparent, so only venv↔venv dependency cycles
    can deadlock, while main-terminated chains (``main→V1→V2→main``) are legal and are exactly
    what step 8.3 enables. The adjacency comes from ``_collect_channels`` rather than from the
    channels themselves -- under ``(direction, env)`` keying every channel has main on one side,
    so the venv→venv relation lives inside the channels' lanes, not in their keys.
    """
    cycle = _detect_cycle(quotient)
    if cycle is not None:
        raise ValueError('Cross-environment cycle between virtual environments: ' + ' -> '.join(cycle))


def _assign_channel_ids(
    channels: 'OrderedDict[Tuple[str, str], Dict[str, Any]]',
    buckets: 'OrderedDict[str, List[Dict[str, Any]]]',
    venv_envs: List[str],
) -> Dict[str, str]:
    """Assign each channel its ``channelId`` and bridge node ids; return per-child stub ids.

    One forward and one return channel per venv, so ``channelId`` stays ``{sourceEnv}->{targetEnv}``
    -- i.e. ``main->{env}`` and ``{env}->main``, naming the socket peers. Node ids are unique
    **within their own document** (main and each child are separate docs); the ``channelId`` is the
    global routing key and must not collide (only possible if an env id itself contains ``->``).
    The channel *key* is ``(direction, env)`` and must not be unpacked here -- the ids come from
    the channel's own ``sourceEnv``/``targetEnv``.
    """
    taken: Dict[str, set] = {env: {leaf.get('id') for leaf in leaves} for env, leaves in buckets.items()}
    stub_ids = {env: _unique_id(VENV_SOURCE_STUB_ID, taken[env]) for env in venv_envs}

    seen_channel_ids: set = set()
    for channel in channels.values():
        source_env, target_env = channel['sourceEnv'], channel['targetEnv']
        channel_id = f'{source_env}->{target_env}'
        if channel_id in seen_channel_ids:
            raise ValueError(f'Duplicate channelId "{channel_id}"; environment ids must not contain "->"')
        seen_channel_ids.add(channel_id)
        channel['channelId'] = channel_id
        parts = '--'.join(_sanitize_id_part(p) for p in (source_env, target_env))
        channel['egressNode'] = _unique_id(f'venv_egress--{parts}', taken[source_env])
        channel['ingressNode'] = _unique_id(f'venv_ingress--{parts}', taken[target_env])
    return stub_ids


def _bridge_config(channel: Dict[str, Any]) -> Dict[str, Any]:
    """The bridge node's config; child URL/token are filled at spawn (step 7).

    One channel carries a whole boundary (all its ``lanes``) over one socket. A forward channel
    (``main -> env``) that is paired with a return channel additionally carries the return
    channel's id and lanes: its main-side node is a **round-trip** ``venv`` node that both sends
    the forward stream and delivers the return downstream, so the spawn injection dials one
    socket carrying both directions (``?channel=..&return=..``).
    """
    config = {
        'channelId': channel['channelId'],
        'sourceEnv': channel['sourceEnv'],
        'targetEnv': channel['targetEnv'],
        'lanes': list(channel['lanes'].keys()),
    }
    ret = channel.get('returnChannel')
    if ret is not None:
        config['returnChannelId'] = ret['channelId']
        config['returnLanes'] = list(ret['lanes'].keys())
    return config


def _pair_boundaries(channels: 'OrderedDict[Tuple[str, str], Dict[str, Any]]') -> None:
    """Pair each venv's forward and return channel for the round-trip bridge model.

    The venv boundary is a request/response splice of one object: the object never forks, so the
    return must ride back over the **forward** socket and re-enter main through the **same** node
    that sent it (the engine allows one open object per pipe stack, entered at the root -- a
    separate async re-injection cannot open the already-open object). Channels are keyed
    ``(direction, env)``, so each venv has at most one forward and one return channel by
    construction, each carrying all its lanes over one socket (§1.2, Arch-1).

    Mutates the channels in place: sets ``returnChannel`` on the paired forward channel, and
    points the return channel's ``deliverNode``/``ingressNode`` at the round-trip node (the
    forward egress) that delivers it.
    """
    forward_by_env = {env: channel for (direction, env), channel in channels.items() if direction == FORWARD}
    return_by_env = {env: channel for (direction, env), channel in channels.items() if direction == RETURN}

    for env, ret in return_by_env.items():
        forward = forward_by_env.get(env)
        if forward is None:
            # Nothing dials the child, so its egress would have no socket to ship the return on.
            raise ValueError(f'Virtual environment "{env}" produces output but nothing is routed into it')
        forward['returnChannel'] = ret
        # The round-trip node (the forward egress) both sends into the venv and delivers its
        # return downstream, so return consumers read from it and its routing entry points there
        # rather than at a now-absent main ingress node.
        ret['deliverNode'] = forward['egressNode']
        ret['ingressNode'] = forward['egressNode']


def _resolve_forward_sources(channels: 'OrderedDict[Tuple[str, str], Dict[str, Any]]', env_of: Dict[str, str]) -> None:
    """Record, per forward lane, the **main-side** node its bridge reads from.

    A lane produced in main is read from the producer itself; a lane produced inside another venv
    is read from *that* venv's bridge node, which is what delivers it into main. That single
    substitution is the whole of graph serialization (§4.6): the venv quotient becomes ordinary
    edges between bridge nodes.

    Must run **after** ``_pair_boundaries``: a venv that emits into another venv but is itself fed
    by nothing has no forward channel, and pairing is what turns that into a named error rather
    than the ``KeyError`` this lookup would raise.
    """
    for (direction, _env), channel in channels.items():
        if direction != FORWARD:
            continue
        for lane_info in channel['lanes'].values():
            producer_env = env_of[lane_info['producer']]
            lane_info['sourceNode'] = (
                lane_info['producer'] if producer_env == MAIN_ENV else channels[(FORWARD, producer_env)]['egressNode']
            )


def _synthesize_bridges(
    channels: 'OrderedDict[Tuple[str, str], Dict[str, Any]]',
    buckets: 'OrderedDict[str, List[Dict[str, Any]]]',
    stub_ids: Dict[str, str],
) -> Dict[str, List[Dict[str, Any]]]:
    """Build the bridge nodes for each channel, grouped by the env whose document holds them.

    The boundary is spliced with the ``remote`` request/response model, over one socket per
    forward channel:

    - A **forward channel** yields a round-trip ``venv`` node in main (it reads each lane's
      main-side source -- a main producer, or another venv's bridge node -- sends the forward
      stream, and, when a return is paired, delivers that return downstream to the repointed
      consumers) and a ``venv_server`` ingress in the child (reads the child stub, applies the
      forward stream locally).
    - A **return channel** yields only a ``venv_server`` egress in the child (reads the venv
      producers, ships the return back over the **same** socket its paired forward node dialed).
      Main has no separate ingress node: the return re-enters through the round-trip node, so the
      object is never re-opened (see ``_pair_boundaries``).
    """
    bridge_nodes: Dict[str, List[Dict[str, Any]]] = {env: [] for env in buckets}
    for channel in channels.values():
        src, dst = channel['sourceEnv'], channel['targetEnv']
        if src == MAIN_ENV:
            # Forward channel: round-trip node in main + ingress in the child. Its inputs are the
            # lanes' main-side sources, so a venv-produced lane arrives from that venv's bridge.
            bridge_nodes[MAIN_ENV].append(
                {
                    'id': channel['egressNode'],
                    'provider': 'venv',
                    'input': [{'lane': lane, 'from': info['sourceNode']} for lane, info in channel['lanes'].items()],
                    'config': _bridge_config(channel),
                }
            )
            bridge_nodes[dst].append(
                {
                    'id': channel['ingressNode'],
                    'provider': 'venv_server',
                    'input': [{'lane': lane, 'from': stub_ids[dst]} for lane in channel['lanes']],
                    'config': _bridge_config(channel),
                }
            )
        else:
            # Return channel: egress in the child only; main delivery is the paired
            # round-trip node (no separate main ingress). One input edge per lane, each from
            # that lane's single in-venv producer.
            bridge_nodes[src].append(
                {
                    'id': channel['egressNode'],
                    'provider': 'venv_server',
                    'input': [{'lane': lane, 'from': info['producer']} for lane, info in channel['lanes'].items()],
                    'config': _bridge_config(channel),
                }
            )
    return bridge_nodes


def _assemble_documents(
    work: Dict[str, Any],
    buckets: 'OrderedDict[str, List[Dict[str, Any]]]',
    bridge_nodes: Dict[str, List[Dict[str, Any]]],
    env_blocks: 'OrderedDict[str, Dict[str, Any]]',
    stub_ids: Dict[str, str],
) -> 'OrderedDict[str, Dict[str, Any]]':
    """Assemble one flat sub-document per environment: main first, then each venv.

    Non-component top-level fields carry over (§4.9 keying needs ``project_id``); main keeps
    its original ``source`` field, each child's source is its synthesized stub.
    """

    def base_fields(reference: Dict[str, Any]) -> Dict[str, Any]:
        return {key: copy.deepcopy(value) for key, value in reference.items() if key != 'components'}

    environments: 'OrderedDict[str, Dict[str, Any]]' = OrderedDict()
    main_doc = base_fields(work)
    main_doc['components'] = list(buckets.get(MAIN_ENV, [])) + bridge_nodes[MAIN_ENV]
    environments[MAIN_ENV] = main_doc

    for env in env_blocks:
        if env not in buckets:
            continue  # an empty venv contributes no document
        stub = {'id': stub_ids[env], 'provider': VENV_SOURCE_STUB_PROVIDER, 'config': {}}
        child_doc = base_fields(work)
        child_doc['source'] = stub_ids[env]
        child_doc['components'] = [stub] + bridge_nodes[env] + list(buckets[env])
        environments[env] = child_doc
    return environments


def _reject_main_cycles(main_components: List[Dict[str, Any]], bridge_env: Dict[str, str]) -> None:
    """Reject a cycle that the cut itself creates in main's graph.

    A venv collapses to exactly **one** bridge node in main (§1.2, Arch-1), so entering the same
    environment twice around a base-environment node folds into ``MV -> m -> MV`` -- a cycle,
    even though the authored document is a DAG and runs fine flattened under ``=0``. The author
    never wrote the node names in that cycle, so the engine's own check (which names a lifecycle
    root index, and only at pipeline open, after N children have been spawned) cannot explain it;
    this one names the environment at cut time instead.

    Deliberately narrow: only a cycle that passes through a bridge node is ours to reject, so the
    partitioner never becomes stricter than the engine on shapes that have nothing to do with
    venvs. ``_detect_cycle`` returns a single cycle, so a document containing both a user cycle
    and a bridge cycle may surface neither here -- the engine remains the backstop.
    """
    known = {component.get('id') for component in main_components}
    adjacency: Dict[str, List[str]] = {component.get('id'): [] for component in main_components}
    for component in main_components:
        for edge in component.get('input', []) or []:
            producer = edge.get('from')
            if producer in known and component.get('id') not in adjacency[producer]:
                adjacency[producer].append(component.get('id'))

    cycle = _detect_cycle(adjacency)
    if cycle is None or not any(node in bridge_env for node in cycle):
        return
    envs = list(OrderedDict.fromkeys(bridge_env[node] for node in cycle if node in bridge_env))
    path = ' -> '.join(bridge_env.get(node, node) for node in cycle)
    raise ValueError(
        f'Virtual environment "{envs[0]}" is entered more than once along "{path}"; a virtual '
        'environment is one bridge node in the base pipeline, so re-entering it around a base '
        'component forms a cycle. Move the base component inside the environment, or split it '
        'into two environments.'
    )


def _routing_table(channels: 'OrderedDict[Tuple[str, str], Dict[str, Any]]') -> List[Dict[str, Any]]:
    """One routing entry per channel, keyed by ``channelId``.

    ``producers``/``consumers`` stay **authored** ids: for a venv→venv lane the producer lives in
    the source venv's document and the consumer in the target's, not the bridge nodes that carry
    them across. The rewritten main-side source is kept separately, on the lane's ``sourceNode``.
    """
    return [
        {
            'channelId': channel['channelId'],
            'sourceEnv': channel['sourceEnv'],
            'targetEnv': channel['targetEnv'],
            'lanes': list(channel['lanes'].keys()),
            'producers': {lane: info['producer'] for lane, info in channel['lanes'].items()},
            'consumers': {lane: list(info['consumers']) for lane, info in channel['lanes'].items()},
            'egressNode': channel['egressNode'],
            'ingressNode': channel['ingressNode'],
        }
        for channel in channels.values()
    ]


def _cut_pipeline(pipeline: Dict[str, Any], source: Optional[str]) -> PartitionResult:
    """Cut isolated groups into per-venv sub-documents wired by bridge nodes.

    See ``packages/server/design/virtual-environments.md`` §4.3/§4.6. Non-isolated groups
    still flatten; each isolated group becomes its own flat sub-document, and every boundary
    data-lane edge gets a ``venv``/``venv_server`` bridge pair plus a ``channelId`` recorded
    in the routing table. The input document is not modified. The body is a sequence of pure
    steps over a single deep copy — one ``_*`` helper above per step.
    """
    _validate_containers(pipeline, source, scoped=True)

    work = copy.deepcopy(pipeline)

    # Before anything reasons about environments: drop what the source cannot reach, so
    # the cut sees the graph the engine would actually have loaded. Every step below --
    # bucketing, boundary channels, cycle checks -- then works on the live document only.
    work['components'] = _prune_unreachable(work.get('components', []), source or work.get('source'))

    components = work.get('components', [])
    env_of = _env_of(components)

    env_blocks = _isolated_env_blocks(components)
    if MAIN_ENV in env_blocks:
        raise ValueError(
            f'A virtual environment cannot be named "{MAIN_ENV}"; that id is reserved for the base environment'
        )

    buckets = _bucket_leaves_by_env(components, env_of)
    _validate_source_placement(components, env_of, work.get('source'), buckets)

    channels, edge_rewrites, quotient = _collect_channels(components, env_of)
    _reject_env_cycles(quotient)

    venv_envs = [env for env in buckets if env != MAIN_ENV]
    stub_ids = _assign_channel_ids(channels, buckets, venv_envs)
    _pair_boundaries(channels)
    _resolve_forward_sources(channels, env_of)

    # Repoint each boundary consumer edge. A forward consumer (in a child) reads from its
    # child ingress; a return consumer (in main) reads from the round-trip node that both
    # sent the forward stream and delivers the return (its ``deliverNode``).
    for edge, key in edge_rewrites:
        channel = channels[key]
        edge['from'] = channel['deliverNode'] if channel['targetEnv'] == MAIN_ENV else channel['ingressNode']

    bridge_nodes = _synthesize_bridges(channels, buckets, stub_ids)
    environments = _assemble_documents(work, buckets, bridge_nodes, env_blocks, stub_ids)

    # Only now does main's graph exist in final form, which is where a venv entered twice shows
    # up as a cycle between its own bridge node and the base components around it.
    bridge_env = {channel['egressNode']: env for (direction, env), channel in channels.items() if direction == FORWARD}
    _reject_main_cycles(environments[MAIN_ENV]['components'], bridge_env)

    groups = {env: env_blocks[env] for env in venv_envs}
    return PartitionResult(environments=environments, routing=_routing_table(channels), groups=groups)


def partition_pipeline(pipeline: Dict[str, Any], source: Optional[str] = None, scoped: bool = False):
    """Prepare a pipeline for the engine, flattening containers or cutting venvs.

    The canvas nests a container's members under ``config.pipeline.components``, but the
    engine reads **only** the top-level ``components`` list and ignores nested ones — so
    without this pass every grouped component is silently dropped from the run.

    ``scoped=False`` (default, and the permanent ``ROCKETRIDE_SERVER_USE_VENV=0`` mode):
    flatten every container to the top level and return a single document, exactly as
    increment 1 did. Membership carries no runtime meaning by itself — members keep their
    ids and connections, so an edge that crossed the boundary needs no rewriting. A
    pipeline with no containers is returned unchanged, by identity.

    ``scoped=True`` (venv scoping on): isolated groups become a separate flat
    sub-document per venv, wired by ``venv``/``venv_server`` bridge pairs at each boundary
    data-lane edge, and the return value is a :class:`PartitionResult`. Non-isolated
    groups still flatten. The call site in ``task_engine.py`` uses the default and is
    unchanged; the scoped gate is flipped by the orchestrator in step 8.

    Args:
        pipeline: Resolved pipeline configuration.
        source: The run's source component id, validated against the containers when
            given.
        scoped: Whether venv scoping is enabled for this run.

    Returns:
        ``scoped=False`` → a new flat pipeline dict, or ``pipeline`` itself when it holds no
        container. Neither form modifies the input, but neither is a private copy either: the
        dict is shallow and its components are the **same objects** as the input's, so mutating
        a component of the result mutates the caller's document.
        ``scoped=True`` → a :class:`PartitionResult` built over a ``deepcopy``, sharing nothing
        with the input. This is the path to use for a speculative call -- a dry partition run
        only to collect the errors below is safe here and is not safe above.

    Raises:
        ValueError: A container nests an environment inside another, holds the source, is
            connected to as if it produced data, an invoke edge crosses an environment
            boundary; or, when scoped, a boundary edge on a non-bridgeable lane, a
            cross-env cycle between venvs, an environment entered more than once around a
            base component, one boundary carrying a lane from two producers, an environment
            that emits but is fed by nothing, or a base environment left without the source.
    """
    if scoped:
        return _cut_pipeline(pipeline, source)

    components = pipeline.get('components', [])
    if not any(is_container(component) for component, _ in walk_components(components)):
        return pipeline

    _validate_containers(pipeline, source, scoped=False)

    partitioned = dict(pipeline)
    partitioned['components'] = _flatten_members(components)
    return partitioned


def resolve_implied_source(pipeline: Dict[str, Any]) -> Optional[str]:
    """Find the implied source component from a pipeline's components list.

    Scans components for exactly one with config.mode == 'Source'.

    Returns:
        The source component ID, or None if no source component found.

    Raises:
        ValueError: If multiple source components are found.
    """
    seen_source = False
    source_id = None
    for component in pipeline.get('components', []):
        config = component.get('config', {})
        if config.get('mode', '') == 'Source':
            if seen_source:
                raise ValueError('Pipeline has multiple source components, please specify one explicitly')
            seen_source = True
            source_id = component.get('id', None)
    return source_id
