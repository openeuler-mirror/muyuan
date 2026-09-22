"""Deterministic candidate-pool resolution and tolerance selection."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from pydantic import Field, field_validator, model_validator

from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.base import SearchModel


ScoreDirection = Literal["maximize", "minimize"]
RankingKey = Literal["hard_score", "quality_score"]


class SelectionSpec(SearchModel):
    ranking_keys: list[RankingKey] = Field(
        default_factory=lambda: ["hard_score"], min_length=1,
        json_schema_extra={"uniqueItems": True},
        description="hard_score uses metric_name; quality_score uses LLM evaluation (not rolling_candidates). Not metric names.",
    )
    ranking_tolerance: list[Annotated[float, Field(ge=0, allow_inf_nan=False)]] = Field(
        default_factory=lambda: [0.0], min_length=1,
        description="Absolute tolerances, one per ranking_key in the same order.",
    )

    @field_validator("ranking_keys")
    @classmethod
    def ranking_keys_are_unique(cls, value: list[RankingKey]) -> list[RankingKey]:
        if len(value) != len(set(value)):
            raise ValueError("selection ranking_keys must be unique")
        return value

    @model_validator(mode="after")
    def keys_and_tolerances_align(self) -> "SelectionSpec":
        if len(self.ranking_keys) != len(self.ranking_tolerance):
            raise ValueError("selection ranking_keys and ranking_tolerance must align")
        return self


@dataclass(frozen=True)
class SelectionSubjectKey:
    candidate_id: str
    iteration: int | None
    settlement_id: str | None
    artifact_hash: str | None
    verifier_subject_hash: str


class ControllerSelectionTarget(SearchModel):
    """Exact hard-settled subject selected by a trusted external controller."""

    candidate_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    settlement_id: str = Field(min_length=1)
    artifact_ref: ArtifactRef | None
    artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ControllerSelectionDirective(SearchModel):
    """Controller decision binding without importing external judge semantics."""

    target: ControllerSelectionTarget
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class SelectionOption:
    subject: SelectionSubjectKey
    hard_score: float
    original_index: int
    preferred: bool = False


@dataclass(frozen=True)
class ScoreResolution:
    state: Literal["pending", "unavailable", "value"]
    value: float | None = None
    reason: str | None = None

    @classmethod
    def pending(cls, reason: str | None = None) -> "ScoreResolution":
        return cls("pending", reason=reason)

    @classmethod
    def unavailable(cls, reason: str | None = None) -> "ScoreResolution":
        return cls("unavailable", reason=reason)

    @classmethod
    def resolved(cls, value: float) -> "ScoreResolution":
        if not math.isfinite(value):
            return cls.unavailable("score is not finite")
        return cls("value", value=float(value))


class ScoreSource(Protocol):
    key: str
    direction: ScoreDirection

    def resolve(self, option: SelectionOption) -> ScoreResolution: ...


@dataclass(frozen=True)
class ResolvedSelectionOption:
    option: SelectionOption
    values: dict[str, float]


@dataclass(frozen=True)
class CandidatePoolResolution:
    state: Literal["wait", "ready", "blocked"]
    pool: tuple[ResolvedSelectionOption, ...]
    exclusions: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class SelectionResult:
    winner: ResolvedSelectionOption
    trace: tuple[dict[str, object], ...]


def resolve_candidate_pool(
    options: list[SelectionOption],
    ranking_keys: list[str],
    score_sources: dict[str, ScoreSource],
) -> CandidatePoolResolution:
    if not ranking_keys:
        raise ValueError("selection requires at least one ranking key")
    missing = [key for key in ranking_keys if key not in score_sources]
    if missing:
        raise ValueError("selection score source is not registered: " + ", ".join(missing))

    resolved: list[ResolvedSelectionOption] = []
    exclusions: list[dict[str, object]] = []
    waiting = False
    for option in options:
        values: dict[str, float] = {}
        unavailable: list[str] = []
        pending: list[str] = []
        for key in ranking_keys:
            result = score_sources[key].resolve(option)
            if result.state == "pending":
                pending.append(key)
            elif result.state == "unavailable" or result.value is None:
                unavailable.append(key)
            else:
                values[key] = result.value
        if pending:
            waiting = True
            exclusions.append(
                {"subject": option.subject, "reason": "pending", "keys": pending}
            )
        elif unavailable:
            exclusions.append(
                {"subject": option.subject, "reason": "unavailable", "keys": unavailable}
            )
        else:
            resolved.append(ResolvedSelectionOption(option, values))
    if waiting:
        return CandidatePoolResolution("wait", (), tuple(exclusions))
    if not resolved:
        return CandidatePoolResolution("blocked", (), tuple(exclusions))
    return CandidatePoolResolution("ready", tuple(resolved), tuple(exclusions))


def select_winner(
    pool: tuple[ResolvedSelectionOption, ...] | list[ResolvedSelectionOption],
    ranking_keys: list[str],
    ranking_tolerance: list[float],
    score_sources: dict[str, ScoreSource],
    rejected: set[SelectionSubjectKey] | None = None,
) -> SelectionResult:
    if len(ranking_keys) != len(ranking_tolerance) or not ranking_keys:
        raise ValueError("selection keys and tolerances must be non-empty and aligned")
    survivors = [
        item for item in pool if item.option.subject not in (rejected or set())
    ]
    if not survivors:
        raise ValueError("selection has no non-rejected candidates")
    trace: list[dict[str, object]] = []
    for key, tolerance in zip(ranking_keys, ranking_tolerance, strict=True):
        if not math.isfinite(tolerance) or tolerance < 0:
            raise ValueError("selection tolerance must be finite and nonnegative")
        direction = score_sources[key].direction
        scores = [item.values[key] for item in survivors]
        best = max(scores) if direction == "maximize" else min(scores)
        threshold = best - tolerance if direction == "maximize" else best + tolerance
        if direction == "maximize":
            survivors = [item for item in survivors if item.values[key] >= threshold]
        else:
            survivors = [item for item in survivors if item.values[key] <= threshold]
        trace.append(
            {
                "key": key,
                "direction": direction,
                "best": best,
                "tolerance": tolerance,
                "threshold": threshold,
                "survivor_count": len(survivors),
            }
        )
        if len(survivors) == 1:
            break
    winner = min(
        survivors,
        key=lambda item: (not item.option.preferred, item.option.original_index),
    )
    return SelectionResult(winner, tuple(trace))


__all__ = [
    "CandidatePoolResolution",
    "ControllerSelectionDirective",
    "ControllerSelectionTarget",
    "ResolvedSelectionOption",
    "ScoreDirection",
    "ScoreResolution",
    "ScoreSource",
    "SelectionOption",
    "SelectionResult",
    "SelectionSpec",
    "SelectionSubjectKey",
    "resolve_candidate_pool",
    "select_winner",
]
