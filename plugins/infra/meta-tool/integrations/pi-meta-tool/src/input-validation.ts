import Ajv2020 from "ajv/dist/2020.js";
import type { ValidateFunction } from "ajv";

// Match bate.validation: Draft 2020-12 by default, format is annotation,
// no input coercion/default insertion, and no remote schema retrieval.
const validators = new Map<string, ValidateFunction | null>();
const dialect = "https://json-schema.org/draft/2020-12/schema";

function finiteJson(value: unknown, active = new Set<object>()): boolean {
	if (value === null || typeof value === "string" || typeof value === "boolean") return true;
	if (typeof value === "number") return Number.isFinite(value);
	if (typeof value !== "object" || active.has(value)) return false;
	if (!Array.isArray(value) && Object.getPrototypeOf(value) !== Object.prototype
		&& Object.getPrototypeOf(value) !== null) return false;
	active.add(value);
	try { return Object.values(value).every((item) => finiteJson(item, active)); }
	finally { active.delete(value); }
}

function referenceOnly(value: unknown): boolean {
	if (value === null || typeof value !== "object") return false;
	// Ajv also supports non-JSON-Schema extensions. They must not change the
	// meaning of schemas handled by the Python reference validator.
	if (!Array.isArray(value)) {
		const object = value as Record<string, unknown>;
		if ("nullable" in object || "$async" in object) return true;
		if ("$schema" in object && object.$schema !== dialect) return true;
	}
	return Object.values(value).some(referenceOnly);
}

export function directInputValidator(schema: Record<string, unknown>): ValidateFunction | null {
	try {
		if (!finiteJson(schema) || referenceOnly(schema)) return null;
		const key = JSON.stringify(schema);
		if (validators.has(key)) return validators.get(key)!;
		let validate: ValidateFunction | null = null;
		try {
			// Per-schema instances prevent references resolving against unrelated
			// catalog entries, and allow the bounded cache to release compiled code.
			const ajv = new Ajv2020({ strict: false, strictSchema: true,
				strictNumbers: true, validateFormats: false, ownProperties: true });
			validate = ajv.compile(JSON.parse(key));
		} catch {
			// Unsupported dialect/keyword, malformed schema or unresolved ref:
			// let the Python path validate before it executes any primitive.
		}
		if (validators.size >= 128) validators.clear();
		validators.set(key, validate);
		return validate;
	} catch { return null; }
}

export function validateDirectInputs(schema: Record<string, unknown>, inputs: Record<string, unknown>): void {
	const validate = directInputValidator(schema);
	if (!validate) throw new Error("Meta-tool input schema requires Python validation");
	if (inputs === null || typeof inputs !== "object" || Array.isArray(inputs) || !finiteJson(inputs))
		throw new Error("Invalid meta-tool inputs: expected a finite JSON object");
	if (!validate(inputs)) {
		const error = validate.errors?.[0];
		throw new Error(`Invalid meta-tool inputs at $${error?.instancePath ?? ""}: violates ${error?.keyword ?? "schema"}`);
	}
}
