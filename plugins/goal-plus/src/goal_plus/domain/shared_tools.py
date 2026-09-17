"""Pure contracts and structural invariants."""

from __future__ import annotations

from pathlib import Path
from typing import (
    Any,
    Literal,
)

from pydantic import (
    Field,
    field_validator,
    model_validator,
)

from goal_plus.domain.base import SearchModel

SharedToolPublishStatus = Literal[
    "legacy_unknown", "not_staged",
    "skipped_unattributed_verifier", "skipped_failed_verifier", "published",
    "partially_published", "consumed_unchanged", "snapshot_rejected",
    "snapshot_error",
]


ToolizationSignal = Literal[
    "repeated_workflow",
    "domain_construction_or_probe",
    "behavior_or_invariant_checker",
    "reproducer_fixture_or_case_generator",
    "parser_trace_or_comparator",
    "peer_setup_or_feedback_reduction",
]


ToolizationExclusion = Literal[
    "single_common_command",
    "logic_free_wrapper",
    "restricted_artifact",
    "candidate_private_state",
    "duplicate_snapshot",
    "no_toolizable_material",
    "existing_family_sufficient",
    "no_material_tool_delta",
    "draft_not_ready",
    "max_published_versions_reached",
]


ToolizationAdvisory = Literal[
    "toolization_review_missing",
    "toolization_stage_missing",
    "toolization_decision_mismatch",
]


