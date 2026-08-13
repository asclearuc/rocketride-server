# MIT License
#
# Copyright (c) 2026 Aparavi Software AG
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""
Virtual-environment API namespace for the RocketRide Python SDK.

Enumerates and reclaims the per-environment ``site-packages`` overlays via the
``rrext_venv`` DAP command (dispatched by ``subcommand``).

**These act on the server's disk, not the caller's.** The engine resolves the
overlay root from its own executable's directory, so against a local engine
this is your machine and against a remote or cloud engine it emphatically is
not.

Two shapes of "no": a destructive call returns ``False``/``0`` when the target
simply was not there — idempotent success, not a failure — and *raises* when
the engine refuses, most often because a run of that project is still live.
Refusal messages come from the engine verbatim; do not rewrite them.

Usage:
    overlays = await client.venv.list(project_id='proj-1')
    await client.venv.purge('proj-1', 'group_1')
    await client.venv.delete_env('proj-1', 'group_1')
    removed = await client.venv.delete_project('proj-1')
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from .types.venv import VenvOverlay

if TYPE_CHECKING:
    from .client import RocketRideClient


class VenvApi:
    """
    Virtual-environment namespace on RocketRideClient.

    Accessed via ``client.venv`` -- not instantiated directly. All methods
    delegate to the parent client's ``call()`` method which handles envelope
    construction, sending, error detection, and tracing.
    """

    def __init__(self, client: RocketRideClient) -> None:
        """
        Bind this namespace to its parent client.

        Args:
            client: The RocketRideClient instance that owns this namespace.
        """
        self._client = client

    # =========================================================================
    # READ
    # =========================================================================

    async def list(
        self,
        project_id: Optional[str] = None,
        *,
        sizes: bool = False,
        team_id: str = '',
    ) -> List[VenvOverlay]:
        """
        List environment overlays on the server.

        Without ``project_id`` this enumerates **every** overlay on the machine,
        not "yours" -- overlays are disk state keyed by project id and nothing
        ties one to an account. That is the point of the unfiltered form: it
        shows the ones you have forgotten about.

        Args:
            project_id: Limit the listing to one project. Omitted, every overlay
                on the server is returned.
            sizes: Also report ``bytes`` per overlay. Opt-in because it is
                expensive: sizing walks every populated ``site-packages``
                recursively, on the order of half a million ``stat`` calls on a
                well-used machine.
            team_id: Resolve the permission against this team rather than the
                caller's default context.

        Returns:
            One row per overlay, in directory order (project, then environment).
        """
        args = {'subcommand': 'list'}
        if project_id:
            args['projectId'] = project_id
        if sizes:
            args['sizes'] = True
        if team_id:
            args['teamId'] = team_id
        body = await self._client.call('rrext_venv', **args)
        return body.get('environments', [])

    # =========================================================================
    # RECLAIM
    # =========================================================================

    async def purge(self, project_id: str, env_id: str, *, team_id: str = '') -> bool:
        """
        Empty one environment's ``site-packages``, keeping its compiled inputs.

        ``combined.txt`` and ``constraints.txt`` survive, but the next run still
        recompiles: purge drops ``requirements.hash`` first, so a mid-wipe
        failure can never leave a half-emptied overlay marked as installed.

        Args:
            project_id: Pipeline ``project_id``, or the on-disk name from
                :meth:`list`.
            env_id: Container node id, or the on-disk name from :meth:`list`.
            team_id: Resolve the permission against this team.

        Returns:
            True when packages were removed; **False when the overlay did not
            exist**, which is success, not failure.

        Raises:
            RuntimeError: A run of that project is live, or the caller lacks
                ``task.control``. The engine's message is carried verbatim.
        """
        args = {'subcommand': 'purge', 'projectId': project_id, 'envId': env_id}
        if team_id:
            args['teamId'] = team_id
        body = await self._client.call('rrext_venv', **args)
        return body['purged']

    async def delete_env(self, project_id: str, env_id: str, *, team_id: str = '') -> bool:
        """
        Remove one environment overlay entirely, compiled inputs included.

        Args:
            project_id: Pipeline ``project_id``, or the on-disk name from
                :meth:`list`.
            env_id: Container node id, or the on-disk name from :meth:`list`.
            team_id: Resolve the permission against this team.

        Returns:
            True when the overlay was removed; **False when it did not exist**.

        Raises:
            RuntimeError: A run of that project is live, or the caller lacks
                ``task.control``.
        """
        args = {'subcommand': 'delete_env', 'projectId': project_id, 'envId': env_id}
        if team_id:
            args['teamId'] = team_id
        body = await self._client.call('rrext_venv', **args)
        return body['deleted']

    async def delete_project(self, project_id: str, *, team_id: str = '') -> int:
        """
        Remove a project's whole ``venvs/<project_id>/`` **subtree** -- the
        directory itself, not merely its contents.

        Despite the name this deletes no project and no pipeline: it reclaims
        the disk that project's environments occupy. The pipeline document is
        untouched and the next run rebuilds whatever it needs.

        Args:
            project_id: Pipeline ``project_id``, or the on-disk name from
                :meth:`list`.
            team_id: Resolve the permission against this team.

        Returns:
            How many environments were removed; **0 when the project had none**.

        Raises:
            RuntimeError: A run of that project is live, or the caller lacks
                ``task.control``.
        """
        args = {'subcommand': 'delete_project', 'projectId': project_id}
        if team_id:
            args['teamId'] = team_id
        body = await self._client.call('rrext_venv', **args)
        return body['deletedEnvironments']
