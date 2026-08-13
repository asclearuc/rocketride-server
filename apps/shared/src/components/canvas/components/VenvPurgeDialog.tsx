// =============================================================================
// MIT License
// Copyright (c) 2026 Aparavi Software AG Inc.
// =============================================================================

/**
 * VenvPurgeDialog — §4.10's operation A: empty one virtual environment's
 * installed packages, keeping the container and everything on the canvas.
 *
 * Opened from the container's overflow menu (which only sets the target id)
 * and rendered here by FlowCanvas, because a `position: fixed` dialog rendered
 * inside a node would inherit ReactFlow's viewport transform — the same reason
 * the node config panel lives out here.
 *
 * It reports three outcomes, not two. `purge` returns a boolean that is not
 * pass/fail: `false` means there was no overlay, which the engine documents as
 * idempotent success and which is the normal answer for a container that has
 * never run. Only a throw is a refusal, and then the engine's own message is
 * shown verbatim — it names the cause (a live run, files held open) and
 * rewording it would throw that away.
 */

import { ReactElement, useState } from 'react';
import { ConfirmDialog } from 'shell';

import { useFlowGraph } from '../context/FlowGraphContext';
import { useFlowProject } from '../context/FlowProjectContext';

// =============================================================================
// Component
// =============================================================================

/**
 * Renders the purge confirmation for the container named by
 * `venvPurgeNodeId`, and performs the call when it is confirmed.
 *
 * @returns The dialog element.
 */
export default function VenvPurgeDialog(): ReactElement {
	const { nodeMap, venvPurgeNodeId, setVenvPurgeNodeId, setConfigSnackbar } = useFlowGraph();
	const { venvOps, currentProject } = useFlowProject();
	const [busy, setBusy] = useState(false);

	const nodeId = venvPurgeNodeId ?? '';
	const node = nodeMap[nodeId];
	const environment = node?.data?.config?.environment as { name?: string } | undefined;
	const label = environment?.name || node?.data?.name || nodeId;
	const projectId = currentProject?.project_id ?? '';

	const handleConfirm = async (): Promise<void> => {
		if (!venvOps || !projectId || busy) return;
		setBusy(true);
		try {
			// Both ids travel raw: the engine resolves them literal-first and
			// otherwise shortens them itself, exactly as it did when the overlay
			// was created. Shortening here would address a plausible, absent
			// directory and then report success.
			const purged = await venvOps.purge(projectId, nodeId);
			setConfigSnackbar(purged ? `Purged the installed packages of ${label}` : `Nothing to reclaim — ${label} has no installed packages`);
		} catch (error) {
			setConfigSnackbar(error instanceof Error ? error.message : String(error));
		} finally {
			setBusy(false);
			setVenvPurgeNodeId(undefined);
		}
	};

	const message = (
		<div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
			<div>Deletes the packages installed for this environment on the server. The container, its members and their configuration are untouched.</div>
			<div>Packages are reinstalled on the next run — the requirements live in the pipeline, so nothing you wrote is lost. The cost is that run&apos;s install time.</div>
		</div>
	);

	return <ConfirmDialog title={`Purge the packages of ${label}?`} message={message} confirmLabel="Purge" cancelLabel="Cancel" destructive confirmDisabled={busy} onConfirm={() => void handleConfirm()} onCancel={() => setVenvPurgeNodeId(undefined)} />;
}
