// =============================================================================
// MIT License
// Copyright (c) 2026 Aparavi Software AG Inc.
//
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in
// all copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
// SOFTWARE.
// =============================================================================

/**
 * The two decisions behind a virtual-environment container's settings panel.
 *
 * They live apart from the JSX because `shared:test` cannot render — it preloads a stub
 * that returns `undefined` for every named import out of `shell` and `rocketride`, so a
 * rendering test renders `undefined` as a component. Everything that can be a plain
 * function therefore is one, and only the rendered appearance stays a manual check.
 *
 * Both decisions have a measured failure behind them rather than a hypothetical one.
 */

import type { IEnvironment } from '../types';

/** What the panel currently holds for a container's environment. */
export interface IEnvPanelState {
	name: string;
	isolated: boolean;
	forced: string;
}

/**
 * The `config.environment` object to write back.
 *
 * **Spreads the existing environment rather than rebuilding it**, which is the whole point.
 * The panel used to write `{ name, isolated }` — an object literal from two state variables,
 * while the outer `config` was spread — so any third key on `environment` was dropped by the
 * next save through this panel. That was measured before `forced` existed, and it is why
 * teaching the panel about `forced` by name would have been the wrong fix: the key after it
 * would meet the same literal.
 *
 * Blank forced text removes the key instead of storing `''`, so an emptied box round-trips as
 * a container that simply has no forced requirements — which is what the engine reads as an
 * empty digest, naming no file and merging nothing.
 *
 * The text is stored **exactly as typed**. Normalising (line endings, trailing whitespace)
 * belongs to the parent process that digests it; doing it here as well would put two
 * definitions of "the same text" in the system, and only one of them decides rebuilds.
 */
export function buildEnvironmentWriteBack(existing: IEnvironment | undefined, state: IEnvPanelState): IEnvironment {
	const next: IEnvironment = { ...(existing ?? ({} as IEnvironment)), name: state.name, isolated: state.isolated };
	if (state.forced.trim()) {
		next.forced = state.forced;
	} else {
		delete next.forced;
	}
	return next;
}

/**
 * Whether forced requirements would actually be applied — **the engine's question**.
 *
 * The engine's predicate is `bool(environment and environment.get('isolated'))`: a *missing*
 * key is not isolated. Three client surfaces answer this with `environment?.isolated !== false`
 * instead — the canvas badge, and the panel's checkbox state on both init and save — so a
 * hand-authored container carrying `{ name: 'x' }` is badged isolated while the engine reads it
 * as not isolated. That disagreement is shipped and is recorded as `OQ-16`; it is deliberately
 * not repaired here, because flipping the checkbox's default would change whether saving a
 * container makes it isolated, which is not a side effect to bury in a text-box change.
 *
 * What this increment must not do is *inherit* it. A forced field enabled by `!== false` would
 * look live, accept text, and then have the run refused at launch — the worst version of the
 * bug. So the field asks truthiness, like the engine.
 */
export function forcedFieldEnabled(isolated: boolean | undefined): boolean {
	return Boolean(isolated);
}

/**
 * Why the forced field is greyed out, stated as a **consequence** rather than a state.
 *
 * "Isolation is off" would leave the trap intact and look like it had been handled. The
 * sequence that actually bites is not someone typing into a disabled box — it is a container
 * that already holds forced text and a user who clears the isolation checkbox to debug in one
 * process. The text stays in the document, the field greys, and the next launch is refused. So
 * the reason has to say what will happen and what to do about it.
 */
export const FORCED_DISABLED_REASON =
	'Forced requirements only apply to an isolated environment. With isolation off they are ignored and the run is refused — clear the field or re-enable isolated dependencies.';

/**
 * The first inadmissible forced line, or `null` when every line is fine.
 *
 * **This is an ergonomic check, not the security boundary — do not delete the server one on
 * the strength of it.** The partitioner refuses with `packaging.requirements.Requirement`,
 * because documents can be hand-authored, imported, or deployed without ever passing through
 * this panel. What this adds is *when*: without it a mistyped line is only refused at Run, and
 * a partition-time refusal never reaches the canvas — it fails before a task exists, so the
 * node has no status to render and the message lands in the log alone. Telling the author here,
 * in the box they typed it in, is the difference between a typo and a mystery.
 *
 * Deliberately not a PEP 508 implementation, and it must not grow into one: a second parser
 * would drift from the first, and the one that decides is on the server. It catches the shapes
 * that actually get typed — a single `=`, a flag, a path, a URL, a missing name.
 */
export function firstInadmissibleForcedLine(forced: string): string | null {
	for (const raw of forced.split('\n')) {
		const line = raw.split('#')[0].trim();
		if (!line) continue;
		if (line.startsWith('-')) return line;
		if (line.includes('://') || line.startsWith('.') || line.startsWith('/') || /^[A-Za-z]:[\\/]/.test(line)) return line;
		if (!/^[A-Za-z0-9][A-Za-z0-9._-]*/.test(line)) return line;
		// `tabulate=0.10.0` is the one people actually type. A lone `=` is never a PEP 508
		// operator; every real one ends in `=` preceded by another character.
		const body = line.replace(/^[A-Za-z0-9][A-Za-z0-9._-]*(\[[^\]]*\])?\s*/, '');
		if (/(^|[^=!<>~])=(?!=)/.test(body)) return line;
	}
	return null;
}

/** The message shown when {@link firstInadmissibleForcedLine} finds one. */
export function inadmissibleForcedMessage(line: string): string {
	return `Forced requirement "${line}" is not admissible. One PEP 508 requirement per line (for example tabulate==0.9.0); comments and blank lines are allowed. No -r, -c, -e, index flags, paths or URLs. The run would be refused.`;
}
