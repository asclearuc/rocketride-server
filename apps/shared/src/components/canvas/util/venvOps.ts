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
 * Pure decision logic behind the virtual-environment container's destructive
 * operations — the run gate, and what a delete actually removes.
 *
 * It lives apart from the context on purpose. `onBeforeDelete` now guards
 * every delete route on the canvas (menu, keyboard, region select), so its
 * decision is the part a refactor can silently break, while the JSX around it
 * is not. Here it is plain input → output and testable; the `shared:test`
 * runner stubs the `shell` barrel, so anything that renders is not.
 */

import type { ITaskState } from '../types';
import { INodeType } from '../types';

// =============================================================================
// Structural inputs
// =============================================================================

/**
 * The task-status fields the run gate reads.
 * Structurally satisfied by `ITaskStatus`.
 */
export interface IRunStatus {
	/** Lifecycle state — a `TASK_STATE` value. */
	state: number;
	/** True once the task has finished, however it finished. */
	completed: boolean;
}

/**
 * The node fields the delete planner reads.
 * Structurally satisfied by `FlowNode`.
 */
export interface IDeletableNode {
	id: string;
	type?: string;
	parentId?: string;
	position: { x: number; y: number };
}

/**
 * The edge fields the delete planner reads.
 * Structurally satisfied by ReactFlow's `Edge`.
 */
export interface IDeletableEdge {
	id: string;
	source: string;
	target: string;
}

// =============================================================================
// Run gate
// =============================================================================

/** Fails to compile unless the operand is exactly `true`. */
type Assert<T extends true> = T;

/**
 * Task states that still hold an environment's files open.
 *
 * Spelled as literals because `shared:test` stubs the `shell` barrel that
 * re-exports `ITaskState`, so reading the enum there yields `undefined`.
 * {@link _RunningStatesPinned} ties the literals back to the enum at compile
 * time, so the duplication cannot drift in silence.
 */
const RUNNING_STATES: readonly number[] = [1, 2, 3, 4];

/** STARTING, INITIALIZING, RUNNING, STOPPING. Type-only — erased at runtime. */
type _RunningStatesPinned = Assert<[ITaskState.STARTING, ITaskState.INITIALIZING, ITaskState.RUNNING, ITaskState.STOPPING] extends readonly [1, 2, 3, 4] ? true : false>;

/**
 * Whether any task of this project still holds the overlay's files.
 *
 * The engine refuses purge and delete_env per **project**, so this asks the
 * same question across every status rather than about one node.
 *
 * Deliberately not `isPipelineRunning`, which reads
 * `state !== COMPLETED && state !== CANCELLED` and so counts `NONE`
 * ("no resources allocated") as running: it would grey the menu out before
 * anything had ever run. A failed run is the mirror case — it reports through
 * `completed`, not through a state of its own, and it is exactly when a user
 * wants the overlay back. `STOPPING` does count: teardown still holds files.
 *
 * A predicate looser than the engine's merely delays a refusal the user can
 * read; one tighter than the engine's greys out an operation the engine would
 * have honoured, and leaves nothing on screen to explain why.
 *
 * @param taskStatuses - Per-node status map from the host, if any.
 * @returns True while any task is starting, initializing, running or stopping.
 */
export function isProjectRunning(taskStatuses?: Record<string, IRunStatus>): boolean {
	return Object.values(taskStatuses ?? {}).some((status) => RUNNING_STATES.includes(status.state) && !status.completed);
}

// =============================================================================
// Delete planning
// =============================================================================

/**
 * Ids of the virtual-environment containers inside a pending deletion set.
 *
 * Detects by node type, never by sniffing `config.environment`: the document
 * stores the container as a `group` carrying that config, and the canvas type
 * is the one place the distinction is already resolved.
 *
 * @param nodes - The nodes ReactFlow is about to remove.
 * @returns Their container ids, in the order given. Empty when there are none,
 *          which is the fast path every ordinary delete takes.
 */
export function findVenvContainerIds(nodes: readonly IDeletableNode[]): string[] {
	return nodes.filter((node) => node.type === INodeType.VirtualEnv).map((node) => node.id);
}

/**
 * Lifts the members of the given containers back to the top level.
 *
 * Their stored position is relative to the container, so it becomes absolute
 * on the way out; `parentId` and `extent` are cleared so ReactFlow stops
 * treating them as contained.
 *
 * Call this **before** the removal lands, so a member never spends a render
 * referencing a parent that is already gone.
 *
 * @param nodes        - Every node on the canvas, not just the pending set.
 * @param containerIds - Containers whose members are to be promoted.
 * @returns A new array; untouched nodes keep their identity.
 */
export function promoteContainerMembers<N extends IDeletableNode>(nodes: readonly N[], containerIds: readonly string[]): N[] {
	const containers = new Set(containerIds);
	if (containers.size === 0) return [...nodes];

	const parentById = new Map(nodes.filter((node) => containers.has(node.id)).map((node) => [node.id, node]));

	return nodes.map((node) => {
		if (node.parentId == null || !containers.has(node.parentId)) return node;
		const parent = parentById.get(node.parentId);
		return {
			...node,
			position: {
				x: (parent?.position.x ?? 0) + node.position.x,
				y: (parent?.position.y ?? 0) + node.position.y,
			},
			parentId: undefined,
			extent: undefined,
		};
	});
}

/**
 * Narrows a pending deletion set to what the user actually confirmed.
 *
 * ReactFlow cascades: every node whose `parentId` matches one being removed is
 * already in the set, connected edges with it. So the two answers map onto the
 * set by **filtering**, never by adding.
 *
 * Keeping the members also keeps the edges between them — deleting a
 * connection the user asked to preserve would be the same silent damage as
 * deleting the node. An edge is dropped only when an endpoint of it really
 * goes. (An edge the user had *separately* selected inside the same gesture is
 * spared by that rule; the conservative direction is the right one here.)
 *
 * @param pending       - The set ReactFlow handed to `onBeforeDelete`.
 * @param containerIds  - The venv containers in that set.
 * @param deleteMembers - The user's answer: delete the members, or ungroup.
 * @returns The set to remove. Cancelling is not modelled here — the caller
 *          returns `false` and nothing at all happens.
 */
export function applyVenvDeleteAnswer<N extends IDeletableNode, E extends IDeletableEdge>(pending: { nodes: readonly N[]; edges: readonly E[] }, containerIds: readonly string[], deleteMembers: boolean): { nodes: N[]; edges: E[] } {
	if (deleteMembers) return { nodes: [...pending.nodes], edges: [...pending.edges] };

	const containers = new Set(containerIds);
	const memberIds = new Set(pending.nodes.filter((node) => node.parentId != null && containers.has(node.parentId)).map((node) => node.id));

	const nodes = pending.nodes.filter((node) => !memberIds.has(node.id));
	const removedIds = new Set(nodes.map((node) => node.id));
	const edges = pending.edges.filter((edge) => removedIds.has(edge.source) || removedIds.has(edge.target));

	return { nodes, edges };
}
