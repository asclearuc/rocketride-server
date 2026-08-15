/**
 * MIT License
 *
 * Copyright (c) 2026 Aparavi Software AG
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */

/**
 * Unit tests for the `client.venv` namespace.
 *
 * A fake client records the command and arguments handed to `call()`, so these
 * assert exact wire payloads with no server. The wire spelling is the thing
 * under test: the engine reads `projectId` / `envId` camelCase, and a
 * snake_case slip would silently address the shared `default` bucket instead of
 * failing loudly.
 *
 * The engine's refusals (active run, missing argument, permissions) belong to
 * `cmd_venv`, not to the SDK, and are not exercised here.
 */

import { describe, it, expect, jest } from '@jest/globals';
import { VenvApi } from '../src/client/venv';
import type { VenvGcReport } from '../src/client/types/venv';

const PROJECT = 'proj-abc';
const ENV = 'group_1';
const TEAM = 'team-prod';

function fakeClient() {
	return { call: jest.fn(async (_command: unknown, _args: unknown) => ({})) } as any;
}

describe('VenvApi.list', () => {
	it('dispatches rrext_venv/list and unwraps the environments array', async () => {
		const c = fakeClient();
		const rows = [{ projectId: 'p', envId: 'main', installed: true }];
		c.call.mockResolvedValueOnce({ environments: rows });
		const venv = new VenvApi(c);
		const out = await venv.list();
		expect(c.call).toHaveBeenCalledWith('rrext_venv', { subcommand: 'list' });
		expect(out).toEqual(rows);
	});

	it('omits projectId, sizes and teamId when unset', async () => {
		const c = fakeClient();
		c.call.mockResolvedValueOnce({ environments: [] });
		await new VenvApi(c).list();
		expect(c.call).toHaveBeenCalledWith('rrext_venv', { subcommand: 'list' });
	});

	it('passes the project filter, sizes opt-in and team scope wire-spelled', async () => {
		const c = fakeClient();
		c.call.mockResolvedValueOnce({ environments: [] });
		await new VenvApi(c).list({ projectId: PROJECT, sizes: true, teamId: TEAM });
		expect(c.call).toHaveBeenCalledWith('rrext_venv', {
			subcommand: 'list',
			projectId: PROJECT,
			sizes: true,
			teamId: TEAM,
		});
	});

	it('returns [] when the body carries no environments key', async () => {
		const c = fakeClient();
		c.call.mockResolvedValueOnce({});
		expect(await new VenvApi(c).list()).toEqual([]);
	});
});

describe('VenvApi.purge', () => {
	it('sends the purge subcommand with camelCase ids', async () => {
		const c = fakeClient();
		c.call.mockResolvedValueOnce({ purged: true });
		await new VenvApi(c).purge(PROJECT, ENV);
		expect(c.call).toHaveBeenCalledWith('rrext_venv', {
			subcommand: 'purge',
			projectId: PROJECT,
			envId: ENV,
		});
	});

	it('unwraps the purged flag', async () => {
		const c = fakeClient();
		c.call.mockResolvedValueOnce({ purged: true });
		expect(await new VenvApi(c).purge(PROJECT, ENV)).toBe(true);
	});

	it('treats an absent overlay as false, not an error', async () => {
		const c = fakeClient();
		c.call.mockResolvedValueOnce({ purged: false });
		expect(await new VenvApi(c).purge(PROJECT, ENV)).toBe(false);
	});

	it('sends teamId only when a scope is supplied', async () => {
		const c = fakeClient();
		c.call.mockResolvedValueOnce({ purged: true });
		await new VenvApi(c).purge(PROJECT, ENV, { teamId: TEAM });
		expect(c.call).toHaveBeenCalledWith('rrext_venv', {
			subcommand: 'purge',
			projectId: PROJECT,
			envId: ENV,
			teamId: TEAM,
		});
	});
});

