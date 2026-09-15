"""Host payload and bounded workspace/asset reads for admitted sessions."""

import json

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib
from pathlib import Path
from typing import Any
from goal_plus.agent_hosts import candidate_launch_adapter
from goal_plus.infrastructure.host_bindings import candidate_inner_agent_binding
from goal_plus.application.candidate_creation import format_task_skill_listing
from goal_plus.domain.agents import INNER_AGENT_COMMON
from goal_plus.domain.search import AgentSessionRecord, FrozenSpec
from goal_plus.paths import host_assets_root
from goal_plus.ports.candidate_host import ResolvedInnerAgentBinding
from goal_plus.ports.candidate_sessions import (
    MODEL_HANDOFF_RELATIVE_PATH,
    MAX_MODEL_HANDOFF_BYTES,
)
from goal_plus.ports.state import CandidateRecord


class HostCandidateLaunch:
    def __init__(self, root_dir: Path | str) -> None:
        self.root_dir = Path(root_dir).resolve()

    def _candidate_worker_launch(
        self,
        frozen: FrozenSpec,
        candidate_record: CandidateRecord,
    ) -> dict[str, Any] | None:
        worker_policy = candidate_record.task.strategy_metadata.get("worker_policy", {})
        launch = worker_policy.get("worker_launch")
        base = (
            dict(launch)
            if launch is not None
            else (
                (
                    frozen.spec.strategy.worker_launch.model_dump(
                        mode="json", exclude_none=True
                    )
                    if frozen.spec.strategy.worker_launch
                    else None
                )
                or {}
            )
        )
        selected_launch = candidate_record.task.model_provenance.get("worker_launch")
        if isinstance(selected_launch, dict):
            base.update(
                {
                    key: value
                    for key, value in selected_launch.items()
                    if value is not None
                }
            )
        return base or None

    def prepare(
        self,
        frozen: FrozenSpec,
        candidate_id: str,
        agent_session_id: str,
        directive: dict[str, Any],
        candidate_record: CandidateRecord,
        worker_budget_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        binding = candidate_inner_agent_binding(frozen, candidate_record.task)
        proposal = candidate_record.task.proposal
        if proposal is not None and proposal.intent:
            short_intent = proposal.intent
        elif directive.get("goal"):
            short_intent = str(directive["goal"])
        else:
            short_intent = candidate_record.task.hypothesis

        idea_lines: list[str] = []
        if proposal is not None and proposal.intent:
            idea_lines.append(f"candidate_intent: {proposal.intent}")
            if proposal.hypothesis:
                idea_lines.append(f"candidate_hypothesis: {proposal.hypothesis}")
            if proposal.expected_tradeoff:
                idea_lines.append(f"expected_tradeoff: {proposal.expected_tradeoff}")
            if proposal.instructions:
                idea_lines.append(
                    "candidate_instructions: " + " | ".join(proposal.instructions)
                )
        else:
            idea_lines.append(
                f"candidate_hypothesis: {candidate_record.task.hypothesis}"
            )
        if directive:
            idea_lines.extend(
                f"main_directive.{key}: {value}" for key, value in directive.items()
            )
        idea_lines.append(
            "task_local_skills_visible: "
            + format_task_skill_listing(candidate_record.task.task_skill_paths)
        )
        one_paragraph_idea = "; ".join(idea_lines)

        adapter = candidate_launch_adapter(frozen.agent_harness, frozen.runtime_provider)
        worker_budget = worker_budget_override
        return adapter.build_launch_payload(
            candidate_id=candidate_id,
            agent_session_id=agent_session_id,
            short_intent=short_intent,
            one_paragraph_idea=one_paragraph_idea,
            worker_budget=worker_budget,
            worker_launch=self._candidate_worker_launch(frozen, candidate_record),
            root=str(self.root_dir),
            worker_prompt=self._worker_prompt_for_binding(binding),
        )

    def continue_session(
        self,
        frozen: FrozenSpec,
        session: AgentSessionRecord,
        candidate_record: CandidateRecord,
        worker_budget_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        binding = candidate_inner_agent_binding(frozen, candidate_record.task)
        if binding.inner_agent_id == INNER_AGENT_COMMON:
            short_intent = "在同一 candidate 中完成下一个有界尝试"
            directive_text = (
                "沿用当前 candidate、workspace、运行时上下文和 Evidence，完成一个新的"
                "有界尝试；调用一次 goal_plus_search_run_verifier 后返回，不要自行开始下一轮。"
            )
        else:
            short_intent = "继续同一条自主候选循环"
            directive_text = (
                "沿用已加载的运行时上下文和 Evidence 继续同一条自主搜索循环，"
                "探索下一个有证据支持的方向，并验证每项实质变更。"
            )

        adapter = candidate_launch_adapter(session.agent_harness, session.runtime_provider)
        worker_budget = worker_budget_override
        return adapter.build_continue_payload(
            candidate_id=session.candidate_id,
            agent_session_id=session.agent_session_id,
            external_id=session.session_handle.external_id,
            short_intent=short_intent,
            one_paragraph_idea=directive_text,
            root=str(self.root_dir),
            worker_prompt=self._worker_prompt_for_binding(binding),
            worker_budget=worker_budget,
            worker_launch=self._candidate_worker_launch(frozen, candidate_record),
        )

    def _worker_prompt_for_binding(
        self,
        binding: ResolvedInnerAgentBinding,
    ) -> str | None:
        repository_root = host_assets_root()
        if binding.prompt_asset is None:
            return None
        prompt_path = repository_root / binding.prompt_asset
        if binding.agent_harness == "codex":
            try:
                payload = tomllib.loads(prompt_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
                raise RuntimeError(
                    "InnerAgentAssetUnavailable: could not load "
                    f"{binding.inner_agent_id!r} prompt asset {prompt_path}"
                ) from exc
            prompt = payload.get("developer_instructions")
            if not isinstance(prompt, str) or not prompt.strip():
                raise RuntimeError(
                    "InnerAgentAssetUnavailable: Codex prompt asset has no "
                    f"developer_instructions: {prompt_path}"
                )
            return prompt
        try:
            prompt = prompt_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError(
                "InnerAgentAssetUnavailable: could not load "
                f"{binding.inner_agent_id!r} prompt asset {prompt_path}"
            ) from exc
        if not prompt.strip():
            raise RuntimeError(
                f"InnerAgentAssetUnavailable: prompt asset is empty: {prompt_path}"
            )
        return prompt

    @staticmethod
    def read_handoff(workspace: Path) -> tuple[dict[str, Any] | None, str | None]:
        """Read a bounded candidate-authored handoff without failing handle binding."""
        workspace = workspace.resolve()
        handoff_path = workspace / MODEL_HANDOFF_RELATIVE_PATH
        if not handoff_path.exists():
            return None, None
        try:
            resolved = handoff_path.resolve(strict=True)
            if not resolved.is_relative_to(workspace):
                return None, "handoff path resolves outside the candidate workspace"
            if resolved.stat().st_size > MAX_MODEL_HANDOFF_BYTES:
                return None, f"handoff exceeds {MAX_MODEL_HANDOFF_BYTES} bytes"
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, f"could not read handoff: {type(exc).__name__}: {exc}"
        if not isinstance(payload, dict):
            return None, "handoff must be a JSON object"
        return payload, None
