"""Pipeline utility functions for source resolution, substitution and partitioning."""

import copy
import json
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, Any, Iterator, List, Optional, Tuple

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
    boundary channel (keyed by ``channelId``) so the step-8 orchestrator can byte-route
    frames between children through main. ``groups`` carries each venv's original
    ``config.environment`` block (its display name; the container node itself is
    stripped from the documents) for step-7/8 logging and metrics.
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


def _validate_containers(pipeline: Dict[str, Any], source: Optional[str]) -> None:
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


def _collect_channels(
    components: List[Dict[str, Any]], env_of: Dict[str, str]
) -> Tuple['OrderedDict[Tuple[str, str, str], Dict[str, Any]]', List[Tuple[Dict[str, Any], Tuple[str, str, str]]]]:
    """Classify boundary edges into channels, deduped per ``(producer, lane, targetEnv)``.

    Returns the channels in first-encounter order and the ``(edge, key)`` pairs to repoint
    once ingress ids exist. An unknown ``from`` stays a dangling edge (as today); a boundary
    edge on a non-bridgeable lane is rejected.
    """
    channels: 'OrderedDict[Tuple[str, str, str], Dict[str, Any]]' = OrderedDict()
    edge_rewrites: List[Tuple[Dict[str, Any], Tuple[str, str, str]]] = []
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
            key = (producer, lane, target_env)
            channel = channels.get(key)
            if channel is None:
                channel = {
                    'lane': lane,
                    'sourceEnv': source_env,
                    'targetEnv': target_env,
                    'producer': producer,
                    'consumers': [],
                }
                channels[key] = channel
            if consumer_id not in channel['consumers']:
                channel['consumers'].append(consumer_id)
            edge_rewrites.append((edge, key))
    return channels, edge_rewrites


def _reject_env_cycles(channels: 'OrderedDict[Tuple[str, str, str], Dict[str, Any]]') -> None:
    """Reject a directed cycle in the venv-only env-quotient graph.

    Main is excluded: routing through main is transparent, so only venv↔venv dependency
    cycles can deadlock, while main-terminated chains (``main→V1→V2→main``) stay legal.
    """
    quotient: Dict[str, List[str]] = {}
    for channel in channels.values():
        src, dst = channel['sourceEnv'], channel['targetEnv']
        if src == MAIN_ENV or dst == MAIN_ENV:
            continue
        quotient.setdefault(src, [])
        quotient.setdefault(dst, [])
        if dst not in quotient[src]:
            quotient[src].append(dst)
    cycle = _detect_cycle(quotient)
    if cycle is not None:
        raise ValueError('Cross-environment cycle between virtual environments: ' + ' -> '.join(cycle))


def _assign_channel_ids(
    channels: 'OrderedDict[Tuple[str, str, str], Dict[str, Any]]',
    buckets: 'OrderedDict[str, List[Dict[str, Any]]]',
    venv_envs: List[str],
) -> Dict[str, str]:
    """Assign each channel its ``channelId`` and bridge node ids; return per-child stub ids.

    Node ids are unique **within their own document** (main and each child are separate
    docs); ``channelId`` is the global routing key and must not collide.
    """
    taken: Dict[str, set] = {env: {leaf.get('id') for leaf in leaves} for env, leaves in buckets.items()}
    stub_ids = {env: _unique_id(VENV_SOURCE_STUB_ID, taken[env]) for env in venv_envs}

    seen_channel_ids: set = set()
    for (producer, lane, target_env), channel in channels.items():
        source_env = channel['sourceEnv']
        channel_id = f'{source_env}->{target_env}/{lane}/{producer}'
        if channel_id in seen_channel_ids:
            raise ValueError(f'Duplicate channelId "{channel_id}"; component ids must not contain "->" or "/"')
        seen_channel_ids.add(channel_id)
        channel['channelId'] = channel_id
        parts = '--'.join(_sanitize_id_part(p) for p in (source_env, target_env, lane, producer))
        channel['egressNode'] = _unique_id(f'venv_egress--{parts}', taken[source_env])
        channel['ingressNode'] = _unique_id(f'venv_ingress--{parts}', taken[target_env])
    return stub_ids


