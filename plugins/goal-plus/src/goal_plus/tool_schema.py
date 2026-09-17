"""Lossless, compact JSON Schema presentation for the shared tool catalog."""

from collections import Counter
from typing import Any

from fastmcp.utilities.json_schema import compress_schema


# These constraints apply only to their JSON instance type, so also allow null.
_NULL_COMPATIBLE_KEYS = {
    "type", "properties", "required", "additionalProperties", "minProperties", "maxProperties",
    "items", "prefixItems", "minItems", "maxItems", "uniqueItems",
    "minLength", "maxLength", "pattern", "format",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "description", "default", "title",
}


def compact_tool_schema(schema: dict[str, Any]) -> dict[str, Any]:
    references: Counter[str] = Counter()

    def children(node, transform):
        result = dict(node)
        for key in ("properties", "$defs", "definitions", "patternProperties"):
            if key in result:
                result[key] = {name: transform(child) for name, child in result[key].items()}
        for key in ("items", "additionalProperties", "not"):
            if isinstance(result.get(key), dict):
                result[key] = transform(result[key])
        for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            if key in result:
                result[key] = [transform(child) for child in result[key]]
        return result

    def count(node):
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            references[node["$ref"]] += 1
        return children(node, count)

    count(schema)
    definitions = schema.get("$defs", {})

    def compact(node):
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref", "")
        if ref.startswith("#/$defs/") and references[ref] == 1:
            name = ref[len("#/$defs/"):].replace("~1", "/").replace("~0", "~")
            if name in definitions:
                target = compact(definitions[name])
                siblings = {key: value for key, value in node.items() if key != "$ref"}
                # Conflicting sibling constraints are intersections, not overrides.
                if any(key in target and target[key] != value for key, value in siblings.items()):
                    return {"allOf": [target, compact(siblings)]}
                return {**target, **compact(siblings)}
        result = children(node, compact)
        variants = result.get("anyOf", [])
        if variants and all(
            isinstance(item, dict) and set(item) == {"type"}
            and isinstance(item["type"], str) for item in variants
        ) and "type" not in result:
            result["type"] = list(dict.fromkeys(item["type"] for item in variants))
            del result["anyOf"]
        elif len(variants) == 2 and {"type": "null"} in variants:
            value = next(item for item in variants if item != {"type": "null"})
            if (
                isinstance(value, dict) and isinstance(value.get("type"), str)
                and set(value) <= _NULL_COMPATIBLE_KEYS
                and not (set(value) & (set(result) - {"anyOf"}))
            ):
                del result["anyOf"]
                result.update(value, type=[value["type"], "null"])
        return result

    return compress_schema(compact(schema), prune_titles=True)
