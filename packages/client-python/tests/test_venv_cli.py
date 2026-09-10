# =============================================================================
# MIT License
# Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""
Unit tests for the `rocketride venv` CLI command.

These exercise `run_venv` through the CLI's parse + dispatch path with a fake
client, so no live server is required. They exist because the venv CLI was
**ported** onto the command-module layout the CLI grew after it was written:
the old monolithic entry point registered it inline, and nothing outside that
file proved the registration was there at all. A port that wires the parser but
forgets the dispatch branch — or the reverse — passes every other test in this
package, so the parse-and-dispatch path is exactly what these pin.
"""

import importlib
import json
from typing import Any, Dict, List

import pytest

cli_main = importlib.import_module('rocketride.cli.main')
cli_venv = importlib.import_module('rocketride.cli.commands.venv')
cli_common = importlib.import_module('rocketride.cli.utils.common')

pytestmark = pytest.mark.asyncio


class FakeVenvApi:
    """Records what the CLI asked the SDK for, and answers with canned data."""

    def __init__(self, overlays=None, purged=True, deleted=True, removed=3, report=None):
        self.calls: List[Dict[str, Any]] = []
        self._overlays = overlays if overlays is not None else []
        self._purged = purged
        self._deleted = deleted
        self._removed = removed
        self._report = report or {
            'dryRun': False,
            'maxAgeSeconds': 30 * 86400,
            'scanned': 2,
            'collected': [{'projectId': 'p', 'envId': 'v1', 'ageSeconds': 40 * 86400}],
            'skipped': [{'projectId': 'live', 'reason': 'project is live'}],
            'failed': [],
        }

    async def list(self, project_id=None, *, sizes=False):
        self.calls.append({'op': 'list', 'projectId': project_id, 'sizes': sizes})
        return self._overlays

    async def purge(self, project_id, env_id):
        self.calls.append({'op': 'purge', 'projectId': project_id, 'envId': env_id})
        return self._purged

    async def delete_env(self, project_id, env_id):
        self.calls.append({'op': 'delete', 'projectId': project_id, 'envId': env_id})
        return self._deleted

    async def delete_project(self, project_id):
        self.calls.append({'op': 'delete-project', 'projectId': project_id})
        return self._removed

    async def gc(self, project_id, *, max_age_days=None, dry_run=False):
        self.calls.append({'op': 'gc', 'projectId': project_id, 'maxAgeDays': max_age_days, 'dryRun': dry_run})
        return self._report


class FakeClient:
    def __init__(self, venv: FakeVenvApi):
        self.venv = venv
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False


async def run_cli(monkeypatch, fake: FakeClient, argv: List[str]) -> int:
    """Run the CLI's parse + dispatch path with a fake client, returning its exit code."""

    async def fake_connect_client(uri, apikey='', on_event=None):
        cli_common._active_clients.append(fake)
        await fake.connect()
        return fake

    monkeypatch.setattr(cli_venv, 'connect_client', fake_connect_client)
    parser = cli_main.setup_parser()
    args = parser.parse_args(['venv', *argv])
    return await cli_main._dispatch(args)


class TestVenvCliWiring:
    """The registration itself — the half a port silently drops."""

    async def test_the_subcommand_is_registered_at_all(self):
        # Parsing is the cheapest proof that the parser half of the port landed.
        args = cli_main.setup_parser().parse_args(['venv', 'list'])
        assert args.command == 'venv'
        assert args.venv_subcommand == 'list'

    async def test_dispatch_reaches_run_venv(self, monkeypatch):
        # And this is the other half: a parser without a dispatch branch parses fine
        # and then falls through to "Unknown command".
        fake = FakeClient(FakeVenvApi(overlays=[]))
        assert await run_cli(monkeypatch, fake, ['list']) == 0
        assert fake.venv.calls[0]['op'] == 'list'

    async def test_a_bare_venv_with_no_subcommand_is_refused(self, monkeypatch, capsys):
        parser = cli_main.setup_parser()
        args = parser.parse_args(['venv'])
        assert await cli_main._dispatch(args) == 1
        assert 'Venv subcommand is required' in capsys.readouterr().err


