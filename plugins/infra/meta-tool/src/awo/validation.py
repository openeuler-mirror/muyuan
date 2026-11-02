"""JSON Schema validation before any primitive or fallback is executed.

Imports of the validation dependencies are lazy so trace/spec I/O still works
without an execution environment. Invalid schemas fail closed; references can
resolve within the schema but never trigger a network fetch.
"""
from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
import json
import math
from typing import Any


class MetaToolValidationError(ValueError):
    """The supplied arguments or their declared schema cannot be validated."""


def _path(parts) -> str:
    return "$" + "".join(
        f"[{part}]" if isinstance(part, int)
        else f".{part}" if part.isidentifier()
        else f"[{json.dumps(part, ensure_ascii=False)}]"
        for part in parts
    )


def _json_value(value: Any, *, label: str, path=(), active=None) -> Any:
    """Copy Mapping inputs without coercing scalar types or accepting NaN."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if active is None:
        active = set()
    if isinstance(value, (Mapping, list)):
        if id(value) in active:
            raise MetaToolValidationError(f"{label} at {_path(path)}: cyclic value")
        active.add(id(value))
        try:
            if isinstance(value, Mapping):
                result = {}
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise MetaToolValidationError(
                            f"{label} at {_path(path)}: object keys must be strings"
                        )
                    result[key] = _json_value(
                        item, label=label, path=(*path, key), active=active
                    )
                return result
            return [_json_value(item, label=label, path=(*path, i), active=active)
                    for i, item in enumerate(value)]
        finally:
            active.remove(id(value))
    raise MetaToolValidationError(
        f"{label} at {_path(path)}: expected a finite JSON value"
    )


@lru_cache(maxsize=128)
def _validator(schema_json: str):
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError
    from jsonschema.validators import validator_for
    from referencing import Registry

    schema = json.loads(schema_json)
    if "$schema" in schema and not isinstance(schema["$schema"], str):
        raise MetaToolValidationError("Invalid meta-tool input schema: $schema must be a string")
    cls = (validator_for(schema, default=None) if "$schema" in schema
           else Draft202012Validator)
    if cls is None:
        raise MetaToolValidationError("Invalid meta-tool input schema: unsupported $schema")
    try:
        cls.check_schema(schema)
    except SchemaError as error:
        raise MetaToolValidationError(
            f"Invalid meta-tool input schema at {_path(error.absolute_path)}: "
            f"violates {error.validator or 'schema'}"
        ) from None
    # An empty Registry has no remote retriever. Local refs still work.
    return cls(schema, registry=Registry())


def validate_inputs(schema: Mapping[str, Any], inputs: Mapping[str, Any]) -> None:
    """Validate declared constraints without modifying caller arguments.

    Unspecified dialects use Draft 2020-12; format is an annotation.
    Errors identify the field/constraint without echoing argument values.
    """
    if not isinstance(inputs, Mapping):
        raise MetaToolValidationError("Invalid meta-tool inputs at $: expected an object")
    if not isinstance(schema, Mapping):
        raise MetaToolValidationError("Invalid meta-tool input schema: expected an object")
    try:
        instance = _json_value(inputs, label="Invalid meta-tool inputs")
        normalized_schema = _json_value(schema, label="Invalid meta-tool input schema")
        encoded = json.dumps(normalized_schema, sort_keys=True, ensure_ascii=False, allow_nan=False)
        validator = _validator(encoded)
    except RecursionError:
        raise MetaToolValidationError("Meta-tool input/schema nesting exceeds validation limits") from None
    except ImportError:
        raise MetaToolValidationError(
            "Meta-tool execution requires jsonschema and referencing; install meta-tool-online with its dependencies"
        ) from None
    from jsonschema.exceptions import ValidationError
    from referencing.exceptions import Unresolvable

    try:
        validator.validate(instance)
    except ValidationError as error:
        constraint = error.validator or "schema"
        path = tuple(error.absolute_path)
        if constraint == "required" and isinstance(error.instance, Mapping):
            missing = next((key for key in error.validator_value
                            if key not in error.instance), None)
            if missing is not None:
                path = (*path, missing)
        detail = (f"expected type {json.dumps(error.validator_value)}"
                  if constraint == "type" else f"violates {constraint}")
        raise MetaToolValidationError(
            f"Invalid meta-tool inputs at {_path(path)}: {detail}"
        ) from None
    except Unresolvable:
        raise MetaToolValidationError(
            "Invalid meta-tool input schema: unresolved reference; "
            "references must be available within the schema"
        ) from None
    except RecursionError:
        raise MetaToolValidationError("Meta-tool input/schema nesting exceeds validation limits") from None
