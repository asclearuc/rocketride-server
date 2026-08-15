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

"""cmd_venv handler tests — what the COMMAND layer owns.

``venv_env``'s own primitives are contract-tested in
``rocketlib-python/tests/test_venv_env.py`` (on Windows AND under WSL). These tests pin the
protocol face: the per-subcommand permission split, the active-run gate INCLUDING the
shortened-id form, and the per-subcommand id rules.

Every refusal is asserted **by cause**, never by "it raised": each gate here fails closed, so
an exception check alone would pass against the wrong gate firing.
"""

from types import MethodType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ai.account.models import resolve_team_permissions
from ai.modules.task.commands.cmd_venv import VenvCommands


TEAM = 'team-1'


def _account_info(perms=('task.control', 'task.monitor')):
    return SimpleNamespace(
        userId='user-1',
        displayName='Rod C',
        email='rod@example.com',
        defaultTeam=TEAM,
        organization={
            'id': 'org-1',
            'name': 'Acme',
            'permissions': [],
            'teams': [{'id': TEAM, 'name': 'Production', 'permissions': list(perms)}],
        },
        sysPermissions=[],
    )


class _FakeVenvEnv:
    """Stand-in for the engine-side module; records what the handler asked for."""

    def __init__(self):
        self.calls = []

    def list_envs(self, exe_dir, project_id=None, sizes=False):
        self.calls.append(('list_envs', project_id, sizes))
        return [{'projectId': 'p', 'envId': 'v1', 'installed': True}]

    def purge_env(self, exe_dir, project_id, env_id):
        self.calls.append(('purge_env', project_id, env_id))
        return True

    def delete_env(self, exe_dir, project_id, env_id):
        self.calls.append(('delete_env', project_id, env_id))
        return True

    def delete_project(self, exe_dir, project_id):
        self.calls.append(('delete_project', project_id))
        return 2

    GC_DEFAULT_MAX_AGE_SECONDS = 30 * 24 * 3600

    def collect_stale(self, exe_dir, max_age_seconds, *, dry_run=False, project_id=None, is_project_live=None):
        self.calls.append(('collect_stale', project_id, max_age_seconds, dry_run))
        self.live_gate = is_project_live
        return {
            'dryRun': dry_run,
            'maxAgeSeconds': int(max_age_seconds),
            'scanned': 1,
            'collected': [],
            'skipped': [],
            'failed': [],
        }

    @staticmethod
    def short_id(text):
        return f'short-{text}'


def _make_conn(
    monkeypatch,
    *,
    perms=('task.control', 'task.monitor'),
    active=False,
    fake=None,
    registered=False,
    gc_max_age=None,
):
    """A VenvCommands instance with __init__ run but the engine module stubbed out."""
    fake = fake or _FakeVenvEnv()
    conn = VenvCommands.__new__(VenvCommands)
    VenvCommands.__init__(conn, 1, MagicMock(), MagicMock())
    conn._account_info = _account_info(perms)
    conn._server = SimpleNamespace(
        has_active_project_run=lambda pid: active,
        # gc gates on registry presence, not on "a run is active": a ttl-resident engine keeps an
        # overlay's .pyd open long after its run completes.
        has_registered_project=lambda pid: registered,
        venv_gc_max_age_seconds=gc_max_age,
    )
    conn.build_response = MagicMock(side_effect=lambda req, body=None: {'type': 'response', 'body': body})
    conn.debug_message = MagicMock()

    # The flat check records which gate ran; the team check keeps real semantics.
    def verify_permission(self, perm):
        if perm not in perms:
            raise PermissionError(f"Permission '{perm}' denied")

    def verify_team_permission(self, team_id, perm):
        if perm not in resolve_team_permissions(self._account_info, team_id):
            raise PermissionError(f"Permission '{perm}' denied")

    conn.verify_permission = MethodType(verify_permission, conn)
    conn.verify_team_permission = MethodType(verify_team_permission, conn)
    monkeypatch.setattr(VenvCommands, '_venv_env', staticmethod(lambda: fake))
    monkeypatch.setattr(VenvCommands, '_exe_dir', staticmethod(lambda: '/exe'))
    conn._fake = fake
    return conn


