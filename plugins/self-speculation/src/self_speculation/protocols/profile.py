"""Declarative model tool-call protocol profiles."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolCallProtocolCapabilities:
    """Protocol-level support; the selected engine may impose stricter limits."""

    d1: bool = True
    d2: bool = True
    d3: bool = False
    streaming: bool = True

    def to_mapping(self) -> dict[str, bool]:
        return {
            "d1": self.d1,
            "d2": self.d2,
            "d3": self.d3,
            "streaming": self.streaming,
        }


@dataclass(frozen=True, slots=True)
class ToolCallProfile:
    """One coherent parser/formatter/prompt/boundary combination."""

    profile_id: str
    parser: str
    formatter: str
    prompt_renderer: str
    boundary_text: str
    probe_prefix: str
    close_text: str | None = None
    aliases: tuple[str, ...] = ()
    model_patterns: tuple[str, ...] = ()
    template_markers: tuple[str, ...] = ()
    capabilities: ToolCallProtocolCapabilities = field(
        default_factory=ToolCallProtocolCapabilities
    )
    version: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized = self.profile_id.strip().lower()
        if not normalized:
            raise ValueError("profile_id must not be empty")
        object.__setattr__(self, "profile_id", normalized)
        object.__setattr__(
            self,
            "aliases",
            tuple(alias.strip().lower() for alias in self.aliases if alias.strip()),
        )
        for field_name in ("parser", "formatter", "prompt_renderer"):
            normalized_value = str(getattr(self, field_name)).strip().lower()
            if not normalized_value:
                raise ValueError(f"{field_name} must not be empty")
            object.__setattr__(self, field_name, normalized_value)
        for field_name in ("boundary_text", "probe_prefix"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} must not be empty")
        object.__setattr__(self, "model_patterns", tuple(self.model_patterns))
        object.__setattr__(self, "template_markers", tuple(self.template_markers))
        object.__setattr__(self, "metadata", dict(self.metadata))
        if not self.probe_prefix.startswith(self.boundary_text):
            raise ValueError("probe_prefix must start with boundary_text")
        if self.version <= 0:
            raise ValueError("profile version must be positive")
        for pattern in self.model_patterns:
            re.compile(pattern, re.IGNORECASE)

    def matches_model(self, model_name: str) -> bool:
        return any(
            re.search(pattern, model_name, re.IGNORECASE) is not None
            for pattern in self.model_patterns
        )

    def matches_template(self, chat_template: str) -> bool:
        if not self.template_markers:
            return False
        normalized = chat_template.lower()
        return all(marker.lower() in normalized for marker in self.template_markers)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "id": self.profile_id,
            "version": self.version,
            "parser": self.parser,
            "formatter": self.formatter,
            "prompt_renderer": self.prompt_renderer,
            "boundary": self.boundary_text,
            "capabilities": self.capabilities.to_mapping(),
            **({"metadata": dict(self.metadata)} if self.metadata else {}),
        }


__all__ = ["ToolCallProfile", "ToolCallProtocolCapabilities"]