class TestVenvCliSubcommands:
    async def test_list_passes_project_and_sizes(self, monkeypatch, capsys):
        overlays = [{'projectId': 'p', 'envId': 'v1', 'installed': True, 'bytes': 1024}]
        fake = FakeClient(FakeVenvApi(overlays=overlays))
        assert await run_cli(monkeypatch, fake, ['list', 'proj-1', '--sizes']) == 0
        assert fake.venv.calls[0] == {'op': 'list', 'projectId': 'proj-1', 'sizes': True}
        assert 'p/v1' in capsys.readouterr().out

    async def test_list_without_a_project_asks_for_every_overlay(self, monkeypatch):
        # The empty positional must reach the SDK as None, not as '': the server reads
        # "no project" as "every overlay", and '' would filter for a project named ''.
        fake = FakeClient(FakeVenvApi(overlays=[]))
        await run_cli(monkeypatch, fake, ['list'])
        assert fake.venv.calls[0]['projectId'] is None

    async def test_an_empty_listing_says_so(self, monkeypatch, capsys):
        fake = FakeClient(FakeVenvApi(overlays=[]))
        assert await run_cli(monkeypatch, fake, ['list']) == 0
        assert 'No environment overlays found' in capsys.readouterr().out

    async def test_purge_reports_a_miss_without_failing(self, monkeypatch, capsys):
        # False is not a failure: the overlay simply was not there, and an operator
        # scripting a purge loop must not have to special-case that.
        fake = FakeClient(FakeVenvApi(purged=False))
        assert await run_cli(monkeypatch, fake, ['purge', 'p', 'v1']) == 0
        assert 'Nothing to reclaim' in capsys.readouterr().out

    async def test_purge_names_what_it_reclaimed(self, monkeypatch, capsys):
        fake = FakeClient(FakeVenvApi(purged=True))
        await run_cli(monkeypatch, fake, ['purge', 'p', 'v1'])
        assert fake.venv.calls[0] == {'op': 'purge', 'projectId': 'p', 'envId': 'v1'}
        assert 'Purged p/v1' in capsys.readouterr().out

    async def test_delete_and_delete_project_reach_their_own_sdk_calls(self, monkeypatch):
        fake = FakeClient(FakeVenvApi())
        await run_cli(monkeypatch, fake, ['delete', 'p', 'v1'])
        await run_cli(monkeypatch, fake, ['delete-project', 'p'])
        assert [c['op'] for c in fake.venv.calls] == ['delete', 'delete-project']

    async def test_gc_forwards_its_two_options(self, monkeypatch):
        fake = FakeClient(FakeVenvApi())
        assert await run_cli(monkeypatch, fake, ['gc', 'p', '--max-age-days', '7', '--dry-run']) == 0
        assert fake.venv.calls[0] == {'op': 'gc', 'projectId': 'p', 'maxAgeDays': 7.0, 'dryRun': True}

    async def test_gc_prints_skips_and_failures_rather_than_summarising(self, monkeypatch, capsys):
        # The interesting half of a collection report: a live project is normal, a failure
        # names the process still holding the overlay. Summarising these away hides both.
        report = {
            'dryRun': True,
            'maxAgeSeconds': 7 * 86400,
            'scanned': 3,
            'collected': [],
            'skipped': [{'projectId': 'live', 'reason': 'project is live'}],
            'failed': [{'projectId': 'p', 'envId': 'v1', 'reason': 'file in use'}],
        }
        fake = FakeClient(FakeVenvApi(report=report))
        await run_cli(monkeypatch, fake, ['gc', 'p', '--dry-run'])
        out = capsys.readouterr().out
        assert 'skipped live' in out
        assert 'FAILED  p/v1  file in use' in out

    async def test_json_output_carries_the_payload(self, monkeypatch, capsys):
        overlays = [{'projectId': 'p', 'envId': 'v1', 'installed': True}]
        fake = FakeClient(FakeVenvApi(overlays=overlays))
        parser = cli_main.setup_parser()
        args = parser.parse_args(['venv', 'list', '--json'])

        async def fake_connect_client(uri, apikey='', on_event=None):
            cli_common._active_clients.append(fake)
            await fake.connect()
            return fake

        monkeypatch.setattr(cli_venv, 'connect_client', fake_connect_client)
        assert await cli_main._dispatch(args) == 0
        assert json.loads(capsys.readouterr().out) == overlays

    async def test_an_unknown_subcommand_fails_rather_than_silently_passing(self, monkeypatch):
        fake = FakeClient(FakeVenvApi())
        parser = cli_main.setup_parser()
        args = parser.parse_args(['venv', 'list'])
        args.venv_subcommand = 'nonsense'

        async def fake_connect_client(uri, apikey='', on_event=None):
            cli_common._active_clients.append(fake)
            await fake.connect()
            return fake

        monkeypatch.setattr(cli_venv, 'connect_client', fake_connect_client)
        assert await cli_main._dispatch(args) != 0