describe('VenvApi.deleteEnv', () => {
	it('sends the delete_env subcommand with both ids', async () => {
		const c = fakeClient();
		c.call.mockResolvedValueOnce({ deleted: true });
		await new VenvApi(c).deleteEnv(PROJECT, ENV);
		expect(c.call).toHaveBeenCalledWith('rrext_venv', {
			subcommand: 'delete_env',
			projectId: PROJECT,
			envId: ENV,
		});
	});

	it('unwraps the deleted flag', async () => {
		const c = fakeClient();
		c.call.mockResolvedValueOnce({ deleted: false });
		expect(await new VenvApi(c).deleteEnv(PROJECT, ENV)).toBe(false);
	});
});

describe('VenvApi.deleteProject', () => {
	it('sends no envId — the wire rejects one on delete_project', async () => {
		const c = fakeClient();
		c.call.mockResolvedValueOnce({ deletedEnvironments: 2 });
		await new VenvApi(c).deleteProject(PROJECT);
		expect(c.call).toHaveBeenCalledWith('rrext_venv', {
			subcommand: 'delete_project',
			projectId: PROJECT,
		});
	});

	it('unwraps the removed-environment count', async () => {
		const c = fakeClient();
		c.call.mockResolvedValueOnce({ deletedEnvironments: 3 });
		expect(await new VenvApi(c).deleteProject(PROJECT)).toBe(3);
	});

	it('treats a project with no overlays as 0', async () => {
		const c = fakeClient();
		c.call.mockResolvedValueOnce({ deletedEnvironments: 0 });
		expect(await new VenvApi(c).deleteProject(PROJECT)).toBe(0);
	});
});

describe('VenvApi.gc', () => {
	// Shaped like the server's answer, including a project-level failure row with no envId.
	// If VenvGcFailed.envId were typed required, this fixture would stop compiling — which is
	// the cheapest possible check on that shape.
	const report: VenvGcReport = {
		dryRun: false,
		maxAgeSeconds: 30 * 24 * 3600,
		scanned: 3,
		collected: [{ projectId: PROJECT, envId: ENV, ageSeconds: 40 * 24 * 3600 }],
		skipped: [{ projectId: PROJECT, reason: 'live' }],
		failed: [{ projectId: PROJECT, reason: 'permission denied' }],
	};

	it('sends only the project when no options are given', async () => {
		const c = fakeClient();
		c.call.mockResolvedValueOnce(report);
		await new VenvApi(c).gc(PROJECT);
		expect(c.call).toHaveBeenCalledWith('rrext_venv', {
			subcommand: 'gc',
			projectId: PROJECT,
		});
	});

	it('sends every option in wire spelling', async () => {
		const c = fakeClient();
		c.call.mockResolvedValueOnce({ ...report, dryRun: true });
		await new VenvApi(c).gc(PROJECT, { maxAgeDays: 7, dryRun: true, teamId: TEAM });
		expect(c.call).toHaveBeenCalledWith('rrext_venv', {
			subcommand: 'gc',
			projectId: PROJECT,
			maxAgeDays: 7,
			dryRun: true,
			teamId: TEAM,
		});
	});

	it('sends maxAgeDays: 0 rather than dropping it — the server floors it, and 0 is legal', async () => {
		const c = fakeClient();
		c.call.mockResolvedValueOnce({ ...report, maxAgeSeconds: 3600 });
		const result = await new VenvApi(c).gc(PROJECT, { maxAgeDays: 0 });
		expect(c.call).toHaveBeenCalledWith('rrext_venv', {
			subcommand: 'gc',
			projectId: PROJECT,
			maxAgeDays: 0,
		});
		expect(result.maxAgeSeconds).toBe(3600);
	});

	it('returns the whole report — unwrapping one key would discard the reasons', async () => {
		const c = fakeClient();
		c.call.mockResolvedValueOnce(report);
		const result = await new VenvApi(c).gc(PROJECT);
		expect(result.scanned).toBe(3);
		expect(result.collected[0].envId).toBe(ENV);
		expect(result.skipped[0].reason).toBe('live');
		expect(result.failed[0].envId).toBeUndefined();
	});
});
