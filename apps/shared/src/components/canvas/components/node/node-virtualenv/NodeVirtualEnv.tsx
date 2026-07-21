// =============================================================================
// MIT License
// Copyright (c) 2026 Aparavi Software AG Inc.
// =============================================================================

/**
 * NodeVirtualEnv — container whose members resolve their dependencies in isolation.
 *
 * Shares the group's mechanics — resizable, holds children by `parentId`, nests them
 * into `config.pipeline.components` on save — but is its own node type so that the
 * isolation treatment, and everything the environment will grow later, does not
 * accumulate as branches inside the plain group.
 *
 * Canvas-only type: the document stores it as a `group` carrying `config.environment`
 * (see `util/graph.ts`), so an editor that predates this container still reads the
 * structure instead of dropping the members.
 */

import React, { ReactElement, memo } from 'react';
import { NodeResizer, useStore, useNodeId } from '@xyflow/react';

import { INodeData } from '../../../types';
import type { IEnvironment } from '../../../types';
import NodeHeader from '../node-component/header';

// =============================================================================
// Types
// =============================================================================

/**
 * Props passed by ReactFlow for registered node type components.
 */
interface INodeVirtualEnvProps {
	id: string;
	data: INodeData;
	type?: string;
	parentId?: string;
}

// =============================================================================
// Component
// =============================================================================

/**
 * Renders the virtual environment container: a resizable boundary with a drop area
 * for its members and a badge stating that they are isolated.
 */
function NodeVirtualEnv({ id, data, type, parentId }: INodeVirtualEnvProps): ReactElement {
	const nodeId = useNodeId();
	const selected = useStore((s) => s.nodeLookup?.get(nodeId ?? '')?.selected ?? false);
	const environment = data.config?.environment as IEnvironment | undefined;

	return (
		<div style={styles.root}>
			<NodeResizer minWidth={240} minHeight={140} isVisible={selected} lineStyle={{ borderWidth: 1, borderColor: 'var(--rr-accent)' }} color="var(--rr-accent)" />

			{/* Top corner cap — matches header titlebar */}
			<div className="rr-corner-cap-top" />

			{/* Header — icon, title, gear, overflow menu */}
			<NodeHeader id={id} title={environment?.name || data.name || 'Virtual Environment'} nodeType={type} hideEdit={false} formDataValid={data.formDataValid} description={data.description} parentId={parentId} />

			{/* Drop area for member nodes. The badge is only drawn once the body has room
			    for it — at header height it would sit on top of the title. */}
			<div style={styles.body}>
				{environment?.isolated !== false && (
					<span style={styles.badge} title="Members resolve and install their dependencies in this environment">
						isolated
					</span>
				)}
			</div>

			{/* Bottom corner cap */}
			<div style={styles.cornerCapBottom} />
		</div>
	);
}

export default memo(NodeVirtualEnv);

// =============================================================================
// Styles
// =============================================================================

const styles = {
	/** Root wrapper — flex column filling the full node dimensions. */
	root: {
		display: 'flex',
		flexDirection: 'column' as const,
		width: '100%',
		height: '100%',
	},

	/** Middle body — drop area that fills the remaining space. */
	body: {
		flex: 1,
		position: 'relative' as const,
		// Clip the badge instead of letting it escape into the header when a container
		// is dragged down to its minimum height.
		overflow: 'hidden',
		backgroundColor: 'var(--rr-bg-paper)',
	} as React.CSSProperties,

	/** Isolation badge — bottom-right so it never covers a dropped member's header. */
	badge: {
		position: 'absolute' as const,
		right: '6px',
		bottom: '4px',
		padding: '0 6px',
		borderRadius: '8px',
		fontSize: '10px',
		lineHeight: '16px',
		letterSpacing: '0.04em',
		textTransform: 'uppercase' as const,
		color: 'var(--rr-accent)',
		border: '1px solid var(--rr-accent)',
		pointerEvents: 'none' as const,
	} as React.CSSProperties,

	/** Bottom cap — rounded bottom, matching the body. */
	cornerCapBottom: {
		height: '4px',
		borderRadius: '0 0 4px 4px',
		backgroundColor: 'var(--rr-bg-paper)',
	} as React.CSSProperties,
};
