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


def compact_tool_schema(
    schema: dict[str, Any], *, prune_additional_properties: bool = False,
) -> dict[str, Any]:
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

    compacted = compress_schema(
        compact(schema),
        prune_additional_properties=prune_additional_properties,
        prune_titles=True,
    )
    if not prune_additional_properties:
        return compacted

    # ``true`` is JSON Schema's default and a string-only enum already constrains
    # the instance type. Removing these redundant structural tokens keeps the
    # registered MCP schema within Codex's compacted tool budget without
    # changing validation semantics.
    def prune_redundant(node: Any) -> Any:
        if not isinstance(node, dict):
            return node
        result = children(node, prune_redundant)
        if result.get("additionalProperties") is True:
            result.pop("additionalProperties")
        enum = result.get("enum")
        if result.get("type") == "string" and isinstance(enum, list) and enum and all(isinstance(value, str) for value in enum):
            result.pop("type")
        return result

    return prune_redundant(compacted)
