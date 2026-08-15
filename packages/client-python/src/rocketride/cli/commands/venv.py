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
RocketRide CLI Venv Command Implementation.

Enumerates and reclaims the per-environment ``site-packages`` overlays that
pipeline containers install into.

**These act on the SERVER you connect to, not on the local machine.** Against
a local engine that is your own disk; against a remote or cloud engine it is
not. An overlay is a rebuildable cache -- the requirements live in the pipeline
document -- so reclaiming one costs the next run's install time and no data.

Commands:
    rocketride venv list [projectId] [--sizes]    - list environment overlays
    rocketride venv purge <projectId> <envId>     - empty one environment
    rocketride venv delete <projectId> <envId>    - remove one environment
    rocketride venv delete-project <projectId>    - remove a project's subtree
"""

import json
from typing import TYPE_CHECKING
from .base import BaseCommand

if TYPE_CHECKING:
    from ..main import RocketRideClient


class VenvCommand(BaseCommand):
    """Command implementation for virtual environment overlay operations."""

    def __init__(self, cli, args):
        """Initialize VenvCommand."""
        super().__init__(cli, args)

        self._subcommand_handlers = {
            'list': self._cmd_list,
            'purge': self._cmd_purge,
            'delete': self._cmd_delete,
            'delete-project': self._cmd_delete_project,
            'gc': self._cmd_gc,
        }

    async def execute(self, client: 'RocketRideClient') -> int:
        """Execute the venv command based on subcommand."""
        try:
            if not self.cli.client.is_connected():
                await self.cli.connect()

            if handler := self._subcommand_handlers.get(self.args.venv_subcommand):
                return await handler(client)
            else:
                raise ValueError(f'Unknown venv subcommand: {self.args.venv_subcommand}')

        except Exception as e:  # noqa: BLE001
            # The engine's refusals (active run, busy files, permissions) carry
            # their own cause; print them verbatim rather than rewording.
            print(f'Error: {e}')
            return 1

    async def _cmd_list(self, client: 'RocketRideClient') -> int:
        """List environment overlays on the server."""
        project_id = getattr(self.args, 'projectId', None) or None
        sizes = bool(getattr(self.args, 'sizes', False))
        overlays = await client.venv.list(project_id, sizes=sizes)

        if hasattr(self.args, 'json') and self.args.json:
            print(json.dumps(overlays, indent=2))
            return 0

        if not overlays:
            print('No environment overlays found')
            return 0

        for overlay in overlays:
            state = 'installed' if overlay.get('installed') else 'empty'
            size = f'  {overlay.get("bytes", 0):>14,} bytes' if 'bytes' in overlay else ''
            print(f'{overlay.get("projectId")}/{overlay.get("envId")}  {state:<9}{size}')
        print(f'    {len(overlays):>8,} Environment(s)')
        return 0

    async def _cmd_purge(self, client: 'RocketRideClient') -> int:
        """Empty one environment's site-packages, keeping its compiled inputs."""
        project_id = self.args.projectId
        env_id = self.args.envId
        print(f'Purging {project_id}/{env_id}...')
        purged = await client.venv.purge(project_id, env_id)
        # False is not a failure: the overlay simply was not there.
        if purged:
            print(f'Purged {project_id}/{env_id}')
        else:
            print(f'Nothing to reclaim -- {project_id}/{env_id} has no overlay')
        return 0

    async def _cmd_delete(self, client: 'RocketRideClient') -> int:
        """Remove one environment overlay entirely."""
        project_id = self.args.projectId
        env_id = self.args.envId
        print(f'Deleting {project_id}/{env_id}...')
        deleted = await client.venv.delete_env(project_id, env_id)
        if deleted:
            print(f'Deleted {project_id}/{env_id}')
        else:
            print(f'Nothing to delete -- {project_id}/{env_id} has no overlay')
        return 0

    async def _cmd_delete_project(self, client: 'RocketRideClient') -> int:
        """Remove a project's whole venvs/<projectId>/ subtree."""
        project_id = self.args.projectId
        print(f'Deleting the overlay subtree of {project_id}...')
        removed = await client.venv.delete_project(project_id)
        print(f'Removed {removed:,} environment(s) of {project_id}')
        return 0

    async def _cmd_gc(self, client: 'RocketRideClient') -> int:
        """Reclaim one project's overlays that nothing has activated for a while."""
        project_id = self.args.projectId
        max_age_days = getattr(self.args, 'max_age_days', None)
        dry_run = bool(getattr(self.args, 'dry_run', False))
        report = await client.venv.gc(project_id, max_age_days=max_age_days, dry_run=dry_run)

        # --json for the same reason list has it and purge does not: this answers with a report,
        # which an operator will want to diff between runs or feed to something else.
        if getattr(self.args, 'json', False):
            print(json.dumps(report, indent=2))
            return 0

        days = report['maxAgeSeconds'] / 86400
        verb = 'Would collect' if report['dryRun'] else 'Collected'
        print(f'Idle longer than {days:,.1f} day(s), of {report["scanned"]:,} overlay(s) examined:')
        for row in report['collected']:
            age = row['ageSeconds'] / 86400
            print(f'  {verb.lower()} {row["projectId"]}/{row["envId"]}  idle {age:,.1f} day(s)')
        # Skips and failures are the interesting half: a live project is normal, a failure names
        # the process still holding the overlay. Never summarise these away.
        for row in report['skipped']:
            print(f'  skipped {row["projectId"]}  ({row["reason"]})')
        for row in report['failed']:
            target = f'{row["projectId"]}/{row["envId"]}' if row.get('envId') else row['projectId']
            print(f'  FAILED  {target}  {row["reason"]}')
        print(f'    {len(report["collected"]):>8,} {verb.lower()}, {len(report["failed"]):,} failed')
        return 0