def _bridge_config(channel: Dict[str, Any]) -> Dict[str, Any]:
    """The bridge node's config; child URL/token are filled at spawn (step 7).

    A forward channel (``main -> env``) that is paired with a return channel additionally
    carries the return channel's id/lane: its main-side node is a **round-trip** ``venv``
    node that both sends the forward stream and delivers the return downstream, so the
    spawn injection dials one socket carrying both directions (``?channel=..&return=..``).
    """
    config = {
        'channelId': channel['channelId'],
        'lane': channel['lane'],
        'sourceEnv': channel['sourceEnv'],
        'targetEnv': channel['targetEnv'],
    }
    ret = channel.get('returnChannel')
    if ret is not None:
        config['returnChannelId'] = ret['channelId']
        config['returnLane'] = ret['lane']
    return config


def _pair_boundaries(channels: 'OrderedDict[Tuple[str, str, str], Dict[str, Any]]') -> None:
    """Pair each venv env's forward (``main -> env``) and return (``env -> main``) channel
    for the round-trip bridge model, and reject shapes v1 does not support (step 8).

    The venv boundary is a request/response splice of one object: the object never forks,
    so the return must ride back over the **forward** socket and re-enter main through the
    **same** node that sent it (the engine allows one open object per pipe stack, entered
    at the root -- a separate async re-injection cannot open the already-open object). v1
    therefore supports the linear ``main -> venv -> main`` splice only: each venv env has
    exactly one forward channel from main and at most one return channel to main. Direct
    ``venv <-> venv`` channels and multi-lane fan-in/out to a single env need the step-8
    byte-router and are rejected here with a named cause.

    Mutates the channels in place: sets ``returnChannel`` on each paired forward channel,
    and points each return channel's ``deliverNode``/``ingressNode`` at the round-trip node
    (the forward egress) that delivers it.
    """
    forward_by_env: Dict[str, Dict[str, Any]] = {}
    return_by_env: Dict[str, Dict[str, Any]] = {}
    for channel in channels.values():
        src, dst = channel['sourceEnv'], channel['targetEnv']
        if src != MAIN_ENV and dst != MAIN_ENV:
            raise ValueError(
                f'Channel "{channel["channelId"]}" crosses directly between virtual environments '
                f'"{src}" and "{dst}"; venv-to-venv routing is not supported yet'
            )
        if src == MAIN_ENV:
            if dst in forward_by_env:
                raise ValueError(
                    f'Virtual environment "{dst}" receives more than one lane from main; '
                    'multi-lane fan-in into one venv is not supported yet'
                )
            forward_by_env[dst] = channel
        else:
            if src in return_by_env:
                raise ValueError(
                    f'Virtual environment "{src}" returns more than one lane to main; '
                    'multi-lane fan-out from one venv is not supported yet'
                )
            return_by_env[src] = channel

    for env, ret in return_by_env.items():
        forward = forward_by_env.get(env)
        if forward is None:
            raise ValueError(f'Virtual environment "{env}" returns to main but is not fed from main')
        forward['returnChannel'] = ret
        # The round-trip node (the forward egress) both sends into the venv and delivers its
        # return downstream, so return consumers read from it and its routing entry points
        # there rather than at a now-absent main ingress node.
        ret['deliverNode'] = forward['egressNode']
        ret['ingressNode'] = forward['egressNode']


