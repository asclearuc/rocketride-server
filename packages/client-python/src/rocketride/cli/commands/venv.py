"""
Virtual environment overlay commands.

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
    rocketride venv gc <projectId>                - reclaim what has gone idle
"""

from ..utils.common import connect_client, run_cli_command
from ..utils.output import Output


async def run_venv(args) -> int:
    """
    Execute one ``venv`` subcommand.

    Args:
        args: Parsed argparse namespace (venv_subcommand, projectId, envId, ...).

    Returns:
        Exit code.
    """

    async def action(out: Output) -> int:
        client = await connect_client(args.uri, args.apikey)
        subcommand = args.venv_subcommand

        if subcommand == 'list':
            # step: enumerate overlays, optionally with their installed size
            project_id = getattr(args, 'projectId', None) or None
            sizes = bool(getattr(args, 'sizes', False))
            overlays = await client.venv.list(project_id, sizes=sizes)
            if not overlays:
                out.line('No environment overlays found')
            else:
                for overlay in overlays:
                    state = 'installed' if overlay.get('installed') else 'empty'
                    size = f'  {overlay.get("bytes", 0):>14,} bytes' if 'bytes' in overlay else ''
                    out.line(f'{overlay.get("projectId")}/{overlay.get("envId")}  {state:<9}{size}')
                out.line(f'    {len(overlays):>8,} Environment(s)')
            out.result(overlays)
            return 0

        if subcommand == 'purge':
            # step: empty one environment's site-packages, keeping its compiled inputs
            project_id = args.projectId
            env_id = args.envId
            purged = await client.venv.purge(project_id, env_id)
            # False is not a failure: the overlay simply was not there.
            if purged:
                out.line(f'Purged {project_id}/{env_id}')
            else:
                out.line(f'Nothing to reclaim -- {project_id}/{env_id} has no overlay')
            out.result({'projectId': project_id, 'envId': env_id, 'purged': purged})
            return 0

        if subcommand == 'delete':
            # step: remove one environment overlay entirely
            project_id = args.projectId
            env_id = args.envId
            deleted = await client.venv.delete_env(project_id, env_id)
            if deleted:
                out.line(f'Deleted {project_id}/{env_id}')
            else:
                out.line(f'Nothing to delete -- {project_id}/{env_id} has no overlay')
            out.result({'projectId': project_id, 'envId': env_id, 'deleted': deleted})
            return 0

        if subcommand == 'delete-project':
            # step: remove a project's whole venvs/<projectId>/ subtree
            project_id = args.projectId
            removed = await client.venv.delete_project(project_id)
            out.line(f'Removed {removed:,} environment(s) of {project_id}')
            out.result({'projectId': project_id, 'removed': removed})
            return 0

        if subcommand == 'gc':
            # step: reclaim one project's overlays that nothing has activated for a while
            project_id = args.projectId
            report = await client.venv.gc(
                project_id,
                max_age_days=getattr(args, 'max_age_days', None),
                dry_run=bool(getattr(args, 'dry_run', False)),
            )
            days = report['maxAgeSeconds'] / 86400
            verb = 'Would collect' if report['dryRun'] else 'Collected'
            out.line(f'Idle longer than {days:,.1f} day(s), of {report["scanned"]:,} overlay(s) examined:')
            for row in report['collected']:
                age = row['ageSeconds'] / 86400
                out.line(f'  {verb.lower()} {row["projectId"]}/{row["envId"]}  idle {age:,.1f} day(s)')
            # Skips and failures are the interesting half: a live project is normal, a failure
            # names the process still holding the overlay. Never summarise these away.
            for row in report['skipped']:
                out.line(f'  skipped {row["projectId"]}  ({row["reason"]})')
            for row in report['failed']:
                target = f'{row["projectId"]}/{row["envId"]}' if row.get('envId') else row['projectId']
                out.line(f'  FAILED  {target}  {row["reason"]}')
            out.line(f'    {len(report["collected"]):>8,} {verb.lower()}, {len(report["failed"]):,} failed')
            out.result(report)
            return 0

        return out.fail(f'Unknown venv subcommand: {subcommand}')

    return await run_cli_command(args, action)
