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
 * `venv` — enumerate and reclaim per-environment `site-packages` overlays.
 *
 * **These act on the SERVER you connect to, not on the local machine.** Against
 * a local engine that is your own disk; against a remote or cloud engine it is
 * somebody else's. An overlay is a rebuildable cache — the requirements live in
 * the pipeline document — so reclaiming one costs the next run's install time
 * and no data.
 */

import { Command } from 'commander';
import { addConnectionOptions, connectClient, runCliCommand } from '../common';
import { Output } from '../output';

/**
 * List environment overlays.
 *
 * Omitting `projectId` lists **every** overlay on the server, not just the
 * caller's — the operator asking is usually asking about disk, not ownership.
 */
async function executeList(projectId: string | undefined, options: { sizes?: boolean }, out: Output): Promise<number> {
	const client = await connectClient(options as never);
	const overlays = await client.venv.list({
		...(projectId ? { projectId } : {}),
		...(options.sizes ? { sizes: true } : {}),
	});

	if (overlays.length === 0) {
		out.line('No environment overlays found');
	} else {
		for (const overlay of overlays) {
			const state = overlay.installed ? 'installed' : 'empty';
			const size = overlay.bytes !== undefined ? `  ${overlay.bytes.toLocaleString().padStart(14)} bytes` : '';
			out.line(`${overlay.projectId}/${overlay.envId}  ${state.padEnd(9)}${size}`);
		}
		out.line(`    ${overlays.length.toLocaleString().padStart(8)} Environment(s)`);
	}
	out.result(overlays);
	return 0;
}

/** Empty one environment's `site-packages`, keeping its compiled inputs. */
async function executePurge(projectId: string, envId: string, options: object, out: Output): Promise<number> {
	const client = await connectClient(options as never);
	const purged = await client.venv.purge(projectId, envId);
	// False is not a failure: the overlay simply was not there, and an operator
	// scripting a purge loop must not have to special-case that.
	out.line(purged ? `Purged ${projectId}/${envId}` : `Nothing to reclaim — ${projectId}/${envId} has no overlay`);
	out.result({ projectId, envId, purged });
	return 0;
}

/** Remove one environment overlay entirely. */
async function executeDelete(projectId: string, envId: string, options: object, out: Output): Promise<number> {
	const client = await connectClient(options as never);
	const deleted = await client.venv.deleteEnv(projectId, envId);
	out.line(deleted ? `Deleted ${projectId}/${envId}` : `Nothing to delete — ${projectId}/${envId} has no overlay`);
	out.result({ projectId, envId, deleted });
	return 0;
}

/** Remove a project's whole `venvs/<projectId>/` subtree. */
async function executeDeleteProject(projectId: string, options: object, out: Output): Promise<number> {
	const client = await connectClient(options as never);
	const removed = await client.venv.deleteProject(projectId);
	out.line(`Removed ${removed.toLocaleString()} environment(s) of ${projectId}`);
	out.result({ projectId, removed });
	return 0;
}

/**
 * Reclaim one project's overlays that nothing has activated for a while.
 *
 * Age is the only host-independent signal available — no component here can
 * enumerate live projects — and it is safe because an overlay is rebuildable:
 * collecting one too early costs a redundant reinstall.
 */
async function executeGc(
	projectId: string,
	options: { maxAgeDays?: string; dryRun?: boolean },
	out: Output,
): Promise<number> {
	const client = await connectClient(options as never);
	const report = await client.venv.gc(projectId, {
		...(options.maxAgeDays !== undefined ? { maxAgeDays: Number(options.maxAgeDays) } : {}),
		...(options.dryRun ? { dryRun: true } : {}),
	});

	const days = report.maxAgeSeconds / 86400;
	const verb = report.dryRun ? 'would collect' : 'collected';
	out.line(`Idle longer than ${days.toFixed(1)} day(s), of ${report.scanned.toLocaleString()} overlay(s) examined:`);
	for (const row of report.collected) {
		out.line(`  ${verb} ${row.projectId}/${row.envId}  idle ${(row.ageSeconds / 86400).toFixed(1)} day(s)`);
	}
	// Skips and failures are the interesting half: a live project is normal, a failure
	// names the process still holding the overlay. Never summarise these away.
	for (const row of report.skipped) {
		out.line(`  skipped ${row.projectId}  (${row.reason})`);
	}
	for (const row of report.failed) {
		const target = row.envId ? `${row.projectId}/${row.envId}` : row.projectId;
		out.line(`  FAILED  ${target}  ${row.reason}`);
	}
	out.line(`    ${report.collected.length.toLocaleString().padStart(8)} ${verb}, ${report.failed.length} failed`);
	out.result(report);
	return 0;
}

/** Register the `venv` command group on the program. */
export function registerVenvCommands(program: Command): void {
	const venvCmd = program.command('venv').description('Virtual environment overlay operations (on the SERVER)');

	const listCmd = venvCmd
		.command('list [projectId]')
		.description('List environment overlays (no projectId = every overlay on the server, not just yours)')
		.option('--sizes', 'Also report installed size — SLOW: walks every populated site-packages recursively')
		.action(async (projectId: string | undefined, options) => {
			await runCliCommand(options, (out) => executeList(projectId, options, out));
		});
	addConnectionOptions(listCmd);

	const purgeCmd = venvCmd
		.command('purge <projectId> <envId>')
		.description("Empty one environment's site-packages, keeping its compiled inputs")
		.action(async (projectId: string, envId: string, options) => {
			await runCliCommand(options, (out) => executePurge(projectId, envId, options, out));
		});
	addConnectionOptions(purgeCmd);

	const deleteCmd = venvCmd
		.command('delete <projectId> <envId>')
		.description('Remove one environment overlay entirely')
		.action(async (projectId: string, envId: string, options) => {
			await runCliCommand(options, (out) => executeDelete(projectId, envId, options, out));
		});
	addConnectionOptions(deleteCmd);

	const deleteProjectCmd = venvCmd
		.command('delete-project <projectId>')
		.description("Remove a project's whole overlay subtree")
		.action(async (projectId: string, options) => {
			await runCliCommand(options, (out) => executeDeleteProject(projectId, options, out));
		});
	addConnectionOptions(deleteProjectCmd);

	const gcCmd = venvCmd
		.command('gc <projectId>')
		.description('Reclaim overlays nothing has activated for a while (age is the only signal)')
		.option('--max-age-days <days>', 'Idle threshold in days')
		.option('--dry-run', 'Report what would be collected, collect nothing')
		.action(async (projectId: string, options) => {
			await runCliCommand(options, (out) => executeGc(projectId, options, out));
		});
	addConnectionOptions(gcCmd);
}
