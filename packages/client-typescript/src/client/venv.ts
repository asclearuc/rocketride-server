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
 * Virtual-environment API namespace for the RocketRide TypeScript SDK.
 *
 * Enumerates and reclaims the per-environment `site-packages` overlays via the
 * `rrext_venv` DAP command (dispatched by `subcommand`).
 *
 * **These act on the server's disk, not the caller's.** The engine resolves the
 * overlay root from its own executable's directory, so against a local engine
 * this is your machine and against a remote or cloud engine it emphatically is
 * not.
 *
 * Two shapes of "no": a destructive call returns `false`/`0` when the target
 * simply was not there — idempotent success, not a failure — and *throws* when
 * the engine refuses, most often because a run of that project is still live.
 * Refusal messages come from the engine verbatim; do not rewrite them.
 */

import type { RocketRideClient } from './client.js';
import type { VenvListOptions, VenvOverlay, VenvScope } from './types/venv.js';

// =============================================================================
// VENV API CLASS
// =============================================================================

/**
 * Typed wrapper around the `rrext_venv` DAP command and its subcommands.
 *
 * Accessed via `client.venv` — not instantiated directly. All methods delegate
 * to {@link RocketRideClient.call}, which handles envelope construction, error
 * detection, and tracing.
 */
export class VenvApi {
	/** @param client - The parent RocketRideClient that owns this namespace. */
	constructor(private client: RocketRideClient) {}

	// =========================================================================
	// READ
	// =========================================================================

	/**
	 * Lists environment overlays on the server.
	 *
	 * With no `projectId` this enumerates **every** overlay on the machine, not
	 * "yours" — overlays are disk state keyed by project id and nothing ties one
	 * to an account. That is the point of the unfiltered form: it shows the ones
	 * you have forgotten about.
	 *
	 * @param options - Optional project filter, `sizes` opt-in, and team scope.
	 * @returns One row per overlay, newest layout order (project, then env).
	 */
	async list(options: VenvListOptions = {}): Promise<VenvOverlay[]> {
		const body = await this.client.call<{ environments: VenvOverlay[] }>('rrext_venv', {
			subcommand: 'list',
			...(options.projectId ? { projectId: options.projectId } : {}),
			...(options.sizes ? { sizes: true } : {}),
			...(options.teamId ? { teamId: options.teamId } : {}),
		});
		return body.environments ?? [];
	}

	// =========================================================================
	// RECLAIM
	// =========================================================================

	/**
	 * Empties one environment's `site-packages`, keeping its compiled inputs.
	 *
	 * `combined.txt` and `constraints.txt` survive, but the next run still
	 * recompiles: purge drops `requirements.hash` first, so a mid-wipe failure
	 * can never leave a half-emptied overlay marked as installed.
	 *
	 * @param projectId - Pipeline `project_id`, or the on-disk name from {@link list}.
	 * @param envId - Container node id, or the on-disk name from {@link list}.
	 * @param scope - Optional team scope for the permission check.
	 * @returns True when packages were removed; **false when the overlay did not
	 *   exist**, which is success, not failure.
	 * @throws When a run of that project is live, or the caller lacks `task.control`.
	 */
	async purge(projectId: string, envId: string, scope: VenvScope = {}): Promise<boolean> {
		const body = await this.client.call<{ purged: boolean }>('rrext_venv', {
			subcommand: 'purge',
			projectId,
			envId,
			...(scope.teamId ? { teamId: scope.teamId } : {}),
		});
		return body.purged;
	}

	/**
	 * Removes one environment overlay entirely, compiled inputs included.
	 *
	 * @param projectId - Pipeline `project_id`, or the on-disk name from {@link list}.
	 * @param envId - Container node id, or the on-disk name from {@link list}.
	 * @param scope - Optional team scope for the permission check.
	 * @returns True when the overlay was removed; **false when it did not exist**.
	 * @throws When a run of that project is live, or the caller lacks `task.control`.
	 */
	async deleteEnv(projectId: string, envId: string, scope: VenvScope = {}): Promise<boolean> {
		const body = await this.client.call<{ deleted: boolean }>('rrext_venv', {
			subcommand: 'delete_env',
			projectId,
			envId,
			...(scope.teamId ? { teamId: scope.teamId } : {}),
		});
		return body.deleted;
	}

	/**
	 * Removes a project's whole `venvs/<projectId>/` **subtree** — the directory
	 * itself, not merely its contents.
	 *
	 * Despite the name this deletes no project and no pipeline: it reclaims the
	 * disk that project's environments occupy. The pipeline document is
	 * untouched and the next run rebuilds whatever it needs.
	 *
	 * @param projectId - Pipeline `project_id`, or the on-disk name from {@link list}.
	 * @param scope - Optional team scope for the permission check.
	 * @returns How many environments were removed; **0 when the project had none**.
	 * @throws When a run of that project is live, or the caller lacks `task.control`.
	 */
	async deleteProject(projectId: string, scope: VenvScope = {}): Promise<number> {
		const body = await this.client.call<{ deletedEnvironments: number }>('rrext_venv', {
			subcommand: 'delete_project',
			projectId,
			...(scope.teamId ? { teamId: scope.teamId } : {}),
		});
		return body.deletedEnvironments;
	}
}
