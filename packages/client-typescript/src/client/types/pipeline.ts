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
 * Data flow connection between pipeline components.
 */
export interface PipelineInputConnection {
	/** Data lane/channel name (e.g., 'text', 'data', 'image') */
	lane: string;

	/** Source component ID providing the data */
	from: string;
}

/**
 * Invoke (control-flow) connection from one component to another.
 */
export interface PipelineControlConnection {
	/** Class type of the invoke channel (e.g., 'llm', 'tool', 'memory') */
	classType: string;

	/** Source component ID providing the invocation */
	from: string;
}

/**
 * Virtual environment carried by a container component.
 *
 * An isolated container's members resolve and install their dependencies into their
 * own environment instead of sharing one with the rest of the pipeline, which is what
 * lets components with incompatible requirements coexist in one document. The name is
 * display metadata: the environment is keyed by the container's `id`, so renaming it
 * does not move the installed environment.
 */
export interface PipelineEnvironment {
	/** Display name for the environment (metadata only — never an identifier) */
	name: string;

	/** Whether members run with their own isolated dependencies */
	isolated: boolean;
}

/**
 * Components nested inside a container component.
 *
 * A container stores its members here rather than at the top level; they are lifted
 * back out before execution.
 */
export interface NestedPipeline {
	/** Components belonging to the container */
	components: PipelineComponent[];
}

/**
 * Component-specific configuration.
 *
 * Open by design — each provider defines its own parameters — with the two
 * structural keys named so a container's shape is not a matter of convention.
 */
export type PipelineComponentConfig = Record<string, unknown> & {
	/** Present on a container component that declares a virtual environment */
	environment?: PipelineEnvironment;

	/** Present on a container component that has members */
	pipeline?: NestedPipeline;
};

/**
 * Pipeline component that processes data.
 *
 * Each component has a unique ID, a provider type that determines its behavior,
 * and provider-specific configuration. Components receive data through input
 * connections from other components.
 */
export interface PipelineComponent {
	/** Unique identifier for this component within the pipeline */
	id: string;

	/** Component type/provider (e.g., 'webhook', 'response', 'ai_chat') */
	provider: string;

	/** Human-readable component name */
	name?: string;

	/** Component description for documentation */
	description?: string;

	/** Component-specific configuration parameters */
	config: PipelineComponentConfig;

	/** UI-specific configuration for visual editors */
	ui?: Record<string, unknown>;

	/** Input connections from other components */
	input?: PipelineInputConnection[];

	/** Invoke (control-flow) connections from other components */
	control?: PipelineControlConnection[];
}

/**
 * Pipeline configuration for RocketRide data processing workflows.
 *
 * Defines a complete pipeline with components, data flow connections,
 * and execution parameters. Pipelines process data through a series
 * of connected components that transform, analyze, or route information.
 */
export interface PipelineConfig {
	/** Human-readable pipeline name — the registry's pipelineName renders it
	    on every deploy surface, and publish requires it. */
	name?: string;

	/** Pipeline description */
	description?: string;

	/** Pipeline version number */
	version?: number;

	/** Array of pipeline components that process data */
	components: PipelineComponent[];

	/** ID of the component that serves as the pipeline entry point */
	source?: string;

	/** Project identifier for organization and permissions */
	project_id?: string;

	/** UI viewport settings for visual editors */
	viewport?: { x: number; y: number; zoom: number };

	/** Editor document revision counter for change tracking (undo/redo, echo detection). */
	docRevision?: number;

	/** Whether the canvas is locked from editing */
	isLocked?: boolean;

	/** Whether node snapping to grid is enabled */
	snapToGrid?: boolean;

	/** Grid size for snapping [x, y] */
	snapGridSize?: [number, number];

	/** Active editor mode (e.g. 'design', 'status', 'flow') */
	editorMode?: string;
}
