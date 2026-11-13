"""Registry and resolver for built-in and application-defined profiles."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal

from .profile import ToolCallProfile, ToolCallProtocolCapabilities


class ProfileRegistrationError(ValueError):
    pass


class ProfileResolutionError(ValueError):
    pass


ResolutionSource = Literal["explicit", "format", "template", "model", "default"]


@dataclass(frozen=True, slots=True)
class ProfileResolution:
    profile: ToolCallProfile
    source: ResolutionSource
    model_name: str | None = None

    def to_mapping(self) -> dict[str, object]:
        return {
            "profile": self.profile.to_mapping(),
            "source": self.source,
            **({"model": self.model_name} if self.model_name else {}),
        }


class ProfileRegistry:
    """Ordered profiles; registration order is auto-detection priority."""

    def __init__(self) -> None:
        self._profiles: OrderedDict[str, ToolCallProfile] = OrderedDict()
        self._aliases: dict[str, str] = {}

    def register(
        self,
        profile: ToolCallProfile,
        *,
        replace: bool = False,
    ) -> None:
        profile_id = profile.profile_id
        names = (profile_id, *profile.aliases)
        conflicts = [name for name in names if name in self._aliases]
        if conflicts and not replace:
            raise ProfileRegistrationError(
                f"profile names are already registered: {', '.join(conflicts)}"
            )
        if replace:
            for name in names:
                previous = self._aliases.get(name)
                if previous is not None and previous != profile_id:
                    self.unregister(previous)
            if profile_id in self._profiles:
                self.unregister(profile_id)
        self._profiles[profile_id] = profile
        for name in names:
            self._aliases[name] = profile_id

    def unregister(self, name: str) -> None:
        profile = self.get(name)
        del self._profiles[profile.profile_id]
        for alias, target in tuple(self._aliases.items()):
            if target == profile.profile_id:
                del self._aliases[alias]

    def names(self) -> tuple[str, ...]:
        return tuple(self._profiles)

    def get(self, name: str) -> ToolCallProfile:
        normalized = name.strip().lower()
        try:
            return self._profiles[self._aliases[normalized]]
        except KeyError as error:
            raise ProfileResolutionError(f"unknown tool-call profile {name!r}") from error

    def resolve(
        self,
        profile: str | None = "auto",
        *,
        model_name: str | None = None,
        format_name: str | None = None,
        chat_template: str | None = None,
        default: str | None = None,
    ) -> ProfileResolution:
        requested = (profile or "auto").strip().lower()
        if requested and requested != "auto":
            return ProfileResolution(self.get(requested), "explicit", model_name)

        normalized_format = (format_name or "").strip().lower()
        profiles = tuple(self._profiles.values())
        candidates = (
            tuple(item for item in profiles if item.formatter == normalized_format)
            if normalized_format and normalized_format != "auto"
            else profiles
        )
        if chat_template:
            for item in candidates:
                if item.matches_template(chat_template):
                    return ProfileResolution(item, "template", model_name)
        if model_name:
            for item in candidates:
                if item.matches_model(model_name):
                    return ProfileResolution(item, "model", model_name)
        if normalized_format and normalized_format != "auto":
            try:
                return ProfileResolution(
                    self.get(normalized_format), "format", model_name
                )
            except ProfileResolutionError:
                pass
            for item in profiles:
                if item.formatter == normalized_format:
                    return ProfileResolution(item, "format", model_name)
            raise ProfileResolutionError(
                f"no tool-call profile provides format {format_name!r}"
            )
        if default:
            return ProfileResolution(self.get(default), "default", model_name)
        raise ProfileResolutionError(
            "tool-call profile could not be inferred; set an explicit profile"
        )


def default_profile_registry() -> ProfileRegistry:
    registry = ProfileRegistry()
    d3 = ToolCallProtocolCapabilities(d1=True, d2=True, d3=True)
    registry.register(
        ToolCallProfile(
            profile_id="qwen35_xml",
            aliases=("qwen3.5_xml", "qwen_xml"),
            parser="qwen_xml",
            formatter="qwen_xml",
            prompt_renderer="native_tools",
            boundary_text="<tool_call>",
            probe_prefix="<tool_call>\n<function=",
            close_text="</tool_call>",
            model_patterns=(r"qwen3\.[5-9]", r"qwen3_[5-9]", r"qwen3[5-9]"),
            template_markers=("<function=", "<parameter="),
            capabilities=d3,
            metadata={"family": "qwen3.5"},
        )
    )
    registry.register(
        ToolCallProfile(
            profile_id="qwen35_tagged_json",
            aliases=("qwen3.5_tagged_json", "qwen35_json"),
            parser="tagged_json",
            formatter="tagged_json",
            prompt_renderer="qwen_json_tools",
            boundary_text="<tool_call>",
            probe_prefix='<tool_call>\n{"name":"',
            close_text="</tool_call>",
            model_patterns=(r"qwen3\.[5-9]", r"qwen3_[5-9]", r"qwen3[5-9]"),
            capabilities=d3,
            metadata={
                "family": "qwen3.5",
                "requires_actor_json_template": True,
            },
        )
    )
    registry.register(
        ToolCallProfile(
            profile_id="qwen3_tagged_json",
            aliases=("qwen3_json", "qwen3"),
            parser="tagged_json",
            formatter="tagged_json",
            prompt_renderer="qwen_json_tools",
            boundary_text="<tool_call>",
            probe_prefix='<tool_call>\n{"name":"',
            close_text="</tool_call>",
            model_patterns=(r"(?:^|[/_.-])qwen3(?:-|$|/)",),
            capabilities=d3,
            metadata={"family": "qwen3"},
        )
    )
    registry.register(
        ToolCallProfile(
            profile_id="deepseek_v4_dsml",
            aliases=("deepseek4_dsml", "deepseek_dsml"),
            parser="deepseek_dsml",
            formatter="deepseek_dsml",
            prompt_renderer="native_tools",
            boundary_text="<｜DSML｜tool_calls>",
            probe_prefix='<｜DSML｜tool_calls>\n<｜DSML｜invoke name="',
            close_text="</｜DSML｜tool_calls>",
            model_patterns=(r"deepseek(?:[-_.]?v)?4(?:\b|[-_/])",),
            template_markers=("dsml", "tool_calls", "invoke"),
            capabilities=d3,
            metadata={"family": "deepseek_v4"},
        )
    )
    registry.register(
        ToolCallProfile(
            profile_id="tagged_json",
            parser="tagged_json",
            formatter="tagged_json",
            prompt_renderer="configured_tools",
            boundary_text="<tool_call>",
            probe_prefix='<tool_call>\n{"name":"',
            close_text="</tool_call>",
            capabilities=d3,
            metadata={"family": "generic"},
        )
    )
    return registry


__all__ = [
    "ProfileRegistrationError",
    "ProfileRegistry",
    "ProfileResolution",
    "ProfileResolutionError",
    "ResolutionSource",
    "default_profile_registry",
]