def _synthesize_bridges(
    channels: 'OrderedDict[Tuple[str, str, str], Dict[str, Any]]',
    buckets: 'OrderedDict[str, List[Dict[str, Any]]]',
    stub_ids: Dict[str, str],
) -> Dict[str, List[Dict[str, Any]]]:
    """Build the bridge nodes for each channel, grouped by the env whose document holds them.

    The boundary is spliced with the ``remote`` request/response model, over one socket per
    forward channel:

    - A **forward channel** (``main -> env``) yields a round-trip ``venv`` node in main (it
      reads the main producer, sends the forward stream, and -- when a return is paired --
      delivers the return downstream to the repointed consumers) and a ``venv_server``
      ingress in the child (reads the child stub, applies the forward stream locally).
    - A **return channel** (``env -> main``) yields only a ``venv_server`` egress in the
      child (reads the venv producer, ships the return back over the **same** socket its
      paired forward node dialed). Main has no separate ingress node: the return re-enters
      through the round-trip node, so the object is never re-opened (see ``_pair_boundaries``).
    """
    bridge_nodes: Dict[str, List[Dict[str, Any]]] = {env: [] for env in buckets}
    for channel in channels.values():
        src, dst = channel['sourceEnv'], channel['targetEnv']
        if src == MAIN_ENV:
            # Forward channel: round-trip node in main + ingress in the child.
            bridge_nodes[MAIN_ENV].append(
                {
                    'id': channel['egressNode'],
                    'provider': 'venv',
                    'input': [{'lane': channel['lane'], 'from': channel['producer']}],
                    'config': _bridge_config(channel),
                }
            )
            bridge_nodes[dst].append(
                {
                    'id': channel['ingressNode'],
                    'provider': 'venv_server',
                    'input': [{'lane': channel['lane'], 'from': stub_ids[dst]}],
                    'config': _bridge_config(channel),
                }
            )
        else:
            # Return channel: egress in the child only; main delivery is the paired
            # round-trip node (no separate main ingress).
            bridge_nodes[src].append(
                {
                    'id': channel['egressNode'],
                    'provider': 'venv_server',
                    'input': [{'lane': channel['lane'], 'from': channel['producer']}],
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


def _routing_table(channels: 'OrderedDict[Tuple[str, str, str], Dict[str, Any]]') -> List[Dict[str, Any]]:
    """One routing entry per channel, keyed by ``channelId`` for the step-8 byte router."""
    return [
        {
            'channelId': channel['channelId'],
            'lane': channel['lane'],
            'sourceEnv': channel['sourceEnv'],
            'targetEnv': channel['targetEnv'],
            'producer': channel['producer'],
            'consumers': list(channel['consumers']),
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
    _validate_containers(pipeline, source)

    work = copy.deepcopy(pipeline)
    components = work.get('components', [])
    env_of = _env_of(components)

    env_blocks = _isolated_env_blocks(components)
    if MAIN_ENV in env_blocks:
        raise ValueError(
            f'A virtual environment cannot be named "{MAIN_ENV}"; that id is reserved for the base environment'
        )

    buckets = _bucket_leaves_by_env(components, env_of)
    _validate_source_placement(components, env_of, work.get('source'), buckets)

    channels, edge_rewrites = _collect_channels(components, env_of)
    _reject_env_cycles(channels)

    venv_envs = [env for env in buckets if env != MAIN_ENV]
    stub_ids = _assign_channel_ids(channels, buckets, venv_envs)
    _pair_boundaries(channels)

    # Repoint each boundary consumer edge. A forward consumer (in a child) reads from its
    # child ingress; a return consumer (in main) reads from the round-trip node that both
    # sent the forward stream and delivers the return (its ``deliverNode``).
    for edge, key in edge_rewrites:
        channel = channels[key]
        edge['from'] = channel['deliverNode'] if channel['targetEnv'] == MAIN_ENV else channel['ingressNode']

    bridge_nodes = _synthesize_bridges(channels, buckets, stub_ids)
    environments = _assemble_documents(work, buckets, bridge_nodes, env_blocks, stub_ids)
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
        ``scoped=False`` → a new flat pipeline dict (input not modified).
        ``scoped=True`` → a :class:`PartitionResult` (input not modified).

    Raises:
        ValueError: A container nests an environment inside another, holds the source, is
            connected to as if it produced data, an invoke edge crosses an environment
            boundary; or, when scoped, a boundary edge on a non-bridgeable lane, a
            cross-env cycle between venvs, or a base environment left without the source.
    """
    if scoped:
        return _cut_pipeline(pipeline, source)

    components = pipeline.get('components', [])
    if not any(is_container(component) for component, _ in walk_components(components)):
        return pipeline

    _validate_containers(pipeline, source)

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
