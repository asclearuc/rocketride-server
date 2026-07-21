"""Pipeline utility functions for source resolution, substitution and partitioning."""

import json
import re
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


def _validate_containers(pipeline: Dict[str, Any], source: Optional[str]) -> None:
    """Reject documents whose containers cannot be executed as written.

    These are structural errors the editor should have prevented, so they fail the
    run with a named cause rather than being silently normalised into something the
    author did not write.
    """
    components = pipeline.get('components', [])
    env_of: Dict[str, Optional[str]] = {}
    container_ids = set()

    for component, container in walk_components(components):
        component_id = component.get('id')
        if is_container(component):
            container_ids.add(component_id)
            if is_isolated(component) and container is not None and is_isolated(container):
                raise ValueError(
                    f'Virtual environment "{component_id}" is nested inside "{container.get("id")}"; nested environments are not supported'
                )
        # The environment a component runs in: itself if isolated, else its container's.
        env_of[component_id] = container.get('id') if container is not None and is_isolated(container) else None

    if source is not None and env_of.get(source) is not None:
        raise ValueError(
            f'Source component "{source}" is inside virtual environment "{env_of[source]}"; the source must stay outside'
        )

    for component, _ in walk_components(components):
        target_env = env_of.get(component.get('id'))
        # Invoke/control edges cannot cross an environment boundary: the callee runs
        # in another process, and the call is synchronous with no lane to carry it.
        for control in component.get('control', []) or []:
            source_env = env_of.get(control.get('from'))
            if source_env != target_env:
                raise ValueError(
                    f'Invoke connection from "{control.get("from")}" to "{component.get("id")}" crosses a virtual environment boundary'
                )
        # A container has no lanes, so nothing can connect to one.
        for lane in component.get('input', []) or []:
            if lane.get('from') in container_ids:
                raise ValueError(
                    f'Component "{component.get("id")}" takes input from container "{lane.get("from")}", which produces no data'
                )


def partition_pipeline(pipeline: Dict[str, Any], source: Optional[str] = None) -> Dict[str, Any]:
    """Flatten container members to the top level so the engine can see them.

    The canvas nests a container's members under ``config.pipeline.components``,
    but the engine reads **only** the top-level ``components`` list and ignores
    nested ones — so without this pass every grouped component is silently dropped
    from the run. Membership carries no runtime meaning by itself: members keep
    their own ids and connections, so lifting them changes nothing else.

    Virtual environments are flattened the same way for now. Running their members
    in a separate process is what the bridge nodes and the orchestrator add later;
    until then an isolated container behaves as an organizational one, which is
    also exactly what the compatibility mode (``ROCKETRIDE_SERVER_USE_VENV=0``)
    must keep doing permanently.

    Args:
        pipeline: Resolved pipeline configuration.
        source: The run's source component id, validated against the containers
            when given.

    Returns:
        A new pipeline whose ``components`` are flat. The input is not modified.

    Raises:
        ValueError: A container nests an environment inside another, holds the
            source, is connected to as if it produced data, or an invoke edge
            crosses an environment boundary.
    """
    components = pipeline.get('components', [])
    if not any(is_container(component) for component, _ in walk_components(components)):
        return pipeline

    _validate_containers(pipeline, source)

    def flatten(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        flat: List[Dict[str, Any]] = []
        for component in items:
            members = _members(component)
            if not is_container(component):
                flat.append(component)
                continue
            # Members take the container's place, keeping author order; the
            # container itself has no runtime behaviour and disappears with it.
            flat.extend(flatten(members))
        return flat

    partitioned = dict(pipeline)
    partitioned['components'] = flatten(components)
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
