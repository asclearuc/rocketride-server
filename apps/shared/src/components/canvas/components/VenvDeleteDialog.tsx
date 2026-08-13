// =============================================================================
// MIT License
// Copyright (c) 2026 Aparavi Software AG Inc.
// =============================================================================

/**
 * VenvDeleteDialog — the two questions §4.10 owes before a virtual-environment
 * container is removed: do its members go with it, and does the environment go
 * from the server's disk.
 *
 * Rendered by FlowCanvas rather than by the container node, because ReactFlow
 * invokes `onBeforeDelete` and not a component: the overflow menu, the Delete
 * key and a region select all arrive through that one hook. Mounted only while
 * a question is open, so its answers reset by unmounting.
 *
 * Both answers are independent, which is why they are two checkboxes in one
 * dialog and not a two-step wizard — someone who wants to ungroup and keep the
 * packages should see both choices at once.
 */

import React, { ReactElement, useState } from 'react';
import { ConfirmDialog } from 'shell';

import type { IVenvDeleteAnswer, IVenvDeleteRequest } from '../context/FlowGraphContext';

// =============================================================================
// Types
// =============================================================================

interface IVenvDeleteDialogProps {
	/** The open question, as parked by `onBeforeDelete`. */
	request: IVenvDeleteRequest;
	/** Answers it. Null cancels, and then nothing at all happens. */
	onResolve: (answer: IVenvDeleteAnswer | null) => void;
}

// =============================================================================
// Component
// =============================================================================

/**
 * Renders the container-delete confirmation.
 *
 * @param props - The open request and its resolver.
 * @returns The dialog element.
 */
export default function VenvDeleteDialog({ request, onResolve }: IVenvDeleteDialogProps): ReactElement {
	// Members are kept by default: that is what deleting a container from the
	// overflow menu has always done, and it is the answer that loses no work.
	const [deleteMembers, setDeleteMembers] = useState(false);
	const [deleteOverlay, setDeleteOverlay] = useState(false);

	const many = request.containerIds.length > 1;
	const title = many ? `Delete ${request.containerIds.length} virtual environments?` : `Delete ${request.containerNames[0]}?`;

	const message = (
		<div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
			{many && <div>{request.containerNames.join(', ')}</div>}

			<label style={styles.choice}>
				<input type="checkbox" checked={deleteMembers} onChange={(e) => setDeleteMembers(e.target.checked)} />
				<span>Delete the member nodes and their connections{many ? '' : ' too'}</span>
			</label>
			<div style={styles.hint}>{deleteMembers ? 'The members go with the container.' : 'The members stay on the canvas, no longer grouped.'}</div>

			{/* The disk question is asked only where it can be answered: a host
			    that wired no server operations, or a pipeline that has never been
			    saved, has no environment to address. */}
			{request.canRemoveOverlay && (
				<>
					<label style={{ ...styles.choice, ...(request.overlayBlockedByRun ? styles.blocked : null) }}>
						<input type="checkbox" checked={deleteOverlay && !request.overlayBlockedByRun} disabled={request.overlayBlockedByRun} onChange={(e) => setDeleteOverlay(e.target.checked)} />
						{/* The reason rides in the label: a disabled control here
						    has nowhere else to explain itself. */}
						<span>{request.overlayBlockedByRun ? 'Also remove the installed packages — a run is in progress' : 'Also remove the installed packages from the server'}</span>
					</label>
					<div style={styles.hint}>Packages are reinstalled on the next run — the requirements live in the pipeline, so nothing you wrote is lost. Undo brings the nodes back, never the packages.</div>
				</>
			)}
		</div>
	);

	return <ConfirmDialog title={title} message={message} confirmLabel="Delete" cancelLabel="Cancel" destructive onConfirm={() => onResolve({ deleteMembers, deleteOverlay: deleteOverlay && !request.overlayBlockedByRun })} onCancel={() => onResolve(null)} />;
}

// =============================================================================
// Styles
// =============================================================================

const styles = {
	/** One checkbox row. */
	choice: {
		display: 'flex',
		alignItems: 'flex-start',
		gap: 8,
		color: 'var(--rr-text-primary)',
		cursor: 'pointer',
	} as React.CSSProperties,

	/** A choice the server would refuse right now. */
	blocked: {
		opacity: 0.6,
		cursor: 'default',
	} as React.CSSProperties,

	/** Consequence line under a choice. */
	hint: {
		marginTop: -6,
		marginLeft: 22,
		fontSize: 12,
		color: 'var(--rr-text-secondary)',
	} as React.CSSProperties,
};