def _req(**args):
    return {'command': 'rrext_venv', 'arguments': args}


# --- routing ---------------------------------------------------------------


async def test_missing_subcommand_is_named(monkeypatch):
    conn = _make_conn(monkeypatch)
    with pytest.raises(ValueError, match='Subcommand is required'):
        await conn.on_rrext_venv(_req())


async def test_unknown_subcommand_is_named(monkeypatch):
    conn = _make_conn(monkeypatch)
    with pytest.raises(ValueError, match='Unknown subcommand: nope'):
        await conn.on_rrext_venv(_req(subcommand='nope'))


# --- permissions, per subcommand -------------------------------------------


async def test_monitor_only_may_list_but_not_purge(monkeypatch):
    # The only assertion that catches the split collapsing into one blanket check.
    conn = _make_conn(monkeypatch, perms=('task.monitor',))
    body = (await conn.on_rrext_venv(_req(subcommand='list')))['body']
    assert body['environments'][0]['envId'] == 'v1'

    with pytest.raises(PermissionError, match='task.control'):
        await conn.on_rrext_venv(_req(subcommand='purge', projectId='p', envId='v1'))


async def test_a_team_scoped_request_resolves_against_that_team(monkeypatch):
    conn = _make_conn(monkeypatch)
    await conn.on_rrext_venv(_req(subcommand='purge', projectId='p', envId='v1', teamId=TEAM))
    assert ('purge_env', 'p', 'v1') in conn._fake.calls

    # A foreign team fails closed, with a membership cause. Measured rather than assumed: the
    # message is NOT identical to a permission miss ("No membership in team ..." vs
    # "Permission '...' denied"), so the two are distinguishable -- what stays hidden is whether
    # the project or the overlay exists, which is the leak that would matter here.
    with pytest.raises(PermissionError, match='team-other'):
        await conn.on_rrext_venv(_req(subcommand='purge', projectId='p', envId='v1', teamId='team-other'))
    assert ('purge_env', 'p', 'v1') not in conn._fake.calls[1:], 'refused before touching disk'


# --- the id rules, per subcommand ------------------------------------------


@pytest.mark.parametrize('sub', ['purge', 'delete_env', 'delete_project'])
async def test_missing_project_id_is_refused_for_every_destructive_subcommand(monkeypatch, sub):
    # Resolving instead of refusing would land on venvs/default/main -- the SHARED bucket whose
    # runs the active-run gate cannot even see.
    conn = _make_conn(monkeypatch)
    with pytest.raises(ValueError, match='projectId is required'):
        await conn.on_rrext_venv(_req(subcommand=sub, envId='v1'))


@pytest.mark.parametrize('sub', ['purge', 'delete_env'])
async def test_missing_env_id_is_refused_where_it_is_required(monkeypatch, sub):
    conn = _make_conn(monkeypatch)
    with pytest.raises(ValueError, match='envId is required'):
        await conn.on_rrext_venv(_req(subcommand=sub, projectId='p'))


@pytest.mark.parametrize('sub', ['delete_project', 'list'])
async def test_env_id_is_rejected_where_it_is_meaningless(monkeypatch, sub):
    # Rejected, not ignored: a client that believes `list` filtered by environment reads a
    # one-row answer as "that is the only environment there is".
    conn = _make_conn(monkeypatch)
    with pytest.raises(ValueError, match='envId'):
        await conn.on_rrext_venv(_req(subcommand=sub, projectId='p', envId='v1'))


async def test_list_is_accepted_with_neither_id(monkeypatch):
    conn = _make_conn(monkeypatch)
    await conn.on_rrext_venv(_req(subcommand='list'))
    assert conn._fake.calls == [('list_envs', None, False)]


async def test_delete_project_reports_how_many_went(monkeypatch):
    conn = _make_conn(monkeypatch)
    body = (await conn.on_rrext_venv(_req(subcommand='delete_project', projectId='p')))['body']
    assert body['deletedEnvironments'] == 2


# --- the active-run gate ---------------------------------------------------


