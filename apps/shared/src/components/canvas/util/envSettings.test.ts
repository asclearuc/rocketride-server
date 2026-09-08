// =============================================================================
// MIT License
// Copyright (c) 2026 Aparavi Software AG Inc.
// =============================================================================

/**
 * Tests for the virtual-environment settings panel's two decisions.
 *
 * The write-back is the one with a measured defect behind it: the panel rebuilt
 * `config.environment` from two state variables while the outer `config` was spread, so
 * any third key on it was dropped by the next save through this panel. That was found by
 * asking whether an unknown key survives a round trip — the answer was no, and it is why
 * the forced field would have erased itself on the very first save through the panel that
 * offers it.
 *
 * The enabled-state rule is the mirror: three client surfaces answer "is this isolated?"
 * with `!== false` while the engine uses truthiness, so a field keyed on the client's
 * answer would look live, accept text, and then have the run refused at launch.
 *
 * Pinned here rather than around the JSX, which `shared:test` cannot render (it stubs the
 * `shell` and `rocketride` barrels).
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

import { buildEnvironmentWriteBack, forcedFieldEnabled, firstInadmissibleForcedLine, inadmissibleForcedMessage, FORCED_DISABLED_REASON } from './envSettings';
import type { IEnvironment } from '../types';

describe('buildEnvironmentWriteBack', () => {
	it('keeps forced text across a save', () => {
		const next = buildEnvironmentWriteBack({ name: 'ocr', isolated: true }, { name: 'ocr', isolated: true, forced: 'tabulate==0.9.0\n' });
		assert.equal(next.forced, 'tabulate==0.9.0\n');
	});

	it('stores the text exactly as typed, leaving normalisation to the engine', () => {
		// Two definitions of "the same text" would be one too many: only the parent's
		// normalisation feeds the digest that decides whether the environment rebuilds.
		const raw = '  tabulate==0.9.0  \r\n\r\n';
		const next = buildEnvironmentWriteBack(undefined, { name: 'ocr', isolated: true, forced: raw });
		assert.equal(next.forced, raw);
	});

	it('removes the key when the box is emptied rather than storing an empty string', () => {
		const next = buildEnvironmentWriteBack({ name: 'ocr', isolated: true, forced: 'numpy\n' }, { name: 'ocr', isolated: true, forced: '   \n' });
		assert.equal('forced' in next, false);
	});

	it('preserves a key it has never heard of — the defect that made this a function', () => {
		// The write-back used to be `{ name, isolated }`, so this key vanished on save. The
		// general repair matters more than the specific one: teaching the panel about
		// `forced` by name would leave the next key added by anyone meeting the same literal.
		const existing = { name: 'ocr', isolated: true, somethingAddedLater: 42 } as unknown as IEnvironment;
		const next = buildEnvironmentWriteBack(existing, { name: 'ocr', isolated: true, forced: '' });
		assert.equal((next as unknown as Record<string, unknown>).somethingAddedLater, 42);
	});

	it('still writes name and isolation from the panel, not from the old object', () => {
		const next = buildEnvironmentWriteBack({ name: 'old', isolated: true }, { name: 'new', isolated: false, forced: '' });
		assert.equal(next.name, 'new');
		assert.equal(next.isolated, false);
	});

	it('works when the container has no environment block yet', () => {
		const next = buildEnvironmentWriteBack(undefined, { name: 'ocr', isolated: true, forced: 'numpy\n' });
		assert.deepEqual(next, { name: 'ocr', isolated: true, forced: 'numpy\n' });
	});
});

describe('forcedFieldEnabled', () => {
	it('asks the engine question — truthiness, not `!== false`', () => {
		// `bool(environment and environment.get('isolated'))`: a missing key is NOT isolated.
		// The canvas badge and the panel checkbox both answer `!== false` here (OQ-16), which
		// is exactly what this field must not inherit.
		assert.equal(forcedFieldEnabled(undefined), false);
		assert.equal(forcedFieldEnabled(false), false);
		assert.equal(forcedFieldEnabled(true), true);
	});

	it('has a reason that states the consequence, not the state', () => {
		// "Isolation is off" would leave the trap intact and look handled. The sequence that
		// bites is text already present, then the checkbox cleared.
		assert.match(FORCED_DISABLED_REASON, /refused/);
		assert.match(FORCED_DISABLED_REASON, /clear the field|re-enable/);
	});
});

describe('firstInadmissibleForcedLine', () => {
	it('catches the one people actually type: a single = instead of ==', () => {
		// The live case. Without this the run is refused at partition time, and a partition-time
		// refusal never reaches the canvas — it fails before a task exists, so there is no node
		// status to render it in and the message lands only in the log.
		assert.equal(firstInadmissibleForcedLine('tabulate=0.10.0\n'), 'tabulate=0.10.0');
	});

	it('accepts every operator PEP 508 really has', () => {
		const good = ['tabulate==0.9.0', 'torch>=2.1', 'torch<=2.4', 'six!=1.16.0', 'numpy~=1.26', 'numpy', 'torch[cuda]>=2.1,<3.0', 'torch==2.10.0+cu128 ; sys_platform != "darwin"'];
		for (const line of good) assert.equal(firstInadmissibleForcedLine(line + '\n'), null, line);
	});

	it('catches flags, paths and URLs', () => {
		for (const bad of ['-r other.txt', '-c c.txt', '-e .', '--index-url https://x/simple', './pkg.whl', '/tmp/pkg.whl', 'C:/tmp/pkg.whl', 'pkg @ https://x/y.tgz']) {
			assert.equal(firstInadmissibleForcedLine(bad + '\n'), bad, bad);
		}
	});

	it('ignores comments and blank lines', () => {
		assert.equal(firstInadmissibleForcedLine('# a note\n\n   \ntabulate==0.9.0\n'), null);
		assert.equal(firstInadmissibleForcedLine('tabulate==0.9.0  # trailing\n'), null);
	});

	it('reports the first bad line, not the last', () => {
		assert.equal(firstInadmissibleForcedLine('numpy\ntabulate=0.9.0\n-r x.txt\n'), 'tabulate=0.9.0');
	});

	it('says what to type and that the run would be refused', () => {
		// A message naming only the offence teaches nothing; the example is the useful half.
		const message = inadmissibleForcedMessage('tabulate=0.10.0');
		assert.match(message, /tabulate=0\.10\.0/);
		assert.match(message, /tabulate==0\.9\.0/);
		assert.match(message, /refused/);
	});
});
