"""Main's freeze input; runtime-owned fields remain in the persisted models."""

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field, ValidationError

from goal_plus.domain.agents import ModelRequest, WorkerBudgetInput
from goal_plus.domain.search import SearchSpec, StrategyOptions
from goal_plus.domain.shared_tools import SharedDirInput
from goal_plus.domain.shared_knowledge import SharedKnowledgeSpec


class StrategyInput(StrategyOptions):
    models: list[ModelRequest] = Field(
        default_factory=list,
        description="At most max_parallel entries. Set count on all entries (sum=max_parallel), or omit all for even allocation.",
    )
    worker_budget: WorkerBudgetInput | None = Field(
        default=None, description="Omit for no limit; otherwise set at least one positive limit per worker.",
    )
    global_evidence_mode: Literal["manual", "auto", "independent"] | None = None


class SearchSpecInput(SearchSpec):
    strategy: StrategyInput = Field(default_factory=StrategyInput)
    shared_dir: SharedDirInput = Field(default_factory=SharedDirInput)
    shared_knowledge: SharedKnowledgeSpec = Field(
        default_factory=SharedKnowledgeSpec
    )

    def to_runtime_spec(self) -> SearchSpec:
        payload = self.model_dump(mode="json")
        mode = payload["strategy"].pop("global_evidence_mode")
        if mode is not None:
            payload["strategy"]["config"] = {"global_evidence_mode": mode}
        return SearchSpec.model_validate(payload)


def format_search_spec_admission_error(
    payload: Any,
    error: ValidationError,
) -> str:
    """Turn a raw freeze payload error into an actionable model-facing error.

    FastMCP validates tool arguments before it enters the tool body.  The
    freeze facade therefore calls this formatter after it has merged the
    optional top-level convenience fields, so malformed SearchSpec payloads
    receive one stable Goal Plus error instead of a provider-specific trace.
    """

    details: list[str] = []
    for item in error.errors():
        location = ".".join(str(part) for part in item.get("loc", ())) or "spec"
        kind = item.get("type", "invalid")
        if kind == "missing":
            details.append(f"{location}: required field is missing")
        elif kind == "extra_forbidden":
            details.append(f"{location}: field is not part of the current SearchSpec")
        else:
            message = str(item.get("msg", "invalid value")).replace("\n", " ")
            details.append(f"{location}: {message}")

    if isinstance(payload, Mapping):
        top_level = set(payload)
    else:
        top_level = set()
    legacy_root_fields = sorted(
        field
        for field in top_level
        if field in {
            "verifier_input",
            "verifier_updates",
            "global_evidence_mode",
            "inner_agent",
            "evidence_annotator",
        }
    )
    legacy_hint = ""
    if legacy_root_fields:
        legacy_hint = (
            " Move these fields to their current nested owners or remove them: "
            + ", ".join(legacy_root_fields)
            + "."
        )

    rendered_details = "; ".join(details[:8])
    if len(details) > 8:
        rendered_details += f"; and {len(details) - 8} more validation error(s)"
    return (
        "SearchSpecAdmissionError: the freeze payload does not match the current "
        "Goal Plus SearchSpec schema. "
        "Recommendation: call goal_plus_get_tool_schema(\"goal_plus_search_freeze_spec\"), "
        "put one complete SearchSpec object under spec, keep source_path and workspace "
        "inside spec, and remove duplicate top-level or legacy fields."
        f"{legacy_hint}"
        " This is a Goal Plus payload error, not a ThinkThread version error."
        + (f" Details: {rendered_details}." if rendered_details else "")
    )
