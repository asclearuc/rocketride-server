// =============================================================================
// MIT License
// Copyright (c) 2026 Aparavi Software AG Inc.
// =============================================================================

/**
 * Tests for the virtual-environment container's destructive decisions.
 *
 * `onBeforeDelete` guards every delete route on the canvas — overflow menu,
 * Delete key, region select — so getting its answer wrong either strands a
 * container's members without a parent or removes work the user asked to keep.
 * The run gate matters for the mirror reason: too tight and it greys out an
 * operation the engine would have honoured, with nothing on screen to say why.
 *
 * Both are pinned here rather than around the JSX, which the `shared:test`
 * runner cannot render anyway (it stubs the `shell` barrel).
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

import { isProjectRunning, findVenvContainerIds, promoteContainerMembers, applyVenvDeleteAnswer } from './venvOps';
import type { IDeletableEdge, IDeletableNode, IRunStatus } from './venvOps';
import { INodeType } from '../types';

// =============================================================================
// Helpers
// =============================================================================

/** TASK_STATE values, spelled out so a reader need not decode the numbers. */
const STATE = { NONE: 0, STARTING: 1, INITIALIZING: 2, RUNNING: 3, STOPPING: 4, COMPLETED: 5, CANCELLED: 6 } as const;

/** Minimal task status. */
const status = (state: number, completed = false): IRunStatus => ({ state, completed });

/** Minimal canvas node. */
const node = (id: string, type: string, extra: Partial<IDeletableNode> = {}): IDeletableNode => ({
	id,
	type,
	position: { x: 0, y: 0 },
	...extra,
});

/** Minimal edge. */
const edge = (id: string, source: string, target: string): IDeletableEdge => ({ id, source, target });

// =============================================================================
// Run gate
// =============================================================================

describe('isProjectRunning', () => {
	it('reports no run for an absent or empty status map', () => {
		assert.equal(isProjectRunning(undefined), false);
		assert.equal(isProjectRunning({}), false);
	});

	it('does not count NONE as running', () => {
		// "Initial state — no resources allocated". Counting it would grey the
		// operation out before anything had ever run — the isPipelineRunning bug.
		assert.equal(isProjectRunning({ a: status(STATE.NONE) }), false);
	});

	it('counts every state that still holds the overlay open', () => {
		assert.equal(isProjectRunning({ a: status(STATE.STARTING) }), true);
		assert.equal(isProjectRunning({ a: status(STATE.INITIALIZING) }), true);
		assert.equal(isProjectRunning({ a: status(STATE.RUNNING) }), true);
		// Teardown still holds files — which is the whole reason for the gate.
		assert.equal(isProjectRunning({ a: status(STATE.STOPPING) }), true);
	});

	it('does not count a finished run, however it finished', () => {
		assert.equal(isProjectRunning({ a: status(STATE.COMPLETED) }), false);
		assert.equal(isProjectRunning({ a: status(STATE.CANCELLED) }), false);
		// A failure reports through `completed`, not through a state of its own,
		// and it is exactly when a user wants the overlay reclaimed.
		assert.equal(isProjectRunning({ a: status(STATE.RUNNING, true) }), false);
	});

	it('asks about the whole project, not one node', () => {
		// A run on a different branch of the same pipeline must still gate:
		// the engine refuses per project.
		assert.equal(isProjectRunning({ a: status(STATE.COMPLETED), b: status(STATE.RUNNING) }), true);
	});
});

// =============================================================================
// Container detection
// =============================================================================

describe('findVenvContainerIds', () => {
	it('finds virtual-environment containers by node type', () => {
		const pending = [node('venv_1', INodeType.VirtualEnv), node('llm_1', INodeType.Default)];
		assert.deepEqual(findVenvContainerIds(pending), ['venv_1']);
	});

	it('does not mistake a plain group for one', () => {
		// Both are containers; only one owns an environment on disk.
		const pending = [node('group_1', INodeType.Group), node('note_1', INodeType.Annotation)];
		assert.deepEqual(findVenvContainerIds(pending), []);
	});

	it('returns nothing for an ordinary deletion', () => {
		// The fast path: this hook fires for every delete on the canvas, and
		// ordinary ones must not pay for the container's questions.
		assert.deepEqual(findVenvContainerIds([node('llm_1', INodeType.Default)]), []);
		assert.deepEqual(findVenvContainerIds([]), []);
	});
});

