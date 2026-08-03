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
VenvCommands: DAP handler for reclaiming per-environment overlays.

Dispatches ``rrext_venv`` on ``arguments.subcommand``:

- ``list``           — read-only enumeration (``task.monitor``)
- ``purge``          — empty an environment's ``site-packages`` (``task.control``)
- ``delete_env``     — remove one environment overlay (``task.control``)
- ``delete_project`` — remove a project's whole ``venvs/`` subtree (``task.control``)

Scope: this is the **engine-side protocol command only**. §4.10's canvas operations A/B/C
are the UI actions that will call it; no SDK method and no canvas wiring ship here.

Every path hangs off one input: ``exe_dir = os.path.dirname(sys.executable)``. This handler
runs in the **server** process, which is itself ``dist/server/engine.exe``, so it resolves to
the same overlay root the task engines install into. Worth stating because it fails silently
rather than loudly: a server launched from anywhere else would enumerate and purge a
different (empty) tree and report success.
"""

import os
import sys
from typing import TYPE_CHECKING, Any, Dict

from ai.common.dap import DAPConn, TransportBase

if TYPE_CHECKING:
    from ..task_server import TaskServer


class VenvCommands(DAPConn):
    """
    DAP router for the ``rrext_venv`` command.

    Permissions are **per subcommand**, not one blanket check: ``task.monitor`` for the read,
    ``task.control`` for the three destructive ones. The gate that actually protects an overlay
    is "no active run for this project", which is orthogonal to permissions and applies on top.
    """

    def __init__(
        self,
        connection_id: int,
        server: 'TaskServer',
        transport: TransportBase,
        **kwargs,
    ) -> None:
        """Initialise the venv subcommand handler lookup table."""
        # All other state (account info, server, transport) lives on TaskConn via the other
        # mixins, so nothing else is set up here. TaskConn must call this explicitly -- see the
        # "Explicit initialization needed due to multiple inheritance" note there.
        self._venv_subcommand_handlers = {
            'list': self._venv_list,
            'purge': self._venv_purge,
            'delete_env': self._venv_delete_env,
            'delete_project': self._venv_delete_project,
        }

    # =========================================================================
    # PERMISSION + ARGUMENT HELPERS
    # =========================================================================

    def _verify_venv_access(self, args: Dict[str, Any], perm: str) -> None:
        """
        The permission gate for every venv subcommand.

        A request that names a team resolves ``perm`` against **that** team; an unscoped
        request keeps the caller's default context. Prior art: ``cmd_log._verify_log_access``
        and ``cmd_task.on_rrext_get_token``.

        **The team branch is a caller-asserted scope check, not a claim of ownership.** An
        overlay is machine-local disk state keyed by a project id; nothing anywhere ties it to
        a team, and this command does not make overlays team-private. Say so here, or the next
        reader builds on a guarantee that is not there.

        Both branches fail closed. Measured rather than assumed, since the neighbouring
        commands' docstrings overstate it: a foreign team raises with a **membership** cause
        (``No membership in team '<id>'``), which is distinguishable from a permission miss.
        What stays hidden is the only thing that matters here — whether the project or the
        overlay exists, which the refusal never reveals.

        Args:
            args: The subcommand arguments (consulted for ``teamId``).
            perm: ``'task.monitor'`` for the read, ``'task.control'`` for the destructive ones.

        Raises:
            PermissionError: The caller lacks ``perm`` in the resolved scope.
        """
        team_id = args.get('teamId') or ''
        if team_id:
            self.verify_team_permission(team_id, perm)
        else:
            self.verify_permission(perm)

    @staticmethod
    def _require(args: Dict[str, Any], name: str) -> str:
        """
        Return a required wire argument, refusing a missing or empty one.

        Refusing beats resolving: ``short_id(None)`` is ``default`` and ``env_dir(exe, None,
        None)`` is ``venvs/default/main`` — the **shared** bucket for engtest/CLI/ad-hoc runs,
        whose runs the active-run gate cannot even see. A destructive call silently landing
        there is the worst outcome available.
        """
        value = (args.get(name) or '').strip()
        if not value:
            raise ValueError(f'{name} is required')
        return value

    @staticmethod
    def _reject(args: Dict[str, Any], name: str, why: str) -> None:
        """Refuse an argument that is meaningless for this subcommand rather than ignoring it.

        A silently ignored ``envId`` is worse than a rejected one: a client that believes
        ``list`` filtered by environment reads a one-row answer as "that is the only
        environment there is".
        """
        if args.get(name):
            raise ValueError(f'{name} {why}')

    @staticmethod
    def _venv_env():
        """Import ``venv_env``, guarded — it lives on the engine's sys.path, not in packages/ai."""
        try:
            import venv_env  # engine sys.path only
        except ImportError as exc:
            raise RuntimeError('venv support is unavailable in this deployment') from exc
        return venv_env

    @staticmethod
    def _exe_dir() -> str:
        return os.path.dirname(sys.executable)

    def _refuse_if_running(self, project_id: str) -> None:
        """Refuse a destructive call while any run of that project is live.

        Per **project**, not per environment: purging ``v1`` is refused while a run that only
        touches ``v2`` is live. A finer gate is buildable from the registry's stored pipeline,
        but it needs three inputs and one of them — whether the run scoped at all — is not
        recorded, while its failure mode is deleting the overlay a live install is writing to.
        The coarse gate costs a scheduling annoyance; the fine one costs an overlay.
        """
        if self._server.has_active_project_run(project_id):
            raise RuntimeError(f'project {project_id} has an active run; stop it before reclaiming')

    # =========================================================================
    # SUBCOMMANDS
    # =========================================================================

    async def _venv_list(self, request: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
        """Enumerate environments. ``projectId`` optional; no ``envId`` — there is nothing to filter with."""
        self._verify_venv_access(args, 'task.monitor')
        self._reject(args, 'envId', 'is not accepted by list')
        rows = self._venv_env().list_envs(
            self._exe_dir(),
            (args.get('projectId') or '').strip() or None,
            sizes=bool(args.get('sizes')),
        )
        return self.build_response(request, body={'environments': rows})

    async def _venv_purge(self, request: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
        """Empty one environment's ``site-packages``, keeping its compiled inputs."""
        self._verify_venv_access(args, 'task.control')
        project_id = self._require(args, 'projectId')
        env_id = self._require(args, 'envId')
        self._refuse_if_running(project_id)
        purged = self._venv_env().purge_env(self._exe_dir(), project_id, env_id)
        return self.build_response(request, body={'purged': purged})

    async def _venv_delete_env(self, request: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
        """Remove one environment overlay entirely."""
        self._verify_venv_access(args, 'task.control')
        project_id = self._require(args, 'projectId')
        env_id = self._require(args, 'envId')
        self._refuse_if_running(project_id)
        deleted = self._venv_env().delete_env(self._exe_dir(), project_id, env_id)
        return self.build_response(request, body={'deleted': deleted})

    async def _venv_delete_project(self, request: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
        """Remove a project's whole ``venvs/`` subtree. ``envId`` is meaningless here."""
        self._verify_venv_access(args, 'task.control')
        project_id = self._require(args, 'projectId')
        self._reject(args, 'envId', 'is not accepted by delete_project')
        self._refuse_if_running(project_id)
        removed = self._venv_env().delete_project(self._exe_dir(), project_id)
        return self.build_response(request, body={'deletedEnvironments': removed})

    # =========================================================================
    # ROUTER
    # =========================================================================

    async def on_rrext_venv(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle DAP ``rrext_venv``: reclaim or enumerate per-environment overlays.

        Args:
            request: DAP request whose ``arguments.subcommand`` selects the operation.
                Arguments are in the **wire** spelling (``projectId``, ``envId``, ``teamId``),
                matching every sibling command; ``venv_env``'s Python parameters stay
                snake_case on purpose.

        Returns:
            DAP response; the body shape depends on the subcommand.
        """
        try:
            args = request.get('arguments') or {}
            subcommand = args.get('subcommand')

            if not subcommand:
                raise ValueError('Subcommand is required')

            if handler := self._venv_subcommand_handlers.get(subcommand):
                return await handler(request, args)
            raise ValueError(f'Unknown subcommand: {subcommand}')

        except Exception as e:
            self.debug_message(f'Venv operation failed: {str(e)}')
            raise
