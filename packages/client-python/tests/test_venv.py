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
Unit tests for the ``client.venv`` namespace.

Uses an async fake client that records the command and kwargs passed to
``call()`` so we can assert exact wire payloads without a live server
connection. The wire spelling is the thing under test: the engine reads
``projectId`` / ``envId`` camelCase, and a snake_case slip would address the
shared ``default`` bucket instead of failing loudly.

The engine's own refusals (active run, missing argument, permissions) are not
exercised here -- they are `cmd_venv`'s behaviour, not the SDK's, and reaching
them needs a live server.
"""

import pytest

from rocketride.venv import VenvApi


# =========================================================================
# FAKE CLIENT
# =========================================================================


class FakeClient:
    """Async fake whose ``call`` method records the command and kwargs it receives."""

    def __init__(self, return_value=None):
        self.calls = []
        self._return_value = return_value or {}

    async def call(self, command, **kwargs):
        """Record the call and return a configurable stub body."""
        self.calls.append({'command': command, **kwargs})
        return self._return_value

    @property
    def last_call(self):
        """Return the most recent recorded call."""
        return self.calls[-1]


# =========================================================================
# HELPERS
# =========================================================================

PROJECT = 'proj-abc'
ENV = 'group_1'
TEAM = 'team-prod'


def make_api(return_value=None):
    """Create a VenvApi backed by a FakeClient."""
    fake = FakeClient(return_value=return_value)
    return VenvApi(fake), fake


# =========================================================================
# list
# =========================================================================


class TestList:
    """Tests for VenvApi.list."""

    @pytest.mark.asyncio
    async def test_sends_list_subcommand_and_unwraps_environments(self):
        """Dispatches rrext_venv/list and returns the environments array."""
        rows = [{'projectId': 'p', 'envId': 'main', 'installed': True}]
        api, fake = make_api(return_value={'environments': rows})
        result = await api.list()
        assert fake.last_call == {'command': 'rrext_venv', 'subcommand': 'list'}
        assert result == rows

    @pytest.mark.asyncio
    async def test_omits_optional_arguments_when_unset(self):
        """An unfiltered list sends no projectId, no sizes and no teamId."""
        api, fake = make_api(return_value={'environments': []})
        await api.list()
        assert 'projectId' not in fake.last_call
        assert 'sizes' not in fake.last_call
        assert 'teamId' not in fake.last_call

    @pytest.mark.asyncio
    async def test_passes_project_filter_sizes_and_team(self):
        """Supplied options travel wire-spelled."""
        api, fake = make_api(return_value={'environments': []})
        await api.list(PROJECT, sizes=True, team_id=TEAM)
        assert fake.last_call['projectId'] == PROJECT
        assert fake.last_call['sizes'] is True
        assert fake.last_call['teamId'] == TEAM

    @pytest.mark.asyncio
    async def test_missing_environments_key_yields_empty_list(self):
        """A body without the key returns [] rather than raising."""
        api, _ = make_api(return_value={})
        assert await api.list() == []


# =========================================================================
# purge
# =========================================================================


class TestPurge:
    """Tests for VenvApi.purge."""

    @pytest.mark.asyncio
    async def test_sends_wire_spelled_ids(self):
        """Dispatches rrext_venv/purge with camelCase ids."""
        api, fake = make_api(return_value={'purged': True})
        await api.purge(PROJECT, ENV)
        assert fake.last_call == {
            'command': 'rrext_venv',
            'subcommand': 'purge',
            'projectId': PROJECT,
            'envId': ENV,
        }

    @pytest.mark.asyncio
    async def test_unwraps_purged_flag(self):
        """Returns the body's purged flag."""
        api, _ = make_api(return_value={'purged': True})
        assert await api.purge(PROJECT, ENV) is True

    @pytest.mark.asyncio
    async def test_absent_overlay_is_false_not_an_error(self):
        """False means 'the overlay was not there' -- idempotent success."""
        api, _ = make_api(return_value={'purged': False})
        assert await api.purge(PROJECT, ENV) is False

    @pytest.mark.asyncio
    async def test_team_scope_is_optional(self):
        """The team scope travels only when supplied."""
        api, fake = make_api(return_value={'purged': True})
        await api.purge(PROJECT, ENV)
        assert 'teamId' not in fake.last_call
        await api.purge(PROJECT, ENV, team_id=TEAM)
        assert fake.last_call['teamId'] == TEAM