class ToolViewRef(SearchModel):
    tool_id: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=1000)
    capabilities: list[str] = Field(max_length=16)
    when_to_use: str = Field(min_length=1, max_length=1000)
    entrypoint: str | None = Field(max_length=500)
    inputs: list[str] = Field(max_length=16)
    outputs: list[str] = Field(max_length=16)
    dependencies: list[str] = Field(max_length=16)
    adoption_steps: list[str] = Field(max_length=16)
    limitations: list[str] = Field(max_length=16)

    @field_validator(
        "capabilities", "inputs", "outputs", "dependencies",
        "adoption_steps", "limitations", mode="before"
    )
    @classmethod
    def normalize_items(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        normalized = []
        for item in value:
            if not isinstance(item, str):
                normalized.append(item)
                continue
            text = " ".join(item.strip().split())
            if not text or len(text) > 500:
                raise ValueError("tool view list items must be non-empty and at most 500 characters")
            normalized.append(text)
        return normalized


class ToolViewRecord(ToolViewRef):
    snapshot_hash: str = Field(min_length=1)
    source_commit: str = Field(min_length=1)
    evidence_scope: str = Field(min_length=1, max_length=1000)


class ToolAdoptionRecord(SearchModel):
    tool_id: str = Field(min_length=1)
    snapshot_hash: str = Field(min_length=1)
    receipt_id: str | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_declaration(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "mode" not in value:
            return value
        payload = dict(value)
        payload.pop("mode", None)
        return payload


class ToolCopyReceipt(SearchModel):
    receipt_id: str = Field(min_length=1)
    tool_id: str = Field(min_length=1)
    snapshot_hash: str = Field(min_length=1)
    source_commit: str | None = None
    agent_session_id: str = Field(min_length=1)
    candidate_base_git_head: str = Field(min_length=1)
    inbox_path: Path
    copied_at: str


class SharedDirInput(SearchModel):
    enabled: bool = False
    max_tools_per_iteration: int = Field(default=16, gt=0, le=128)
    max_published_versions_per_family: int = Field(
        default=2,
        ge=1,
        le=32,
        description="Maximum immutable published records retained per family.",
    )
    max_files_per_iteration: int = Field(default=64, gt=0, le=512)
    max_path_entries_per_iteration: int = Field(default=512, gt=0, le=8192)
    max_depth: int = Field(default=8, ge=1, le=32)
    max_bytes_per_iteration: int = Field(default=2 * 1024 * 1024, gt=0, le=64 * 1024 * 1024)


class SharedDirSpec(SharedDirInput):
    max_pending_revisions_per_family: int = Field(default=1, ge=1, le=1)


ToolPublicationIntent = Literal[
    "new",
    "capability_extension",
    "adoption_fix",
    "contract_change",
]


class SharedToolRecord(SearchModel):
    tool_id: str = Field(min_length=1)
    family_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    publication_intent: ToolPublicationIntent = "new"
    supersedes_tool_id: str | None = None
    capability_ids: list[str] = Field(default_factory=list, max_length=32)
    coverage_keys: list[str] = Field(default_factory=list, max_length=64)
    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    source_commit: str | None = None
    snapshot_hash: str = Field(min_length=1)
    name: str = Field(min_length=1)
    summary: str | None = None
    entrypoint: str | None = None
    source_relative_path: str = Field(min_length=1)
    read_only_path: Path
    files: list[str] = Field(default_factory=list)
    size_bytes: int = Field(ge=0)
    created_at: str

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_asset_id(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "tool_id" in value or "asset_id" not in value:
            return value
        payload = dict(value)
        payload["tool_id"] = payload.pop("asset_id")
        return payload

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_family(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "family_id" in value:
            return value
        payload = dict(value)
        tool_id = payload.get("tool_id") or payload.get("asset_id")
        if isinstance(tool_id, str) and tool_id:
            payload["family_id"] = f"legacy-{tool_id}"
            payload["version"] = 1
            payload["publication_intent"] = "new"
        return payload

    @field_validator("capability_ids", "coverage_keys", mode="before")
    @classmethod
    def normalize_tool_keys(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        normalized: list[Any] = []
        for item in value:
            if not isinstance(item, str):
                normalized.append(item)
                continue
            key = " ".join(item.strip().split())
            if not key or len(key) > 160:
                raise ValueError("tool capability and coverage keys must contain 1-160 characters")
            normalized.append(key)
        if len(set(normalized)) != len(normalized):
            raise ValueError("tool capability and coverage keys must be unique")
        return normalized


class SharedToolFamily(SearchModel):
    family_id: str = Field(min_length=1)
    pending_head: str | None = None
    discoverable_head: str | None = None
    created_at: str
    updated_at: str


class ToolizationDecision(SearchModel):
    outcome: Literal["staged", "not_applicable"]
    signals: list[ToolizationSignal] = Field(
        default_factory=list,
        max_length=6,
        description="Use the six capability-oriented toolization signals.",
    )
    exclusion: ToolizationExclusion | None = None
    rationale: str = Field(min_length=1, max_length=1000)
    tool_names: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("rationale")
    @classmethod
    def rationale_must_be_nonempty(cls, value: str) -> str:
        normalized = " ".join(value.split()).strip()
        if not normalized:
            raise ValueError("toolization rationale must be non-empty")
        return normalized

    @field_validator("signals")
    @classmethod
    def signals_must_be_unique(
        cls, value: list[ToolizationSignal]
    ) -> list[ToolizationSignal]:
        if len(value) != len(set(value)):
            raise ValueError("toolization signals must be unique")
        return value

    @field_validator("tool_names")
    @classmethod
    def tool_names_must_be_unique_and_nonempty(
        cls, value: list[str]
    ) -> list[str]:
        normalized = [" ".join(item.split()).strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("toolization tool_names must be non-empty")
        if any(len(item) > 120 for item in normalized):
            raise ValueError("toolization tool_names must not exceed 120 characters")
        if len(normalized) != len(set(normalized)):
            raise ValueError("toolization tool_names must be unique")
        return normalized

    @model_validator(mode="after")
    def outcome_matches_evidence(self) -> "ToolizationDecision":
        if self.outcome == "staged":
            if not self.signals:
                raise ValueError("staged toolization requires at least one signal")
            if self.exclusion is not None:
                raise ValueError("staged toolization cannot declare an exclusion")
            if not self.tool_names:
                raise ValueError("staged toolization requires at least one tool name")
        else:
            if self.exclusion is None:
                raise ValueError("not_applicable toolization requires an exclusion")
            if self.tool_names:
                raise ValueError("not_applicable toolization cannot declare tool names")
        return self

SHARE_OUT_RELATIVE_PATH = ".tmp/share-out"
TOOL_DRAFTS_RELATIVE_PATH = ".tmp/tool-drafts"
TOOL_INBOX_RELATIVE_PATH = ".tmp/shared-tools"
