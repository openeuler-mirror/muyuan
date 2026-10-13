"""Candidate task creation from admitted workspace results and frozen contracts."""

from pathlib import Path

from goal_plus.domain.adaptive.models import ExpansionSource
from goal_plus.domain.agents import INNER_AGENT_COMMON
from goal_plus.domain.artifacts import ArtifactRef, WorkspaceAccess
from goal_plus.domain.search import (
    CandidateProposal,
    CandidateTask,
    FrozenSpec,
    SearchPlan,
)
from goal_plus.domain.shared_tools import (
    SHARE_OUT_RELATIVE_PATH,
    TOOL_DRAFTS_RELATIVE_PATH,
)
from goal_plus.domain.worker_policy import (
    resolved_inner_agent_id,
    global_evidence_mode,
    candidate_process_environment,
    selected_model_for_slot,
    selected_model_provenance,
)
from goal_plus.application.candidate_ledger import CandidateLedger
from goal_plus.ports.candidate_creation import (
    CandidateMaterialization,
    CandidateCreationState,
)
from goal_plus.ports.state import CandidateRecord, RunRecord
from goal_plus.ports.workspace_creation import (
    CandidateWorkspaceCreation,
    PreparedCandidateWorkspace,
)


def format_task_skill_listing(paths: list[str | Path]) -> str:
    return ", ".join(f"{Path(path).parent.name}={path}" for path in paths) or "none"


