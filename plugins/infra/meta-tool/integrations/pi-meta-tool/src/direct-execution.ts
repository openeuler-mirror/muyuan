import type { DispatchMode, MetaToolSpec } from "./types.ts";
import { isCallerBoundReadBatch } from "./read-batches.ts";
import { directInputValidator, validateDirectInputs } from "./input-validation.ts";

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

type DirectPrimitiveCall = (
	id: string,
	name: string,
	input: Record<string, unknown>,
) => Promise<unknown>;

class DirectStepOutputPathError extends Error {}

function resolveDirectPath(value: unknown, path: unknown): unknown {
	if (!Array.isArray(path)) throw new TypeError("A step binding path must be an array");
	let current = value;
	for (const part of path) {
		try {
			if (Array.isArray(current)) current = current[Number(part)];
			else if (isRecord(current)) current = current[String(part)];
			else throw new Error("not an object or array");
		} catch (error) {
			throw new DirectStepOutputPathError(
				`Step output path ${JSON.stringify(path)} is unavailable`,
				{ cause: error },
			);
		}
		if (current === undefined)
			throw new DirectStepOutputPathError(
				`Step output path ${JSON.stringify(path)} is unavailable`,
			);
	}
	return current;
}

function resolveDirectValue(
	value: unknown,
	inputs: Record<string, unknown>,
	outputs: ReadonlyMap<string, unknown>,
): unknown {
	if (Array.isArray(value))
		return value.map((item) => resolveDirectValue(item, inputs, outputs));
	if (!isRecord(value)) return value;
	const keys = Object.keys(value);
	if (keys.length === 1 && keys[0] === "$input") {
		const name = String(value.$input);
		if (!(name in inputs)) throw new Error(`Input ${name} is unavailable`);
		return inputs[name];
	}
	const stepBinding =
		keys.includes("$step") &&
		(keys.length === 1 ||
			(keys.length === 2 && keys.includes("path")) ||
			(keys.length === 3 && keys.includes("path") && keys.includes("fallback")));
	if (stepBinding) {
		const stepId = String(value.$step);
		if (!outputs.has(stepId))
			throw new Error(`Step output ${stepId} is unavailable`);
		try {
			return resolveDirectPath(outputs.get(stepId), value.path ?? []);
		} catch (error) {
			if (!(error instanceof DirectStepOutputPathError) || !("fallback" in value))
				throw error;
			return resolveDirectValue(value.fallback, inputs, outputs);
		}
	}
	return Object.fromEntries(
		Object.entries(value).map(([key, item]) => [
			key,
			resolveDirectValue(item, inputs, outputs),
		]),
	);
}

function policyNumber(
	policy: Record<string, unknown>,
	key: string,
	defaultValue: number,
): number {
	const value = policy[key];
	return typeof value === "number" && Number.isFinite(value)
		? value
		: defaultValue;
}

function countInternalStepBindings(spec: MetaToolSpec): number {
	const prior = new Set<string>();
	let count = 0;
	function visit(value: unknown): void {
		if (Array.isArray(value)) { value.forEach(visit); return; }
		if (!isRecord(value)) return;
		if (typeof value.$step === "string" && prior.has(value.$step)) { count++; return; }
		Object.values(value).forEach(visit);
	}
	for (const step of spec.steps) { visit(step.arguments); prior.add(step.id); }
	return count;
}

/**
 * A narrow interception gate for the synchronous hot path. It mirrors the
 * SAFE_AUTO admission checks and is deliberately stricter than the Python
 * dispatcher; anything uncertain falls back to the reference implementation.
 */