# =========================================================================
# delete_env
# =========================================================================


class TestDeleteEnv:
    """Tests for VenvApi.delete_env."""

    @pytest.mark.asyncio
    async def test_sends_delete_env_subcommand(self):
        """delete_env dispatches the delete_env subcommand with both ids."""
        api, fake = make_api(return_value={'deleted': True})
        await api.delete_env(PROJECT, ENV)
        assert fake.last_call == {
            'command': 'rrext_venv',
            'subcommand': 'delete_env',
            'projectId': PROJECT,
            'envId': ENV,
        }

    @pytest.mark.asyncio
    async def test_unwraps_deleted_flag(self):
        """delete_env returns the body's deleted flag."""
        api, _ = make_api(return_value={'deleted': False})
        assert await api.delete_env(PROJECT, ENV) is False


# =========================================================================
# delete_project
# =========================================================================


class TestDeleteProject:
    """Tests for VenvApi.delete_project."""

    @pytest.mark.asyncio
    async def test_sends_project_only(self):
        """delete_project sends no envId -- the wire rejects one there."""
        api, fake = make_api(return_value={'deletedEnvironments': 2})
        await api.delete_project(PROJECT)
        assert fake.last_call == {
            'command': 'rrext_venv',
            'subcommand': 'delete_project',
            'projectId': PROJECT,
        }

    @pytest.mark.asyncio
    async def test_unwraps_removed_count(self):
        """delete_project returns how many environments were removed."""
        api, _ = make_api(return_value={'deletedEnvironments': 3})
        assert await api.delete_project(PROJECT) == 3

    @pytest.mark.asyncio
    async def test_absent_project_is_zero(self):
        """Zero means the project had no overlays -- idempotent success."""
        api, _ = make_api(return_value={'deletedEnvironments': 0})
        assert await api.delete_project(PROJECT) == 0


# =========================================================================
# gc
# =========================================================================


def _report(**overrides):
    """A gc report shaped like the server's, including a project-level failure row."""
    report = {
        'dryRun': False,
        'maxAgeSeconds': 30 * 24 * 3600,
        'scanned': 3,
        'collected': [{'projectId': PROJECT, 'envId': ENV, 'ageSeconds': 40 * 24 * 3600}],
        'skipped': [{'projectId': PROJECT, 'reason': 'live'}],
        # No envId: an unreadable project directory has no single environment to blame, which is
        # why the type makes that field optional.
        'failed': [{'projectId': PROJECT, 'reason': 'permission denied'}],
    }
    report.update(overrides)
    return report


class TestGc:
    """Tests for VenvApi.gc."""

    @pytest.mark.asyncio
    async def test_sends_only_the_project_by_default(self):
        """Omitted options stay off the wire, so the server applies its own threshold."""
        api, fake = make_api(return_value=_report())
        await api.gc(PROJECT)
        assert fake.last_call == {
            'command': 'rrext_venv',
            'subcommand': 'gc',
            'projectId': PROJECT,
        }

    @pytest.mark.asyncio
    async def test_sends_every_option_in_wire_spelling(self):
        """maxAgeDays/dryRun/teamId are camelCase on the wire; snake_case here would fail loudly."""
        api, fake = make_api(return_value=_report(dryRun=True))
        await api.gc(PROJECT, max_age_days=7, dry_run=True, team_id=TEAM)
        assert fake.last_call == {
            'command': 'rrext_venv',
            'subcommand': 'gc',
            'projectId': PROJECT,
            'maxAgeDays': 7,
            'dryRun': True,
            'teamId': TEAM,
        }

    @pytest.mark.asyncio
    async def test_returns_the_whole_report(self):
        """The report is the result: unwrapping one key would discard the reasons."""
        api, _ = make_api(return_value=_report())
        result = await api.gc(PROJECT)
        assert result['scanned'] == 3
        assert result['collected'][0]['envId'] == ENV
        assert result['skipped'][0]['reason'] == 'live'
        assert 'envId' not in result['failed'][0]

    @pytest.mark.asyncio
    async def test_max_age_zero_is_sent_rather_than_dropped(self):
        """0 is a legal threshold, not an absent one -- the server's floor makes it safe."""
        api, fake = make_api(return_value=_report(maxAgeSeconds=3600))
        result = await api.gc(PROJECT, max_age_days=0)
        assert fake.last_call['maxAgeDays'] == 0
        assert result['maxAgeSeconds'] == 3600, 'the report shows the floor the server applied'