class CandidateCreation:
    def __init__(
        self,
        workspaces: CandidateWorkspaceCreation,
        state: CandidateCreationState,
        ledger: CandidateLedger,
    ) -> None:
        self.workspaces, self.state, self.ledger = workspaces, state, ledger

    def prepare_task(
        self,
        run: RunRecord,
        frozen: FrozenSpec,
        candidate_id: str,
        plan: SearchPlan,
        proposal: CandidateProposal,
        slot: int,
        *,
        expansion_source: ExpansionSource | None = None,
        source_record: CandidateRecord | None = None,
    ) -> CandidateTask:
        request = CandidateMaterialization(
            run,
            frozen,
            candidate_id,
            plan,
            proposal,
            slot,
            expansion_source,
            source_record,
        )
        prepared = self.workspaces.prepare(request)
        if frozen.spec.workspace.provider == "thinkthread":
            return self._thinkthread_task(request, prepared)
        materialization = prepared.materialization
        workspace_creation_timing = prepared.timing
        process_environment = candidate_process_environment(frozen, slot - 1)
        if (
            expansion_source is None
            and frozen.source_artifact_ref is not None
            and materialization.base_revision != frozen.source_artifact_ref.id
        ):
            raise RuntimeError(
                "FrozenSourceChanged: materialized candidate base revision "
                f"{materialization.base_revision!r} does not match frozen source "
                f"artifact {frozen.source_artifact_ref.id!r}"
            )
        if expansion_source is not None and (
            materialization.base_revision
            != expansion_source.attempt_artifact_ref.id
            or materialization.workspace_provider
            != expansion_source.attempt_artifact_ref.provider
        ):
            raise RuntimeError(
                "ExpansionSourceChanged: materialized candidate base does not "
                "match the selected expansion artifact"
            )
        if (
            run.source_artifact_ref is None
            and materialization.base_revision is not None
        ):
            artifact_kind = (
                "fs_snapshot"
                if materialization.workspace_provider == "thinkthread"
                else "git_commit"
            )
            run.source_artifact_ref = ArtifactRef(
                kind=artifact_kind,
                id=materialization.base_revision,
                provider=materialization.workspace_provider,
            )
        run.source_subdir = materialization.source_subdir
        repository_value = (materialization.provider_handle or {}).get(
            "source_repository"
        )
        if repository_value:
            run.source_repository = Path(repository_value)
        workspace = materialization.workspace
        task_skill_paths = self.workspaces.task_skill_paths(frozen, workspace)
        inner_agent = str(
            plan.worker_policy.get("inner_agent")
            or resolved_inner_agent_id(frozen.spec.strategy, frozen.agent_harness, frozen.runtime_provider)
        )

        instructions = [
            "candidate_task.workspace 是当前候选工作区；工作区内的所有文件都可读取和分析。",
            "candidate_task.acceptance 是 FrozenSpec 中显式声明、受界且对 worker 可见的验收条件；目标和 metric 使用 context 顶层字段。",
            "host launch 不保证切换 cwd；每次文件和 shell 操作都显式使用 candidate_task.workspace。",
            "交付修改仅限 candidate_task.allowed_files。candidate_task.denied_files 和冻结的 verifier 资产保持只读，但可读取、导入和 profile。",
            "workspace/.tmp/ 是 runtime 提供的 candidate-local 可写临时区，不属于交付 edit surface；可用于 scratch，并在计划变化或收尾前用简短 JSON 更新 .tmp/handoff.json，供后续恢复。",
            "可在不修改只读文件的前提下，运行候选工作区内的测试、runner、evaluator 及其他本地诊断；这些结果只属于当前 candidate，不会进入 Global Evidence、被其他 worker 看到或参与最终选择。",
            "在分配的 host budget 内工作；收到用户停止、run 失效或 host 截止提示时及时收尾。",
            "窄代码证据可以直接比较 commit；改动较大或需要浏览完整实现时，调用 goal_plus_search_reference_open 把该 Evidence checkout 到当前 agent 唯一的 ref-workspace，并直接用普通文件和命令工具查看返回路径。再次调用会把同一目录切到新的 reference commit；所有实现修改仍留在 candidate workspace。不要访问或 fetch peer 的真实工作区。",
        ]
        if inner_agent == INNER_AGENT_COMMON:
            instructions.extend(
                [
                    "本次 host dispatch 只完成一个有界 attempt：分析当前状态、修改允许的 product 文件，并使用当前 agent_session_id 通过 goal_plus_search_run_verifier 提交验证。",
                    "用一句话 hypothesis 描述实际完成的尝试；只有 verifier 返回结果会结算为正式 Evidence。收到结果后更新简洁 handoff 并返回，不要自行开始下一轮。",
                ]
            )
        else:
            instructions.extend(
                [
                    "Frozen verifier 是唯一权威验证路径。每项实质变更后，及时调用 goal_plus_search_run_verifier(agent_session_id=...)，使用当前 agent_session_id 并用一句话 hypothesis 描述实际尝试；只有其返回结果会结算为正式 Evidence 并贡献给共享搜索。对初始化较慢但可能高收益的路线，不要仅凭未完成的本地试探判定失败；剩余时间和 verifier 时限允许时，至少完成一次权威验证后再决定是否放弃。以 verifier 返回后的工作区状态为准，继续下一轮，不要自行恢复已结算状态。",
                    "把候选思路当作初始假设，自主分析、修改、验证和转向；不要仅因一次失败或同分而停止。",
                ]
            )
        instructions.append(
            "提交验证后保持工作区不变，按回执 candidate_action 行动：wait_for_verifier 使用原 attempt_id 调用 goal_plus_search_run_verifier 等待；end_turn 结束当前 turn，等待宿主送回结果；handoff_to_main 向 Main 交接。工具异常且没有 attempt_id 时，通过 goal_plus_search_get_agent_context 核对 verification，不重复提交。"
        )
        if process_environment:
            instructions.append(
                "candidate_task.process_environment 是本 candidate 的进程环境；运行本地命令时显式应用这些变量。goal_plus_search_run_verifier 会自动使用同一环境。"
            )
        if "success_threshold" in frozen.spec.constraints:
            instructions.append(
                "达到 success_threshold 表示用户目标已经满足，不会强制中断当前 iteration；若剩余预算足以完成一轮有价值的修改和验证，可继续优化，否则正常收尾。"
            )
        evidence_mode = global_evidence_mode(frozen.spec.strategy.config)
        if evidence_mode == "manual":
            instructions.append(
                "Global Evidence 使用 manual 模式：首次修改前调用 goal_plus_search_get_global_evidence；此后每完成 3 次 goal_plus_search_run_verifier 刷新一次，连续两轮没有提升或准备切换技术路线时提前刷新。"
            )
        elif evidence_mode == "auto":
            instructions.append(
                "Global Evidence 使用 auto 模式：verifier 结算回包中的 global_evidence_snapshot 已自动注入最新共享 View，直接使用该快照继续。"
            )
        if frozen.spec.strategy.orchestration_mode == "adaptive_search":
            instructions.append(
                "Adaptive Search 已启用：LLM Verifier backlog 不阻塞当前 lane，按正常 candidate 循环继续。只有 runtime 或 host 明确拒绝、fence 或中断当前 lane 时才收尾；不要轮询 Verifier task 或 AllocationDecision，也不要自行评分、分配资源或启动派生 worker。"
            )
        if frozen.supplemental_evaluation_enabled:
            instructions.append(
                "开放式补充评价已启用；仅对 Evidence 中 supplemental_available=true 且确需深挖的条目调用 goal_plus_search_get_evidence_detail。补充评价不是硬分或 promotion gate。"
            )
        if task_skill_paths:
            instructions.append(
                "当前 worktree 可见的 Git-tracked task-local Skills: "
                f"{format_task_skill_listing(task_skill_paths)}。逐一完整读取这些 "
                "SKILL.md，再按需读取各自目录内的相对资源并显式遵循；不要读取 "
                "parent、global/plugin 或其他 candidate 的 Skill。"
            )
        else:
            instructions.append(
                "当前 worktree 未发现 Git-tracked task-local Skills "
                "(.agents/skills/*/SKILL.md)。"
            )
        share_out_dir = None
        if frozen.spec.shared_dir.enabled:
            share_out_dir = self.workspaces.initialize_shared_layout(
                run.run_id, workspace
            )
            instructions.extend(
                [
                    "shared_dir 发布方规则：每次 verifier 前回顾尚未发布的命令、临时代码和测试材料；凡能在 peer workspace 独立运行并降低同一 run 内诊断、验证或复现成本的 checker、fixture、probe、parser 或多步 workflow，默认提炼为最小工具。按用途而非文件名分类；不得发布 candidate 产品实现、冻结 verifier/runner/grader、隐藏答案或评分逻辑、日志、原始数据、凭据或构建输出。",
                    f"将显式源文件及依赖放在 {TOOL_DRAFTS_RELATIVE_PATH}/，再用 goal_plus_search_stage_shared_tool 生成 {SHARE_OUT_RELATIVE_PATH}/ staging。首次发布使用 publication_intent=new；更新 family 时只使用 tool_family_catalog 返回的合法 revision_head 和 revision_blocker。capability_extension/contract_change 需要新增稳定语义键，adoption_fix 需要真实采用事实和具体缺陷，不得用改名或同义键制造增量。",
                    "not_applicable 使用工具 schema 中最准确的 exclusion：no_toolizable_material 表示累计回顾后没有工具；existing_family_sufficient 表示现有 family 已覆盖需求；no_material_tool_delta 表示已有 family 没有实质 revision；draft_not_ready 只表示具体的安全、完整、可移植或 peer 可运行性阻塞。",
                    "每次归属于当前 worker 的 process verifier 都提交与实际 staging 一致的 toolization_decision。runtime 以 staging inventory 和 publication settlement 为权威；decision 缺失或不匹配只生成 advisory，不改变 score、disposition、selection 或 promotion。",
                    "shared_dir 采用方规则：通过 Global Evidence 的 Tool View 发现工具；Tool View 不代表推荐或源码行为。可能相关时用 goal_plus_search_copy_shared_tool 和准确的 tool_id、snapshot_hash 复制快照，检查 manifest 和源码后自主决定是否采用；receipt 只表示复制，不表示采用，也不改变 verifier 结算或硬分。",
                ]
            )
        if frozen.spec.shared_cache.enabled:
            cache_sections = "；".join(
                f"{section.key}（{section.title}：{section.requirement}）"
                for section in frozen.spec.shared_cache.sections
            )
            instructions.extend(
                [
                    f"shared_cache 已启用。操作者冻结了以下客观信息模板：{cache_sections}。用 goal_plus_search_read_shared_cache 随时查阅，用 goal_plus_search_append_shared_cache 追加或（以 supersedes）修正。",
                    "shared_cache 记录完全自愿，不影响 verifier 评分、selection 或 promotion；它唯一的用途是让你自己和同 run 的后续 worker 少走弯路。不要为写而写：当你得到对完成本任务有用、且用一句客观陈述就能说清的事实（环境行为、踩过的坑、可复用的参数/命令、一个被证伪的假设）时，顺手记一条，同轮其余时间照常工作。",
                    "探索新方向前先读一次 cache：你准备做的第一步若已有记录，直接从记录继续而不是从零重试。verifier 返回后附带最新 cache 快照，留意其中与你当前方向相关的条目。",
                ]
            )
        instructions.extend(proposal.instructions)
        hypothesis = proposal.hypothesis or proposal.intent or f"候选 {candidate_id}"
        selected_model = (
            source_record.task.selected_model
            if source_record is not None
            else selected_model_for_slot(plan, slot)
        )
        return CandidateTask(
            run_id=run.run_id,
            candidate_id=candidate_id,
            parent_id=(
                expansion_source.candidate_id if expansion_source is not None else None
            ),
            parent_candidate_ids=(
                [
                    *source_record.task.parent_candidate_ids,
                    source_record.candidate_id,
                ]
                if source_record is not None
                else []
            ),
            base_candidate_id=(
                expansion_source.candidate_id if expansion_source is not None else None
            ),
            plan_id=plan.plan_id,
            hypothesis=hypothesis,
            workspace=workspace,
            workspace_provider=materialization.workspace_provider,
            workspace_branch=materialization.branch,
            workspace_base_revision=materialization.base_revision,
            workspace_managed_root=materialization.managed_root,
            workspace_source_subdir=materialization.source_subdir,
            workspace_provider_handle=dict(materialization.provider_handle or {}),
            workspace_creation_timing=workspace_creation_timing,
            task_skill_paths=task_skill_paths,
            allocation_depth=(
                source_record.task.allocation_depth + 1
                if source_record is not None
                else 0
            ),
            expansion_source=expansion_source,
            base_artifact_ref=(
                ArtifactRef(
                    kind=(
                        "fs_snapshot"
                        if materialization.workspace_provider == "thinkthread"
                        else "git_commit"
                    ),
                    id=materialization.base_revision,
                    provider=materialization.workspace_provider,
                )
                if materialization.base_revision is not None
                else None
            ),
            workspace_access=WorkspaceAccess(
                logical_root="workspace",
                native_cwd=workspace,
                bootstrap_required=frozen.agent_harness == "codex",
                confinement="protected_execution",
                writable_surfaces=list(frozen.spec.edit_surface.allow),
                managed_root=materialization.managed_root,
                source_subdir=materialization.source_subdir,
                provider_handle=dict(materialization.provider_handle or {}),
            ),
            share_out_dir=share_out_dir,
            allowed_files=frozen.spec.edit_surface.allow,
            denied_files=frozen.spec.edit_surface.deny,
            acceptance=frozen.spec.candidate_acceptance,
            process_environment=process_environment,
            instructions=instructions,
            expected_artifacts=["patch", "notes", "logs"],
            stop_conditions={},
            proposal=proposal,
            selected_model=selected_model,
            model_provenance=(
                selected_model_provenance(selected_model) if selected_model else {}
            ),
            strategy_metadata={
                "strategy": plan.strategy.name,
                "worker_policy": plan.worker_policy,
                "plan_id": plan.plan_id,
                "slot": slot,
                "selected_model": selected_model.model if selected_model else None,
                "workspace_provider": materialization.workspace_provider,
                "workspace_branch": materialization.branch,
                "workspace_base_revision": materialization.base_revision,
                "allocation_depth": (
                    source_record.task.allocation_depth + 1
                    if source_record is not None
                    else 0
                ),
                "allocation_source_candidate_id": (
                    expansion_source.candidate_id
                    if expansion_source is not None
                    else None
                ),
            },
        )

    def _thinkthread_task(
        self,
        request: CandidateMaterialization,
        prepared: PreparedCandidateWorkspace,
    ) -> CandidateTask:
        run, frozen, candidate_id = request.run, request.frozen, request.candidate_id
        plan, proposal, slot = request.plan, request.proposal, request.slot
        expansion_source, source_record = request.expansion_source, request.source_record
        inner_agent = str(
            plan.worker_policy.get("inner_agent")
            or resolved_inner_agent_id(frozen.spec.strategy, frozen.agent_harness, frozen.runtime_provider)
        )
        base_revision = prepared.materialization.base_revision
        baseline = (
            expansion_source.attempt_artifact_ref
            if expansion_source is not None
            else run.source_artifact_ref
        )
        if (
            baseline is None
            or baseline.kind != "fs_snapshot"
            or baseline.provider != "thinkthread"
            or base_revision != baseline.id
        ):
            raise RuntimeError(
                "ThinkThread candidate requires its exact base snapshot"
            )
        logical_workspace = prepared.materialization.workspace
        provider_handle = {
            **run.source_provider_handle,
            "base_snapshot_id": baseline.id,
            "candidate_id": candidate_id,
        }
        instructions = [
            "当前 candidate 运行在 ThinkThread private Child；文件操作使用 Child 自己的受保护工作区。",
            "public candidate context 不提供本地 workspace 路径；以 workspace_access.logical_root='.' 和 source_subdir 定位源码。",
            "只修改 candidate_task.allowed_files，保持 candidate_task.denied_files 与冻结 verifier 资产不变。",
            "Root live tree、sibling Child 和 sibling Session 不属于当前候选的可见范围。",
            "每项实质变更后调用 goal_plus_search_run_verifier；它只会立即返回带稳定 attempt_id 的 accepted receipt。收到 accepted 后立即结束当前 turn，不再写入；Root controller 只能在 quiescent boundary 捕获 exact Child snapshot 并验证。",
            "accepted receipt 不是验证结果。完整 ScoreReport、attempt snapshot、恢复后的 workspace snapshot、score 与 disposition 会在同一 retained Child 的下一次 wake 中以 authoritative JSON 强制注入；先据此决定下一轮，不依赖自行刷新 context。",
            "worker 文本输出和 live branch 不构成 Evidence；Search context 与 iteration history 可用于核对已注入的 verifier feedback。",
        ]
        if inner_agent == INNER_AGENT_COMMON:
            instructions.extend(
                [
                    "本次 host dispatch 只完成一个有界 attempt；收到 accepted receipt 后结束当前 turn，不要自行开始下一轮。",
                    "后续 wake 先消费注入的 verifier feedback，再按 continuation 指令完成下一个有界 attempt；最终选择与 publication 属于 Root controller。",
                ]
            )
        else:
            instructions.extend(
                [
                    "把候选思路当作初始假设，在 host budget 内自主分析、修改、验证和转向；不要仅因一次失败或同分而停止。",
                    "每次 wake 先消费注入的 verifier feedback，再继续同一条自主搜索循环；最终选择与 publication 属于 Root controller。",
                    "当前内层实现是 AutoResearch-Git：trusted host adapter 在 Child 内维护 private .git attempt history 和 results.tsv。可以只读检查这些历史，但不要自行 commit、reset、rebase、改写 refs 或编辑 results.tsv。",
                ]
            )
        evidence_mode = global_evidence_mode(frozen.spec.strategy.config)
        if evidence_mode == "manual":
            instructions.append(
                "Global Evidence 使用 manual 模式：首次修改前读取，此后按 verifier 节奏刷新。"
            )
        elif evidence_mode == "auto":
            instructions.append(
                "Global Evidence 使用 auto 模式：在下一次 wake 后从已加载 context 使用最新共享 View；accepted receipt 不包含尚未产生的 Evidence。"
            )
        instructions.extend(proposal.instructions)
        selected_model = (
            source_record.task.selected_model
            if source_record is not None
            else selected_model_for_slot(plan, slot)
        )
        hypothesis = proposal.hypothesis or proposal.intent or f"候选 {candidate_id}"
        return CandidateTask(
            run_id=run.run_id,
            candidate_id=candidate_id,
            parent_id=(
                expansion_source.candidate_id if expansion_source is not None else None
            ),
            parent_candidate_ids=(
                [*source_record.task.parent_candidate_ids, source_record.candidate_id]
                if source_record is not None
                else []
            ),
            base_candidate_id=(
                expansion_source.candidate_id if expansion_source is not None else None
            ),
            plan_id=plan.plan_id,
            hypothesis=hypothesis,
            execution_scope="thinkthread_private",
            workspace=logical_workspace,
            workspace_provider="thinkthread",
            workspace_base_revision=baseline.id,
            workspace_source_subdir=str(
                run.source_provider_handle.get("source_relative_path") or "."
            ),
            workspace_provider_handle=provider_handle,
            allocation_depth=(
                source_record.task.allocation_depth + 1
                if source_record is not None
                else 0
            ),
            expansion_source=expansion_source,
            base_artifact_ref=baseline,
            workspace_access=WorkspaceAccess(
                logical_root=".",
                native_cwd=None,
                bootstrap_required=False,
                confinement="protected_execution",
                writable_surfaces=list(frozen.spec.edit_surface.allow),
                managed_root=None,
                source_subdir=str(
                    run.source_provider_handle.get("source_relative_path") or "."
                ),
                provider_handle=provider_handle,
            ),
            share_out_dir=None,
            allowed_files=frozen.spec.edit_surface.allow,
            denied_files=frozen.spec.edit_surface.deny,
            instructions=instructions,
            expected_artifacts=["fs_snapshot", "notes", "logs"],
            proposal=proposal,
            selected_model=selected_model,
            model_provenance=(
                selected_model_provenance(selected_model) if selected_model else {}
            ),
            strategy_metadata={
                "strategy": plan.strategy.name,
                "worker_policy": plan.worker_policy,
                "plan_id": plan.plan_id,
                "slot": slot,
                "selected_model": selected_model.model if selected_model else None,
                "workspace_provider": "thinkthread",
                "workspace_base_revision": baseline.id,
                "allocation_depth": (
                    source_record.task.allocation_depth + 1
                    if source_record is not None
                    else 0
                ),
                "allocation_source_candidate_id": (
                    expansion_source.candidate_id
                    if expansion_source is not None
                    else None
                ),
            },
        )

    def materialize(self, request: CandidateMaterialization) -> CandidateRecord:
        """Create one admitted candidate under the caller's held run lock.

        This use case owns workspace/initial ledger creation, not Allocation
        policy or worker lifecycle. Source coordinates are returned on request.run.
        """
        run, frozen, candidate_id = request.run, request.frozen, request.candidate_id
        try:
            record = self.state.read_candidate(run.run_id, candidate_id)
        except FileNotFoundError:
            task = self.prepare_task(
                run=run,
                frozen=frozen,
                candidate_id=candidate_id,
                plan=request.plan,
                proposal=request.proposal,
                slot=request.slot,
                expansion_source=request.expansion_source,
                source_record=request.source_record,
            )
            record = CandidateRecord(
                candidate_id=candidate_id,
                status="created",
                task=task,
                results_ledger=(
                    request.results_ledger
                    if request.results_ledger is not None
                    else self.ledger.inherited(run, task)
                ),
            )
        else:
            if (record.task.expansion_source != request.expansion_source
                or record.task.proposal is None
                or record.task.proposal.metadata.get("allocation_decision_id") != request.proposal.metadata.get("allocation_decision_id")):
                raise RuntimeError("candidate materialization identity collision")
            if record.initial_attempt_base_artifact_ref is not None:
                record.allocation_eligibility = "eligible"
                record.retired_by_decision_id = None
                self.state.write_candidate(run.run_id, record)
                return record
        record.allocation_eligibility = "eligible"
        record.retired_by_decision_id = None
        # Persist the exact parent/child identity before workspace or ledger
        # mutation. Recovery can then resume from ExpansionSource after either
        # side effect fails.
        self.state.write_candidate(run.run_id, record)
        self.ledger.ensure(frozen, record)
        record.initial_attempt_base_artifact_ref = (
            record.results_ledger_artifact_ref
        )
        self.state.write_candidate(run.run_id, record)
        return record