@pytest.mark.parametrize('sub', ['purge', 'delete_env', 'delete_project'])
async def test_a_live_run_refuses_every_destructive_subcommand(monkeypatch, sub):
    conn = _make_conn(monkeypatch, active=True)
    with pytest.raises(RuntimeError, match='active run'):
        await conn.on_rrext_venv(_req(subcommand=sub, projectId='p', envId='v1' if sub != 'delete_project' else None))


async def test_list_is_not_gated_by_a_live_run(monkeypatch):
    conn = _make_conn(monkeypatch, active=True)
    body = (await conn.on_rrext_venv(_req(subcommand='list')))['body']
    assert body['environments']


# --- gc --------------------------------------------------------------------


async def test_gc_needs_task_control(monkeypatch):
    conn = _make_conn(monkeypatch, perms=('task.monitor',))
    with pytest.raises(PermissionError):
        await conn.on_rrext_venv(_req(subcommand='gc', projectId='p'))


async def test_gc_refuses_a_missing_project_id(monkeypatch):
    # Not ergonomics: an unscoped gc would let any holder of task.control reclaim every tenant's
    # overlays on this machine without naming one. The whole-tree sweep stays server-side only.
    conn = _make_conn(monkeypatch)
    with pytest.raises(ValueError, match='projectId'):
        await conn.on_rrext_venv(_req(subcommand='gc'))


async def test_gc_rejects_env_id(monkeypatch):
    conn = _make_conn(monkeypatch)
    with pytest.raises(ValueError, match='envId'):
        await conn.on_rrext_venv(_req(subcommand='gc', projectId='p', envId='v1'))


async def test_gc_defaults_to_the_library_threshold(monkeypatch):
    conn = _make_conn(monkeypatch)
    await conn.on_rrext_venv(_req(subcommand='gc', projectId='p'))
    assert conn._fake.calls == [('collect_stale', 'p', 30 * 24 * 3600, False)]


async def test_gc_prefers_the_servers_configured_threshold(monkeypatch):
    # An operator who set the override must not find the command disagreeing with the sweep.
    conn = _make_conn(monkeypatch, gc_max_age=7 * 24 * 3600)
    await conn.on_rrext_venv(_req(subcommand='gc', projectId='p'))
    assert conn._fake.calls == [('collect_stale', 'p', 7 * 24 * 3600, False)]


async def test_gc_converts_days_to_seconds_and_forwards_dry_run(monkeypatch):
    conn = _make_conn(monkeypatch)
    body = (await conn.on_rrext_venv(_req(subcommand='gc', projectId='p', maxAgeDays=3, dryRun=True)))['body']
    assert conn._fake.calls == [('collect_stale', 'p', 3 * 86400, True)]
    assert body['dryRun'] is True and body['scanned'] == 1


@pytest.mark.parametrize('bad', ['soon', -1, float('inf'), float('nan')])
async def test_gc_refuses_junk_negative_and_non_finite_ages(monkeypatch, bad):
    # inf and nan both survive float(): one is invalid JSON in the response, the other compares
    # false against every threshold and would silently collect nothing.
    conn = _make_conn(monkeypatch)
    with pytest.raises(ValueError, match='maxAgeDays'):
        await conn.on_rrext_venv(_req(subcommand='gc', projectId='p', maxAgeDays=bad))


async def test_gc_gates_on_registry_presence_not_on_an_active_run(monkeypatch):
    # A completed-but-resident engine still holds the overlay open, so presence is the predicate
    # the collector needs; the sibling commands keep the narrower active-run gate.
    conn = _make_conn(monkeypatch, active=False, registered=True)
    await conn.on_rrext_venv(_req(subcommand='gc', projectId='p'))
    assert conn._fake.live_gate('p') is True, 'gc must be handed has_registered_project'


async def test_gc_does_not_refuse_a_live_project(monkeypatch):
    # The inverse of its siblings: gc means "collect what is safely collectable", so a live
    # project is reported as skipped rather than raised at the caller.
    conn = _make_conn(monkeypatch, active=True, registered=True)
    body = (await conn.on_rrext_venv(_req(subcommand='gc', projectId='p')))['body']
    assert body['scanned'] == 1
