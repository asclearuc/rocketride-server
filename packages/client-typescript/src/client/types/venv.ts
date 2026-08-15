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
 * Virtual-environment overlay type definitions for the RocketRide TypeScript SDK.
 *
 * An **overlay** is the on-disk `site-packages` tree one environment of one
 * pipeline installs into, living under `<server>/venvs/<projectId>/<envId>/`.
 * It is a rebuildable cache: the requirements themselves live in the pipeline
 * document, so reclaiming an overlay costs the next run's install time and
 * nothing else.
 *
 * Not to be confused with {@link PipelineEnvironment}, which is the container's
 * block *in the document* (`name` + `isolated`). This file describes the disk.
 */

// =============================================================================
// VENV TYPES
// =============================================================================

/**
 * One environment overlay on disk, as reported by `client.venv.list()`.
 *
 * Travels on the wire under the key `environments`; the type is named for what
 * a row is rather than for the wire key it arrives in.
 */
export interface VenvOverlay {
	/** Project directory name under `venvs/` — the shortened form of the pipeline's `project_id`. */
	projectId: string;

	/** Environment directory name — `main`, or the shortened form of a container node's id. */
	envId: string;

	/** True when a `requirements.hash` is present, i.e. the overlay has been installed into. */
	installed: boolean;

	/** Size of `site-packages` in bytes. Present only when `sizes` was requested. */
	bytes?: number;
}

/**
 * Optional scope for a venv call.
 *
 * A team id resolves the permission check against that team instead of the
 * caller's default context. It is a caller-asserted scope check and nothing
 * more: overlays are disk state on the server's machine, keyed by project id,
 * and are **not** team-owned.
 */
export interface VenvScope {
	/** Resolve the permission against this team rather than the default context. */
	teamId?: string;
}

/**
 * Parameters for `client.venv.list()`.
 */
export interface VenvListOptions extends VenvScope {
	/** Limit the listing to one project. Omitted, every overlay on the server is returned. */
	projectId?: string;

	/**
	 * Also report `bytes` per overlay.
	 *
	 * Opt-in because it is expensive: sizing walks every populated
	 * `site-packages` recursively, which is on the order of half a million
	 * `stat` calls on a well-used machine. Leave it off unless the caller is
	 * deciding what to reclaim.
	 */
	sizes?: boolean;
}

/**
 * Parameters for `client.venv.gc()`. The project is a positional argument, not
 * part of this bag: it is required, and an options object hides that.
 */
export interface VenvGcOptions extends VenvScope {
	/**
	 * Collect overlays idle longer than this. Omitted, the server's own
	 * threshold applies. The server enforces a minimum age on top, so `0` does
	 * not mean "everything" — read {@link VenvGcReport.maxAgeSeconds} back to
	 * see what was actually applied.
	 */
	maxAgeDays?: number;

	/** Report what would be collected without removing anything. */
	dryRun?: boolean;
}

/** One overlay `gc` reclaimed, or would reclaim under `dryRun`. */
export interface VenvGcCollected {
	projectId: string;
	envId: string;

	/** Age at the moment of the pass, measured from the newest activity signal. */
	ageSeconds: number;
}

/** One project `gc` left alone. Project-level: there is no environment to name. */
export interface VenvGcSkipped {
	projectId: string;

	/**
	 * `'live'` when the project is in use; otherwise the reason the server could
	 * not tell, which is treated the same way — skipping is the safe answer.
	 */
	reason: string;
}

/** One overlay, or one project, `gc` could not reclaim. */
export interface VenvGcFailed {
	projectId: string;

	/**
	 * Absent when the failure was project-level, e.g. an unreadable project
	 * directory: there is no single environment to blame. Optional for that
	 * reason and not by oversight.
	 */
	envId?: string;

	/**
	 * The server's own message, carried verbatim because it names the cause —
	 * typically a process still holding a file in the overlay open.
	 */
	reason: string;
}

/**
 * The result of `client.venv.gc()`.
 *
 * Note this is a report rather than the boolean its destructive siblings
 * return: `gc` does not refuse over a live project, it reports one as skipped.
 */
export interface VenvGcReport {
	dryRun: boolean;

	/**
	 * The threshold actually applied, in seconds, **after** the server's minimum
	 * age floor. Asking for 0 and reading this back is how you see the floor.
	 */
	maxAgeSeconds: number;

	/** Overlays examined. Environments of a skipped project are not examined. */
	scanned: number;

	collected: VenvGcCollected[];
	skipped: VenvGcSkipped[];
	failed: VenvGcFailed[];
}
