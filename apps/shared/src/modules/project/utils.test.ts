// =============================================================================
// MIT License
// Copyright (c) 2026 Aparavi Software AG Inc.
// =============================================================================

/**
 * Tests for the synthetic status a refused run gets.
 *
 * The canvas renders a startup failure out of the host's `statusMap`, and a run the server
 * refuses at creation never becomes a task — so nothing upstream produces a status and the node
 * kept its previous idle line while the message went to the log alone. Measured on a
 * virtual-environment container with a mistyped requirement: the *later* install failure of the
 * same feature rendered on the node correctly, the earlier refusal rendered nowhere.
 *
 * What is pinned here is the shape `NodeStatus` reads to decide "✕ Failed to start" —
 * `completed`, a completed `state`, zero completions, and a non-empty `errors`. Get any one of
 * them wrong and the node silently renders something else, which is the failure this whole
 * change is about.
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

import { startupFailureStatus } from './utils';

const MESSAGE = 'Container "Virtual Environment" has an inadmissible forced requirement: "tabulate=0.10.0".';

describe('startupFailureStatus', () => {
	it('matches every condition NodeStatus reads for "Failed to start"', () => {
		// isCompleted && completedCount === 0 && hasErrors — all three, or the node renders
		// a different block and the refusal is invisible again.
		const status = startupFailureStatus('webhook_1', MESSAGE);
		assert.equal(status.completed, true);
		assert.equal(status.state, 5, 'TASK_STATE.COMPLETED');
		assert.equal(status.completedCount, 0);
		assert.equal(status.failedCount, 0);
		assert.equal(status.errors.length, 1);
	});

	it('carries the server message verbatim, because it already names the cause', () => {
		const status = startupFailureStatus('webhook_1', MESSAGE);
		assert.equal(status.errors[0], MESSAGE);
		assert.equal(status.exitMessage, MESSAGE);
	});

	it('keys itself to the source the run was armed on', () => {
		// The statusMap key and the rendered node are the same id; a mismatch would paint the
		// failure on the wrong node, which is worse than painting it nowhere.
		const status = startupFailureStatus('webhook_1', MESSAGE);
		assert.equal(status.source, 'webhook_1');
	});

	it('invents no metrics', () => {
		// Everything here is a number the renderer may show. A plausible-looking count that
		// never came from a run is worse than a zero.
		const status = startupFailureStatus('webhook_1', MESSAGE);
		for (const value of [status.totalCount, status.totalSize, status.completedSize, status.failedSize, status.rateCount]) {
			assert.equal(value, 0);
		}
		assert.equal(status.serviceUp, false);
	});

	it('reports zero elapsed, from an injected clock', () => {
		const status = startupFailureStatus('webhook_1', MESSAGE, 1_000);
		assert.equal(status.startTime, 1_000);
		assert.equal(status.endTime, 1_000);
	});
});
