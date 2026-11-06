from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .models import SemanticType, ToolCall, stable_json


_PATH_MISSING = object()


_DEFAULT_STRUCTURAL_EXACT_TOOL_PATTERNS = (
    r"^(bash|edit|write)$",
    r"(^|[./:])(bash|shell|exec|execute|edit|write|patch)$",
)
_DEFAULT_STRUCTURAL_DISCRIMINATOR_ARGUMENTS = (
    "action",
    "command",
    "kind",
    "method",
    "mode",
    "op",
    "operation",
    "type",
)


@dataclass(frozen=True)
class RegexSubstitution:
    """Regex normalization applied recursively to selected call fields."""

    pattern: str
    replacement: str
    fields: tuple[str, ...] = ("arguments",)
    ignore_case: bool = False

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RegexSubstitution":
        fields = data.get("fields", ("arguments",))
        if isinstance(fields, str):
            fields = (fields,)
        return cls(
            pattern=str(data["pattern"]),
            replacement=str(data.get("replacement", "")),
            fields=tuple(str(item) for item in fields),
            ignore_case=bool(data.get("ignore_case", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern": self.pattern,
            "replacement": self.replacement,
            "fields": list(self.fields),
            "ignore_case": self.ignore_case,
        }


@dataclass(frozen=True)
class ToolRule:
    """Domain-expert classification and canonicalization for matching tools."""

    pattern: str
    canonical_name: str | None = None
    semantic_type: SemanticType | None = None
    domain: str | None = None
    ignore_arguments: tuple[str, ...] = ()
    placeholder_arguments: Mapping[str, str] = field(default_factory=dict)
    argument_equals: Mapping[str, Any] = field(default_factory=dict)
    argument_patterns: Mapping[str, str] = field(default_factory=dict)
    recurrent: bool | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ToolRule":
        raw_type = data.get("semantic_type")
        semantic_type = SemanticType(str(raw_type)) if raw_type is not None else None
        ignored = data.get("ignore_arguments", ())
        if isinstance(ignored, str):
            ignored = (ignored,)
        placeholders = data.get("placeholder_arguments", {})
        if not isinstance(placeholders, Mapping):
            raise ValueError("placeholder_arguments must be an object")
        argument_equals = data.get("argument_equals", {})
        argument_patterns = data.get("argument_patterns", {})
        if not isinstance(argument_equals, Mapping):
            raise ValueError("argument_equals must be an object")
        if not isinstance(argument_patterns, Mapping):
            raise ValueError("argument_patterns must be an object")
        return cls(
            pattern=str(data["pattern"]),
            canonical_name=(
                str(data["canonical_name"])
                if data.get("canonical_name") is not None
                else None
            ),
            semantic_type=semantic_type,
            domain=str(data["domain"]) if data.get("domain") is not None else None,
            ignore_arguments=tuple(str(item) for item in ignored),
            placeholder_arguments={
                str(key): str(value) for key, value in placeholders.items()
            },
            argument_equals={str(key): value for key, value in argument_equals.items()},
            argument_patterns={
                str(key): str(value) for key, value in argument_patterns.items()
            },
            recurrent=(
                bool(data["recurrent"])
                if data.get("recurrent") is not None
                else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"pattern": self.pattern}
        if self.canonical_name is not None:
            value["canonical_name"] = self.canonical_name
        if self.semantic_type is not None:
            value["semantic_type"] = self.semantic_type.value
        if self.domain is not None:
            value["domain"] = self.domain
        if self.ignore_arguments:
            value["ignore_arguments"] = list(self.ignore_arguments)
        if self.placeholder_arguments:
            value["placeholder_arguments"] = dict(self.placeholder_arguments)
        if self.argument_equals:
            value["argument_equals"] = dict(self.argument_equals)
        if self.argument_patterns:
            value["argument_patterns"] = dict(self.argument_patterns)
        if self.recurrent is not None:
            value["recurrent"] = self.recurrent
        return value


@dataclass(frozen=True)
class NormalizationConfig:
    """Horizontal-merging configuration.

    The defaults are deliberately conservative: unknown calls are mutators and
    cross-domain reordering is disabled until a domain expert opts in.
    """

    regex_substitutions: tuple[RegexSubstitution, ...] = ()
    tool_rules: tuple[ToolRule, ...] = ()
    default_semantic_type: SemanticType = SemanticType.MUTATOR
    domain_commutativity: bool = False
    infer_domain_from_name: bool = False
    include_annotators: bool = False
    state_context_fields: tuple[str, ...] = ()
    argument_matching: str = "exact"
    structural_fallback: bool = False
    structural_fallback_max_calls: int = 5000
    structural_exact_tool_patterns: tuple[str, ...] = (
        _DEFAULT_STRUCTURAL_EXACT_TOOL_PATTERNS
    )
    structural_discriminator_arguments: tuple[str, ...] = (
        _DEFAULT_STRUCTURAL_DISCRIMINATOR_ARGUMENTS
    )

    def __post_init__(self) -> None:
        if self.argument_matching not in {"exact", "structural"}:
            raise ValueError(
                "argument_matching must be either 'exact' or 'structural'"
            )
        if self.structural_fallback_max_calls < 2:
            raise ValueError("structural_fallback_max_calls must be at least 2")
        for pattern in self.structural_exact_tool_patterns:
            re.compile(pattern)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NormalizationConfig":
        return cls(
            regex_substitutions=tuple(
                RegexSubstitution.from_dict(item)
                for item in data.get("regex_substitutions", [])
            ),
            tool_rules=tuple(
                ToolRule.from_dict(item) for item in data.get("tool_rules", [])
            ),
            default_semantic_type=SemanticType(
                str(data.get("default_semantic_type", SemanticType.MUTATOR.value))
            ),
            domain_commutativity=bool(data.get("domain_commutativity", False)),
            infer_domain_from_name=bool(data.get("infer_domain_from_name", False)),
            include_annotators=bool(data.get("include_annotators", False)),
            state_context_fields=tuple(
                str(item) for item in data.get("state_context_fields", [])
            ),
            argument_matching=str(data.get("argument_matching", "exact")),
            structural_fallback=bool(data.get("structural_fallback", False)),
            structural_fallback_max_calls=int(
                data.get("structural_fallback_max_calls", 5000)
            ),
            structural_exact_tool_patterns=tuple(
                str(item)
                for item in data.get(
                    "structural_exact_tool_patterns",
                    _DEFAULT_STRUCTURAL_EXACT_TOOL_PATTERNS,
                )
            ),
            structural_discriminator_arguments=tuple(
                str(item)
                for item in data.get(
                    "structural_discriminator_arguments",
                    _DEFAULT_STRUCTURAL_DISCRIMINATOR_ARGUMENTS,
                )
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "regex_substitutions": [
                rule.to_dict() for rule in self.regex_substitutions
            ],
            "tool_rules": [rule.to_dict() for rule in self.tool_rules],
            "default_semantic_type": self.default_semantic_type.value,
            "domain_commutativity": self.domain_commutativity,
            "infer_domain_from_name": self.infer_domain_from_name,
            "include_annotators": self.include_annotators,
            "state_context_fields": list(self.state_context_fields),
            "argument_matching": self.argument_matching,
            "structural_fallback": self.structural_fallback,
            "structural_fallback_max_calls": (
                self.structural_fallback_max_calls
            ),
            "structural_exact_tool_patterns": list(
                self.structural_exact_tool_patterns
            ),
            "structural_discriminator_arguments": list(
                self.structural_discriminator_arguments
            ),
        }


@dataclass(frozen=True)
class CanonicalCall:
    raw: ToolCall
    name: str
    arguments: Mapping[str, Any]
    context: Mapping[str, Any]
    semantic_type: SemanticType
    domain: str
    recurrent: bool
    fingerprint: str
    exact_fingerprint: str
    structural_fingerprint: str | None = None

    @property
    def action_key(self) -> str:
        return f"primitive:{self.fingerprint}"

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "name": self.name,
            "arguments": dict(self.arguments),
            "context": dict(self.context),
            "semantic_type": self.semantic_type.value,
            "domain": self.domain,
            "recurrent": self.recurrent,
            "fingerprint": self.fingerprint,
            "exact_fingerprint": self.exact_fingerprint,
        }
        if self.structural_fingerprint is not None:
            value["structural_fingerprint"] = self.structural_fingerprint
        if include_raw:
            value["raw"] = self.raw.to_dict()
        return value


class TraceNormalizer:
    def __init__(self, config: NormalizationConfig | None = None) -> None:
        self.config = config or NormalizationConfig()

    def canonicalize(self, call: ToolCall) -> CanonicalCall:
        name = call.name
        arguments: Any = copy.deepcopy(dict(call.arguments))
        context: Any = {
            path: copy.deepcopy(_get_nested_path(call.metadata, path))
            for path in self.config.state_context_fields
            if _get_nested_path(call.metadata, path) is not _PATH_MISSING
        }
        semantic_type: SemanticType | None = None
        domain: str | None = None
        recurrent = False
        ignored_paths: list[str] = []
        placeholders: dict[str, str] = {}

        for rule in self.config.tool_rules:
            match = re.search(rule.pattern, call.name)
            if match is None or not _arguments_match_rule(arguments, rule):
                continue
            if rule.canonical_name is not None:
                name = match.expand(rule.canonical_name)
            if rule.semantic_type is not None:
                semantic_type = rule.semantic_type
            if rule.domain is not None:
                domain = match.expand(rule.domain)
            if rule.recurrent is not None:
                recurrent = rule.recurrent
            ignored_paths.extend(rule.ignore_arguments)
            placeholders.update(rule.placeholder_arguments)

        explicit_type = call.metadata.get("semantic_type")
        if explicit_type is not None:
            semantic_type = SemanticType(str(explicit_type))
        explicit_domain = call.metadata.get("domain")
        if explicit_domain is not None:
            domain = str(explicit_domain)
        if call.metadata.get("recurrent") is not None:
            recurrent = bool(call.metadata["recurrent"])

        for path in ignored_paths:
            _delete_path(arguments, path)
        for path, placeholder in placeholders.items():
            _set_path_if_present(arguments, path, placeholder)

        for rule in self.config.regex_substitutions:
            flags = re.IGNORECASE if rule.ignore_case else 0
            if "name" in rule.fields:
                name = re.sub(rule.pattern, rule.replacement, name, flags=flags)
            if "arguments" in rule.fields:
                arguments = _regex_replace_recursive(
                    arguments, rule.pattern, rule.replacement, flags
                )
            if "context" in rule.fields or "metadata" in rule.fields:
                context = _regex_replace_recursive(
                    context, rule.pattern, rule.replacement, flags
                )

        semantic_type = semantic_type or self.config.default_semantic_type
        if domain is None:
            if self.config.infer_domain_from_name:
                domain = _infer_domain(name)
            else:
                domain = "__global__"

        exact_payload = {
            "name": name,
            "arguments": arguments,
            "context": context,
        }
        exact_fingerprint = hashlib.sha256(
            stable_json(exact_payload).encode("utf-8")
        ).hexdigest()
        structural_fingerprint: str | None = None
        fingerprint = exact_fingerprint
        if (
            self.config.argument_matching == "structural"
            and not self._requires_exact_arguments(name)
        ):
            structural_payload = {
                "name": name,
                "arguments": _structural_shape(
                    arguments,
                    discriminator_arguments=frozenset(
                        self.config.structural_discriminator_arguments
                    ),
                ),
                # Explicit state context is an environment identity boundary,
                # not a caller argument, and therefore remains exact.
                "context": context,
            }
            structural_fingerprint = hashlib.sha256(
                stable_json(structural_payload).encode("utf-8")
            ).hexdigest()
            fingerprint = structural_fingerprint
        return CanonicalCall(
            raw=call,
            name=name,
            arguments=arguments,
            context=context,
            semantic_type=semantic_type,
            domain=domain,
            recurrent=recurrent,
            fingerprint=fingerprint,
            exact_fingerprint=exact_fingerprint,
            structural_fingerprint=structural_fingerprint,
        )

    def _requires_exact_arguments(self, name: str) -> bool:
        return any(
            re.search(pattern, name) is not None
            for pattern in self.config.structural_exact_tool_patterns
        )

    def new_state(self) -> "HorizontalState":
        return HorizontalState(
            domain_commutativity=self.config.domain_commutativity
        )


@dataclass
class HorizontalState:
    """Mutable state implementing the paper's AppWorld merge semantics."""

    domain_commutativity: bool
    accessors: set[str] = field(default_factory=set)
    action_history: list[tuple[str, str]] = field(default_factory=list)

    def apply(self, call: CanonicalCall) -> bool:
        """Apply a canonical call and return whether the state changed."""

        if call.semantic_type is SemanticType.ANNOTATOR:
            return False
        if call.semantic_type is SemanticType.ACCESSOR:
            before = len(self.accessors)
            self.accessors.add(call.fingerprint)
            return len(self.accessors) != before

        item = (call.domain, call.fingerprint)
        if call.recurrent and self.action_history and self.action_history[-1] == item:
            return False
        self.action_history.append(item)
        return True

    def payload(self) -> dict[str, Any]:
        if self.domain_commutativity:
            actions: dict[str, Any] = {
                "segments": _commutative_action_segments(self.action_history)
            }
        else:
            actions = {
                "ordered": [
                    {"domain": domain, "call": fingerprint}
                    for domain, fingerprint in self.action_history
                ]
            }
        return {
            "accessors": sorted(self.accessors),
            "actions": actions,
        }

    def state_id(self) -> str:
        if not self.accessors and not self.action_history:
            return "root"
        return hashlib.sha256(stable_json(self.payload()).encode("utf-8")).hexdigest()


def _commutative_action_segments(
    history: Sequence[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Group known domains while preserving unknown-domain ordering barriers."""

    segments: list[dict[str, Any]] = []
    current: dict[str, list[str]] = {}

    def flush() -> None:
        if current:
            segments.append(
                {
                    "commutative_domains": {
                        domain: list(calls)
                        for domain, calls in sorted(current.items())
                    }
                }
            )
            current.clear()

    for domain, fingerprint in history:
        if domain == "__global__":
            flush()
            segments.append(
                {"ordered_barrier": {"domain": domain, "call": fingerprint}}
            )
        else:
            current.setdefault(domain, []).append(fingerprint)
    flush()
    return segments


def _infer_domain(name: str) -> str:
    for separator in (".", "/", ":"):
        if separator in name:
            return name.split(separator, 1)[0]
    return "__global__"


def _arguments_match_rule(arguments: Any, rule: ToolRule) -> bool:
    for path, expected in rule.argument_equals.items():
        actual = _get_nested_path(arguments, path)
        if actual is _PATH_MISSING or stable_json(actual) != stable_json(expected):
            return False
    for path, pattern in rule.argument_patterns.items():
        actual = _get_nested_path(arguments, path)
        if actual is _PATH_MISSING:
            return False
        rendered = actual if isinstance(actual, str) else stable_json(actual)
        if re.search(pattern, rendered) is None:
            return False
    return True


def _regex_replace_recursive(
    value: Any, pattern: str, replacement: str, flags: int
) -> Any:
    if isinstance(value, str):
        return re.sub(pattern, replacement, value, flags=flags)
    if isinstance(value, list):
        return [
            _regex_replace_recursive(item, pattern, replacement, flags)
            for item in value
        ]
    if isinstance(value, tuple):
        return [
            _regex_replace_recursive(item, pattern, replacement, flags)
            for item in value
        ]
    if isinstance(value, Mapping):
        return {
            str(key): _regex_replace_recursive(item, pattern, replacement, flags)
            for key, item in value.items()
        }
    return value


def _structural_shape(
    value: Any,
    *,
    discriminator_arguments: frozenset[str],
    path: tuple[str | int, ...] = (),
) -> Any:
    """Return a deterministic argument shape without caller-specific values.

    Operation selectors and command bodies stay exact.  Other leaves retain
    only their JSON type.  Arrays use their distinct item shapes rather than
    their length, allowing semantically equivalent batches of different sizes
    to align while preserving heterogeneous item schemas.
    """

    nearest_name = next(
        (part for part in reversed(path) if isinstance(part, str)),
        None,
    )
    if nearest_name in discriminator_arguments:
        return {"$exact": value}
    if isinstance(value, Mapping):
        return {
            str(key): _structural_shape(
                item,
                discriminator_arguments=discriminator_arguments,
                path=path + (str(key),),
            )
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        item_shapes = {
            stable_json(
                _structural_shape(
                    item,
                    discriminator_arguments=discriminator_arguments,
                    path=path + (index,),
                )
            )
            for index, item in enumerate(value)
        }
        return {
            "$type": "array",
            "items": [json_value for json_value in sorted(item_shapes)],
        }
    if value is None:
        value_type = "null"
    elif isinstance(value, bool):
        # Flags frequently select behavior and are cheap to preserve exactly.
        return {"$exact": value}
    elif isinstance(value, int):
        value_type = "integer"
    elif isinstance(value, float):
        value_type = "number"
    elif isinstance(value, str):
        value_type = "string"
    else:
        value_type = type(value).__name__
    return {"$type": value_type}


def _split_path(path: str) -> Sequence[str]:
    return tuple(part for part in path.split(".") if part)


def _get_nested_path(value: Any, path: str) -> Any:
    current = value
    for part in _split_path(path):
        if not isinstance(current, Mapping) or part not in current:
            return _PATH_MISSING
        current = current[part]
    return current


def _delete_path(value: Any, path: str) -> None:
    parts = _split_path(path)
    if not parts:
        return
    parent = value
    for part in parts[:-1]:
        if not isinstance(parent, dict) or part not in parent:
            return
        parent = parent[part]
    if isinstance(parent, dict):
        parent.pop(parts[-1], None)


def _set_path_if_present(value: Any, path: str, replacement: Any) -> None:
    parts = _split_path(path)
    if not parts:
        return
    parent = value
    for part in parts[:-1]:
        if not isinstance(parent, dict) or part not in parent:
            return
        parent = parent[part]
    if isinstance(parent, dict) and parts[-1] in parent:
        parent[parts[-1]] = replacement