// =============================================================================
// Ungrouping
// =============================================================================

describe('promoteContainerMembers', () => {
	it('restores absolute positions and clears containment', () => {
		const nodes = [node('venv_1', INodeType.VirtualEnv, { position: { x: 100, y: 50 } }), node('llm_1', INodeType.Default, { parentId: 'venv_1', position: { x: 10, y: 20 } })];

		const promoted = promoteContainerMembers(nodes, ['venv_1']);
		const member = promoted.find((n) => n.id === 'llm_1')!;

		assert.deepEqual(member.position, { x: 110, y: 70 });
		assert.equal(member.parentId, undefined);
	});

	it('leaves members of other containers alone', () => {
		const nodes = [node('venv_1', INodeType.VirtualEnv, { position: { x: 100, y: 50 } }), node('group_1', INodeType.Group, { position: { x: 400, y: 400 } }), node('other_1', INodeType.Default, { parentId: 'group_1', position: { x: 5, y: 5 } })];

		const promoted = promoteContainerMembers(nodes, ['venv_1']);
		const untouched = promoted.find((n) => n.id === 'other_1')!;

		assert.equal(untouched, nodes[2], 'an untouched node keeps its identity');
		assert.equal(untouched.parentId, 'group_1');
	});

	it('is a no-op when nothing is being ungrouped', () => {
		// The cancel path relies on this: nothing may move when the user backs out.
		const nodes = [node('venv_1', INodeType.VirtualEnv), node('llm_1', INodeType.Default, { parentId: 'venv_1' })];
		assert.deepEqual(promoteContainerMembers(nodes, []), nodes);
	});
});

// =============================================================================
// The delete decision
// =============================================================================

describe('applyVenvDeleteAnswer', () => {
	/** A container with two connected members, as ReactFlow cascades it. */
	const pending = {
		nodes: [node('venv_1', INodeType.VirtualEnv), node('llm_1', INodeType.Default, { parentId: 'venv_1' }), node('out_1', INodeType.Default, { parentId: 'venv_1' })],
		edges: [edge('e_inner', 'llm_1', 'out_1')],
	};

	it('removes everything when the members go too', () => {
		const result = applyVenvDeleteAnswer(pending, ['venv_1'], true);
		assert.deepEqual(
			result.nodes.map((n) => n.id),
			['venv_1', 'llm_1', 'out_1']
		);
		assert.deepEqual(
			result.edges.map((e) => e.id),
			['e_inner']
		);
	});

	it('keeps the members when the answer is ungroup', () => {
		const result = applyVenvDeleteAnswer(pending, ['venv_1'], false);
		assert.deepEqual(
			result.nodes.map((n) => n.id),
			['venv_1']
		);
	});

	it('keeps the connections between members it keeps', () => {
		// "Ungroup and keep them" that silently drops their wiring is not keeping
		// them. ReactFlow put this edge in the set only because its endpoints were.
		const result = applyVenvDeleteAnswer(pending, ['venv_1'], false);
		assert.deepEqual(result.edges, []);
	});

	it('still removes edges that reach a node which really goes', () => {
		const withOutside = {
			nodes: pending.nodes,
			edges: [edge('e_inner', 'llm_1', 'out_1'), edge('e_container', 'venv_1', 'llm_1')],
		};
		const result = applyVenvDeleteAnswer(withOutside, ['venv_1'], false);
		assert.deepEqual(
			result.edges.map((e) => e.id),
			['e_container']
		);
	});

	it('does not hold ordinary nodes hostage to the container question', () => {
		// A region select over a container plus loose nodes: one dialog, one
		// answer, and the loose nodes are deleted either way.
		const mixed = {
			nodes: [...pending.nodes, node('loose_1', INodeType.Default)],
			edges: [],
		};
		const result = applyVenvDeleteAnswer(mixed, ['venv_1'], false);
		assert.deepEqual(
			result.nodes.map((n) => n.id),
			['venv_1', 'loose_1']
		);
	});
});
