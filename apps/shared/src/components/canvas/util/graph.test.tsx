// =============================================================================
// MIT License
// Copyright (c) 2026 Aparavi Software AG Inc.
// =============================================================================

/**
 * Round-trip tests for the virtual-environment container.
 *
 * The container is a canvas type that is stored as a `group` carrying
 * `config.environment`. That split is the whole compatibility story — an editor
 * without this container must still read the structure — so the mapping in both
 * directions is worth pinning rather than trusting.
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

import { getNodesFromProject, getProjectComponents } from './graph';
import { INodeType, isContainerType } from '../types';
import type { INode, IProject } from '../types';

// =============================================================================
// Helpers
// =============================================================================

/** Minimal serialised component; `ui` fields the loader backfills are omitted. */
const component = (id: string, overrides: Record<string, unknown> = {}) => ({
	id,
	provider: 'default',
	config: {},
	ui: { position: { x: 0, y: 0 }, measured: { width: 150, height: 36 }, nodeType: INodeType.Default },
	...overrides,
});

/** Minimal canvas node. */
const node = (id: string, type: string, extra: Partial<INode> = {}): INode => ({
	id,
	type,
	position: { x: 0, y: 0 },
	data: { provider: 'default', config: {} },
	...extra,
});

// =============================================================================
// Tests
// =============================================================================

describe('isContainerType', () => {
	it('accepts both container types and nothing else', () => {
		assert.equal(isContainerType(INodeType.Group), true);
		assert.equal(isContainerType(INodeType.VirtualEnv), true);
		assert.equal(isContainerType(INodeType.Default), false);
		assert.equal(isContainerType(INodeType.Annotation), false);
		assert.equal(isContainerType(undefined), false);
	});
});

describe('loading a document', () => {
	it('renders a group carrying an environment as a virtual environment', () => {
		const project = {
			components: [
				component('venv_1', {
					config: { environment: { name: 'vision', isolated: true } },
					ui: { position: { x: 10, y: 10 }, measured: { width: 300, height: 200 }, nodeType: INodeType.Group },
				}),
			],
		} as unknown as IProject;

		const nodes = getNodesFromProject(project);

		assert.equal(nodes[0].type, INodeType.VirtualEnv);
	});

	it('leaves a plain group alone', () => {
		const project = { components: [component('group_1', { ui: { position: { x: 0, y: 0 }, measured: { width: 300, height: 200 }, nodeType: INodeType.Group } })] } as unknown as IProject;

		assert.equal(getNodesFromProject(project)[0].type, INodeType.Group);
	});

	it('keeps the environment on the node config while stripping the nested pipeline', () => {
		const project = {
			components: [
				component('venv_1', {
					config: { environment: { name: 'vision', isolated: true }, pipeline: { components: [component('child_1', { ui: { position: { x: 5, y: 5 }, measured: { width: 150, height: 36 }, nodeType: INodeType.Default, parentId: 'venv_1' } })] } },
					ui: { position: { x: 0, y: 0 }, measured: { width: 300, height: 200 }, nodeType: INodeType.Group },
				}),
			],
		} as unknown as IProject;

		const nodes = getNodesFromProject(project);

		assert.deepEqual(nodes[0].data.config.environment, { name: 'vision', isolated: true });
		assert.equal(nodes[0].data.config.pipeline, undefined, 'members live on the canvas, not inside the container config');
		assert.equal(nodes.length, 2, 'the member is lifted onto the flat canvas list');
		assert.equal(nodes[1].parentId, 'venv_1');
	});
});

describe('saving a document', () => {
	it('writes a virtual environment as a group so older editors keep the structure', () => {
		const nodes = [node('venv_1', INodeType.VirtualEnv, { data: { provider: 'default', config: { environment: { name: 'vision', isolated: true } } } })];

		const [saved] = getProjectComponents(nodes);

		assert.equal(saved.ui?.nodeType, INodeType.Group);
		assert.deepEqual(saved.config.environment, { name: 'vision', isolated: true });
	});

	it('nests the members of a virtual environment', () => {
		const nodes = [node('venv_1', INodeType.VirtualEnv, { data: { provider: 'default', config: { environment: { name: 'vision', isolated: true } } } }), node('member_1', INodeType.Default, { parentId: 'venv_1' })];

		const [saved] = getProjectComponents(nodes);

		assert.equal(saved.config.pipeline?.components?.length, 1);
		assert.equal(saved.config.pipeline?.components?.[0].id, 'member_1');
	});
});

describe('container dimensions', () => {
	it('restores an explicit size for containers so they do not reopen collapsed', () => {
		const project = {
			components: [
				component('venv_1', {
					config: { environment: { name: 'vision', isolated: true } },
					ui: { position: { x: 0, y: 0 }, measured: { width: 373, height: 269 }, nodeType: INodeType.Group },
				}),
				component('group_1', { ui: { position: { x: 0, y: 0 }, measured: { width: 300, height: 200 }, nodeType: INodeType.Group } }),
				component('plain_1'),
			],
		} as unknown as IProject;

		const [venv, group, plain] = getNodesFromProject(project);

		assert.deepEqual([venv.width, venv.height], [373, 269]);
		assert.deepEqual([group.width, group.height], [300, 200], 'plain groups are resizable too');
		assert.equal(plain.width, undefined, 'ordinary nodes keep sizing themselves to their content');
	});

	it('persists the size a resize just produced', () => {
		// After a NodeResizer drag the new size is on width/height; `measured` still
		// holds what ReactFlow last measured.
		const nodes = [node('venv_1', INodeType.VirtualEnv, { width: 420, height: 300, measured: { width: 240, height: 140 } })];

		const [saved] = getProjectComponents(nodes);

		assert.deepEqual(saved.ui?.measured, { width: 420, height: 300 });
	});

	it('survives a size round trip', () => {
		const nodes = [node('venv_1', INodeType.VirtualEnv, { width: 373, height: 269, data: { provider: 'default', config: { environment: { name: 'vision', isolated: true } } } })];

		const [reloaded] = getNodesFromProject({ components: getProjectComponents(nodes) } as unknown as IProject);

		assert.deepEqual([reloaded.width, reloaded.height], [373, 269]);
	});
});

describe('round trip', () => {
	it('survives canvas → document → canvas unchanged', () => {
		const original = [node('venv_1', INodeType.VirtualEnv, { data: { provider: 'default', config: { environment: { name: 'vision', isolated: true } } } }), node('member_1', INodeType.Default, { parentId: 'venv_1' }), node('group_1', INodeType.Group)];

		const reloaded = getNodesFromProject({ components: getProjectComponents(original) } as unknown as IProject);

		assert.deepEqual(
			reloaded.map((n) => [n.id, n.type, n.parentId]),
			[
				['venv_1', INodeType.VirtualEnv, undefined],
				['member_1', INodeType.Default, 'venv_1'],
				['group_1', INodeType.Group, undefined],
			]
		);
	});
});