export function directExecutionAllowed(
	spec: MetaToolSpec,
	mode: DispatchMode,
	policy: Record<string, unknown>,
): boolean {
	if (mode !== "safe_auto" && mode !== "force") return false;
	if (!directInputValidator(spec.input_schema)) return false;
	// Never substitute a weaker default for a configured minimum. Coercible
	// strings and malformed thresholds go through the Python reference path.
	for (const [key, maximum, integer] of [
		["min_varying_data_flows", Infinity, true],
		["min_sequence_purity", 1, false],
		["min_branch_dominance", 1, false],
		["min_llm_turn_coverage", 1, false],
		["min_llm_calls_saved_per_occurrence", Infinity, false],
	] as const) {
		if (!(key in policy)) continue;
		const value = policy[key];
		if (typeof value !== "number" || !Number.isFinite(value) || value < 0
			|| value > maximum || (integer && !Number.isInteger(value))) return false;
	}
	// FORCE has already gone through the extension's fixed-plan confirmation
	// and per-step confirmation checks. The Python dispatcher intentionally
	// bypasses policy in this mode, so the direct executor can do the same.
	if (mode === "force") return true;
	if (spec.metadata.execution_guard !== "none") return false;
	if (spec.metadata.side_effect_free !== true) return false;
	if (
		policy.require_reviewed !== false &&
		spec.metadata.reviewed !== true &&
		spec.metadata.requires_domain_review !== false
	)
		return false;
	if (policy.require_no_compiler_warnings !== false) {
		const warnings = spec.metadata.compiler_warnings;
		if (Array.isArray(warnings) && warnings.length > 0) return false;
		if (warnings && !Array.isArray(warnings)) return false;
	}
	const internalFlows = countInternalStepBindings(spec);
	const declared = spec.metadata.internal_data_flows;
	if (typeof declared === "number" && Number.isInteger(declared) && declared !== internalFlows)
		return false;
	const callerReadBatch = policy.allow_caller_bound_read_batches === true && isCallerBoundReadBatch(spec);
	if (policy.require_internal_data_flow !== false && internalFlows < 1 && !callerReadBatch)
		return false;
	const minimums: Array<[string, number]> = [
		["sequence_purity", policyNumber(policy, "min_sequence_purity", 1)],
		["min_branch_dominance", policyNumber(policy, "min_branch_dominance", 1)],
		["llm_turn_coverage", policyNumber(policy, "min_llm_turn_coverage", 1)],
		[
			"mean_llm_calls_saved_per_observed_occurrence",
			policyNumber(policy, "min_llm_calls_saved_per_occurrence", 1),
		],
	];
	const stableFlows = spec.metadata.stable_runtime_data_flows;
	const verifiedStable = policy.allow_stable_runtime_data_flows === true
		&& typeof stableFlows === "number" && Number.isInteger(stableFlows) && stableFlows > 0
		&& spec.metadata.runtime_local_data_flow_verified === true;
	if (spec.metadata.data_flow_reviewed !== true && !callerReadBatch && !verifiedStable)
		minimums.push(["varying_data_flows", policyNumber(policy, "min_varying_data_flows", 1)]);
	for (const [key, minimum] of minimums) {
		const raw = spec.metadata[key];
		if (typeof raw !== "number") {
			if (minimum > 0) return false;
			continue;
		}
		if (!Number.isFinite(raw) || raw < minimum) return false;
	}
	return true;
}

export async function executeDirectMetaTool(
	spec: MetaToolSpec,
	inputs: Record<string, unknown>,
	callPrimitive: DirectPrimitiveCall,
): Promise<unknown> {
	validateDirectInputs(spec.input_schema, inputs);
	const outputs = new Map<string, unknown>();
	for (const step of spec.steps) {
		const raw = resolveDirectValue(step.arguments, inputs, outputs);
		if (!isRecord(raw))
			throw new TypeError(`Resolved arguments for ${step.tool} are not an object`);
		try {
			outputs.set(step.id, await callPrimitive(step.id, step.tool, raw));
		} catch (error) {
			throw new Error(
				`Meta-tool ${spec.name} failed at ${step.id} (${step.tool}): ${String(error)}`,
				{ cause: error },
			);
		}
	}
	return resolveDirectValue(spec.output, inputs, outputs);
}
