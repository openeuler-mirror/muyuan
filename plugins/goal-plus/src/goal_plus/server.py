from __future__ import annotations

import argparse
import json
from functools import wraps
from pathlib import Path
from typing import Annotated, Any, Literal, TYPE_CHECKING

from mcp.types import TextContent
from pydantic import Field

from goal_plus._version import __version__
from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.domain.agents import AgentHostKind
from goal_plus.domain.search import (
    CandidateProposal,
    SearchSpec,
)
from goal_plus.domain.goal import (
    GoalPlusSpecDraftInput,
    GoalPlusStatus,
    GoalPlusTriage,
    GoalPlusWorkEventKind,
)
from goal_plus.domain.shared_tools import ToolizationDecision
from goal_plus.paths import DEFAULT_RUNTIME_ROOT
from goal_plus.runtime import FileSearchRuntime
from goal_plus.tools import GoalPlusTools, SearchTools
from goal_plus.infrastructure.api_debug import api_debug_enabled, log_api_response_size
from goal_plus.projections.goal_tools import GOAL_HISTORY_DEFAULT_LIMIT, GOAL_HISTORY_MAX_LIMIT

if TYPE_CHECKING:
    from fastmcp import FastMCP


def _json_text(value: Any) -> TextContent:
    return TextContent(
        type="text",
        text=json.dumps(value, ensure_ascii=False, separators=(",", ":")),
    )


def _debug_tool_response(function):
    @wraps(function)
    def measured(*args, **kwargs):
        result = function(*args, **kwargs)
        if api_debug_enabled():
            try:
                text = result.text if isinstance(result, TextContent) else result
                log_api_response_size(
                    function.__name__,
                    text if isinstance(text, str) else json.dumps(
                        text, ensure_ascii=False, separators=(",", ":")
                    ),
                    transport="mcp",
                    format="text" if isinstance(text, str) else "json",
                )
            except (TypeError, ValueError):
                pass
        return result

    return measured


def create_mcp(
    root_dir: str | Path = DEFAULT_RUNTIME_ROOT,
) -> FastMCP:
    from fastmcp import FastMCP

    runtime = FileSearchRuntime(root_dir)
    goal_runtime = FileGoalPlusRuntime(root_dir)
    tools = SearchTools(runtime, goal_runtime)
    goal_tools = GoalPlusTools(goal_runtime)
    mcp = FastMCP("goal-plus", version=__version__)

    @mcp.tool()
    @_debug_tool_response
    def search_freeze_spec(
        goal_plus_id: str,
        spec: SearchSpec,
        verifier_artifact_paths: list[str],
    ) -> dict[str, Any]:
        """将 SearchSpec 及其 verifier 文件冻结为不可变 bundle。

        `goal_plus_id` 必须是当前 host session 绑定的 active Goal。冻结前校验 worker host、
        workspace backend 和 source 状态；`git_worktree`
        要求整个 source repository 对应 clean HEAD，dirty source 会在 verifier preflight
        前失败。ThinkThread 仅接受匹配的 Pi ThinkThread host/Profile，并在 create 时通过
        exact Agent POSIX preflight 固定 Root-owned snapshot；Codex/Pi RPC 不能混用该 backend。
        返回 `frozen_spec_id`、`task_skill_notice` 和 `workspace_capacity_notice`，应在
        `search_create` 前调用。后两者分别列出 source Git index 中
        `.agents/skills/*/SKILL.md` 的自动发现结果，以及按 `budget.max_parallel` 计算的
        source/worktree/object-store 容量预检。verifier 文件按 hash 固定；
        `verifier_artifact_paths` 必须使用相对 `source_path` 的路径，不能越界；
        候选执行期间修改它们会强制分数为 0.0。冻结会预检每个 `ranking_signal`：
        它必须以 0 退出，并输出一个最终 JSON 对象，其中包含有限数值类型的
        `spec.metric_name`，例如 `{"score": 123.0}`。本地 process suite 即使未声明
        ranking_signal，也必须在冻结前实际产出有限数值分数。分数从 stdout 的单行
        JSON 对象读取，不解析文本分数或输出文件。verifier 命令在一次性源码副本中
        运行，不能改变该工作区。编译器产物和临时输出应放入每次调用唯一的
        `GOAL_PLUS_VERIFIER_TMPDIR`、`TMPDIR`、`TMP` 或 `TEMP`；并发验证候选时固定
        `/tmp` 路径不安全。可选自定义 verifier 必须在 Spec Discovery 期间写入源码拥有
        的路径，绝不能放在 `.gp` 或 `.search`。`expected_outputs` 只包含产物路径/glob，
        不是 stdout parser 配置。`spec.budget.max_parallel` 唯一决定初始候选 Agent 数。
        可选 `device_pools` 只能引用 host 通过 `GOAL_PLUS_DEVICE_POOLS` 注册的 pool ID；
        每个引用 pool 至少提供 `spec.budget.max_parallel` 个设备，且不能与另一个引用 pool
        共用同一可见变量。需要 worker 遵守的公开条件写入 `candidate_acceptance`；
        `constraints` 保持 runtime 私有。
        """
        return tools.search_freeze_spec(goal_plus_id, spec, verifier_artifact_paths)

    @mcp.tool()
    @_debug_tool_response
    def goal_plus_list_models(
        host: AgentHostKind,
        query: str | None = None,
    ) -> dict[str, Any]:
        """列出指定 Codex/Pi host 当前可用的模型。

        `/goal-plus models=...` 在冻结 spec 前调用它。返回的 `model` 是可写入
        `strategy.models[].model` 的精确 host 引用；不存在或有歧义的请求会在冻结时
        直接失败。省略 `query` 返回完整清单。
        """
        return tools.goal_plus_list_models(host, query)

    @mcp.tool()
    @_debug_tool_response
    def search_create(
        goal_plus_id: str,
        frozen_spec_id: str,
        source_run_id: str | None = None,
    ) -> dict[str, str]:
        """从冻结 spec 启动 Search run，并返回 `run_id`。

        `goal_plus_id` 必须与 freeze 使用的当前 active Goal 相同。启动前复检 worker host、
        workspace backend 和 source Git 状态；当前 Codex/Pi RPC
        请求 ThinkThread，或 Git-backed source 在 freeze 后变 dirty，都会在创建 run 或
        candidate checkout 前失败。
        创建时会在 `GOAL_PLUS_VERIFIER_RESOURCE_LOCK_DIR` 下独占冻结 spec 引用的 device pool。
        多个进程或容器共享设备时必须映射同一个 host 锁目录。只有无法避免新的不可变 run
        时才传入 `source_run_id`。新 run 会收到来源 frontier、
        限定范围的问题和特性账本的有界 snapshot。来源分数只是历史，必须在新契约下重新验证。
        """
        return tools.search_create(goal_plus_id, frozen_spec_id, source_run_id)

    @mcp.tool()
    @_debug_tool_response
    def search_status(run_id: str) -> dict[str, Any]:
        """返回 run 状态、预算使用和最佳分数的只读 snapshot。"""
        return tools.search_status(run_id)

    @mcp.tool()
    @_debug_tool_response
    def search_recovery_status(run_id: str) -> dict[str, Any]:
        """读取各 candidate 的完整结算恢复点、generation 和 host session，不启动或回滚任何工作。"""
        return tools.search_recovery_status(run_id)

    @mcp.tool()
    @_debug_tool_response
    def search_prepare_recovery(run_id: str, candidate_id: str) -> dict[str, Any]:
        """固定中断 candidate 的结算恢复点并隔离旧 generation，返回 recovery_id。

        随后由 host 停止并确认旧 worker 结束，再调用 search_recover_candidate。
        """
        return tools.search_prepare_recovery(run_id, candidate_id)

    @mcp.tool()
    @_debug_tool_response
    def search_recover_candidate(run_id: str, candidate_id: str, recovery_id: str) -> dict[str, Any]:
        """恢复已结算 artifact，保留 Evidence 与中断现场备份；用同一 recovery_id 重试。

        恢复后由 host 启动并绑定 fresh worker，继续验证。
        """
        return tools.search_recover_candidate(run_id, candidate_id, recovery_id)

    @mcp.tool()
    @_debug_tool_response
    def search_invalidate_run(
        run_id: str,
        reason: Literal[
            "verifier_contract_invalid",
            "verifier_coverage_inadequate",
            "verifier_nondeterministic",
            "verifier_target_mismatch",
            "verifier_infrastructure_failure",
        ],
        summary: str,
        evidence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """主 agent 确认 verifier 不充分后隔离 run。

        该操作会原子地阻止新规划、session、verifier 记录、选择和提升。它不控制 host
        worker：调用后，主 agent 必须中断 host pool，等待每个 worker 达到终态，
        再修复/冻结 verifier，并使用 `source_run_id` 创建新 run。
        """
        return tools.search_invalidate_run(run_id, reason, summary, evidence)

    @mcp.tool()
    @_debug_tool_response
    def goal_plus_monitor_snapshot(
        goal_plus_id: str | None = None,
        run_id: str | None = None,
        stale_after_seconds: int = 600,
        feature_plugins: list[str] | None = None,
        detail: bool = False,
    ) -> dict[str, Any]:
        """返回供轮询 agent 使用的只读 Goal Plus/Search 监控 snapshot。

        从持久化 `.gp` 状态返回 run、candidate、agent-session、verifier、host-log 和 Pi
        usage 证据。默认只返回当前进度、错误和恢复待办；详细统计、历史与宿主诊断
        使用 detail=true。它不会启动、等待或中断 worker。
        """
        return tools.goal_plus_monitor_snapshot(
            goal_plus_id=goal_plus_id,
            run_id=run_id,
            stale_after_seconds=stale_after_seconds,
            feature_plugins=feature_plugins,
            detail=detail,
        )

    @mcp.tool()
    @_debug_tool_response
    def search_list_history(
        run_id: str,
        top_n: int = 5,
        sort_by: str = "score",
    ) -> dict[str, Any]:
        """返回已评估候选及其分数的只读排名列表。"""
        return tools.search_list_history(run_id, top_n, sort_by)

    @mcp.tool()
    @_debug_tool_response
    def search_plan_next(
        run_id: str,
        requested_k: Annotated[
            int,
            Field(
                gt=0,
                description=(
                    "请求初始候选数。运行时按 min(requested_k, budget.max_parallel 的"
                    "剩余可用数量) 进行规划。标准流程必须传入 budget.max_parallel。"
                ),
            ),
        ] = 4,
    ) -> dict[str, Any]:
        """从冻结的整个 run 预算中规划一个候选 batch/轮次。

        `requested_k` 只适用于这一次初始规划。实际 `planned_k` 是 `requested_k` 与
        `budget.max_parallel` 剩余可用数量的最小值。标准流程必须令两者相等。返回
        `plan_id` 和候选任务。
        """
        return tools.search_plan_next(run_id, requested_k)

    @mcp.tool()
    @_debug_tool_response
    def search_start_batch(
        run_id: str,
        plan_id: str,
        proposals: list[CandidateProposal] | None = None,
    ) -> list[dict[str, Any]]:
        """materialize 已规划的候选工作区（`source_path` 的副本）。

        每个返回项拥有隔离工作区和待展示的 `task_skill_notice`；候选编辑必须
        留在其中。不要在 `search_plan_next` 前调用。
        """
        return tools.search_start_batch(run_id, plan_id, proposals)

    @mcp.tool()
    @_debug_tool_response
    def search_start_agent_session(
        run_id: str,
        candidate_id: str,
        directive: dict[str, Any] | str | None = None,
        worker_budget: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """创建 context/provenance handle 和 host 原生 launch payload。

        它不会启动 worker 或跟踪 lifecycle。可选 `worker_budget` 只覆盖本次派发，
        不改变冻结 spec。对所选 host 使用返回的 `launch` payload。prompt 提供的
        `candidate_id` 只是标签；worker 必须从 `search_get_agent_context` 获取权威上下文。
        """
        return tools.search_start_agent_session(
            run_id, candidate_id, directive, worker_budget
        )

    @mcp.tool()
    @_debug_tool_response
    def search_redispatch_candidate(
        run_id: str,
        candidate_id: str,
        worker_budget: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """为现有候选工作区创建新的 worker launch。

        这是状态级恢复，不是同 worker continuation。它为相同 candidate/workspace 返回新的
        `agent_session_id` 和 host launch payload。`worker_budget` 只覆盖本次派发；frozen
        `inner_agent` 和 resolved binding 均保持不变。候选任务 policy 不变。
        """
        return tools.search_redispatch_candidate(
            run_id,
            candidate_id,
            worker_budget,
        )

    @mcp.tool()
    @_debug_tool_response
    def search_bind_agent_handle(
        agent_session_id: str,
        handle: dict[str, Any],
    ) -> dict[str, Any]:
        """把运行时 agent session 绑定到 host worker handle。"""
        return tools.search_bind_agent_handle(agent_session_id, handle)

    @mcp.tool()
    @_debug_tool_response
    def search_get_agent_observability(
        agent_session_id: str,
    ) -> dict[str, Any]:
        """读取一个 agent session 的规范化 host metric 和产物。

        schema 跨 host 共享。可用时 Codex 解析原生 session JSONL；Pi 规范化已绑定的
        `pi_metrics`。此调用只读，不会等待、继续或中断 worker。绝不返回 prompt、
        reasoning 或工具 payload 正文。
        """
        return tools.search_get_agent_observability(agent_session_id)

    @mcp.tool()
    @_debug_tool_response
    def search_continue_agent_session(
        agent_session_id: str,
        worker_budget: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """返回继续已绑定 worker session 所需的 host launch 字段。

        支持原生 continuation 的 host 会复用相同 worker handle。可选 worker budget
        只覆盖本次 continuation 派发。continuation prompt 保持中性，由 worker 选择下一步。
        """
        return tools.search_continue_agent_session(
            agent_session_id,
            worker_budget,
        )

    @mcp.tool()
    @_debug_tool_response
    def search_get_agent_context(agent_session_id: str) -> TextContent:
        """Subagent 的首次调用，返回紧凑的权威工作合同。

        返回 run_id、candidate_id、workspace、candidate_task（含冻结验收合同与 candidate
        进程环境）、最佳/最近 iteration 摘要、canonical .gp ledger 来源和可选 results.tsv
        projection。由 subagent 调用，不由主
        agent 调用。subagent 必须把 prompt 提供的 id 只当作标签，并将此响应作为事实来源。
        """
        return _json_text(tools.search_get_agent_context(agent_session_id))

    @mcp.tool()
    @_debug_tool_response
    def search_get_global_evidence(
        agent_session_id: str,
    ) -> TextContent:
        """返回当前 run 的窄 Global Evidence 视图。

        每项只包含 candidate_id、iteration、score、keep/retain/discard/failure disposition、
        verifier attempt commit、可能延迟的客观 View，以及可选 supplemental evaluation
        的可用标记。任何 View 为 null 都不影响 verifier Evidence；worker 不需要等待，
        可先依据 Evidence 独立探索。
        """
        return _json_text(tools.search_get_global_evidence(agent_session_id))

    @mcp.tool()
    @_debug_tool_response
    def search_reference_open(
        agent_session_id: str,
        source_candidate_id: str,
        source_iteration: Annotated[int, Field(ge=1)],
    ) -> dict[str, Any]:
        """把一条已结算 Evidence checkout 到当前 worker 的 ref-workspace。

        每个 worker 只有一个稳定目录；再次调用会把同一目录切到新的精确 commit。
        返回的 root 可直接使用普通文件和命令工具查看，但不是 peer 的活动 workspace，
        实现修改仍应只发生在 candidate workspace。
        """
        return tools.search_reference_open(
            agent_session_id,
            source_candidate_id,
            source_iteration,
        )

    @mcp.tool()
    @_debug_tool_response
    def search_copy_shared_tool(
        agent_session_id: str,
        tool_id: str,
        snapshot_hash: str,
    ) -> dict[str, Any]:
        """按 Global Evidence 中的精确 id/hash 复制 shared-dir 快照到临时 inbox。

        receipt 由下一次 worker verifier 消费；具体采用契约见 candidate instructions。
        """
        return tools.search_copy_shared_tool(agent_session_id, tool_id, snapshot_hash)

    @mcp.tool()
    @_debug_tool_response
    def search_stage_shared_tool(
        agent_session_id: str,
        name: str,
        summary: str,
        entrypoint: str,
        candidate_relative_source_paths: list[str],
        publication_intent: Literal[
            "new", "capability_extension", "adoption_fix", "contract_change"
        ] = "new",
        supersedes_tool_id: str | None = None,
        capability_ids: list[str] | None = None,
        coverage_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        """将显式的 candidate tool drafts 安全复制到下一次 verifier staging。

        source paths 必须位于当前 candidate 的 `.tmp/tool-drafts/` 下。该工具只执行
        staging；路径和 revision 合法性由 runtime 校验，发布由 passing verifier 结算。
        """
        return tools.search_stage_shared_tool(
            agent_session_id,
            name,
            summary,
            entrypoint,
            candidate_relative_source_paths,
            publication_intent,
            supersedes_tool_id,
            capability_ids,
            coverage_keys,
        )

    @mcp.tool()
    @_debug_tool_response
    def search_get_evidence_detail(
        agent_session_id: str,
        candidate_id: str,
        iteration: Annotated[int, Field(ge=1)],
    ) -> TextContent:
        """按需展开一条已结算 Evidence 的 supplemental evaluation。

        仅当 agent context 声明该能力已开启，且 Global Evidence 对目标行返回
        `supplemental_available=true` 时调用。目标必须属于同一 run；`independent` 模式只
        允许读取调用方自己的 candidate。返回内容不含 peer workspace、transcript 或推理。
        """
        return _json_text(
            tools.search_get_evidence_detail(
                agent_session_id,
                candidate_id,
                iteration,
            )
        )

    @mcp.tool()
    @_debug_tool_response
    def search_run_verifier(
        run_id: str,
        candidate_id: str,
        scope: Literal["process", "promotion"] = "process",
        agent_session_id: str | None = None,
        hypothesis: str | None = None,
        toolization_decision: ToolizationDecision | None = None,
    ) -> dict[str, Any]:
        """Subagent 带 `agent_session_id` 自评分；主流程最终验证不带它。

        subagent 传入准确 `run_id`、`candidate_id`、自己的 `agent_session_id`、一句话
        `hypothesis` 并省略 `scope`，同时遵守 candidate instructions；启用 shared-dir 时
        还可提交结构化
        `toolization_decision`。缺失或与实际 staging 不匹配只记录 advisory，不改变评分、
        结算、选择或 promotion；决策保存在 iteration 中但不进入 Global Evidence。
        hypothesis 应客观概括本轮实际尝试。运行时随后向 canonical `.gp` ledger 追加且只
        追加一条已验证记录；只有 inner binding 声明 results_tsv projection 时才同步并提交
        该 workspace 文件。主 agent 不带 `agent_session_id` 的内部复验不要求 hypothesis；
        `promotion` 只属于主流程。带
        `candidate_action="stop_and_report"` 的 `VerifierWorkspaceSideEffect` 是冻结
        verifier 的基础设施失败：worker 不能清理 verifier 输出或重试。
        """
        return tools.search_run_verifier(
            run_id,
            candidate_id,
            scope,
            agent_session_id,
            hypothesis,
            toolization_decision,
        )

    @mcp.tool()
    @_debug_tool_response
    def search_list_iterations(
        agent_session_id: str,
    ) -> TextContent:
        """返回当前 agent session 所属候选可能很大的完整 iteration 历史。

        candidate worker 只应查询 agent context 中自己的 run/candidate，并且仅在紧凑
        context、可选 ledger projection 和 Git 无法回答旧轮次的准确 verifier 事实时调用。
        """
        return _json_text(tools.search_list_iterations(agent_session_id))

    @mcp.tool()
    @_debug_tool_response
    def search_list_allocation_decisions(
        run_id: str,
        status: Literal[
            "pending", "applied", "superseded", "terminal_error"
        ] | None = None,
    ) -> list[dict[str, Any]]:
        """仅当 FrozenSpec 使用 adaptive_search 时读取持久化资源分配决策。

        该工具只读，不启动、继续或中断任何 host worker。未传 status 时返回全部决策。
        """
        return tools.search_list_allocation_decisions(run_id, status)

    @mcp.tool()
    @_debug_tool_response
    def search_apply_allocation_decision(
        run_id: str,
        decision_id: str,
    ) -> dict[str, Any]:
        """仅当 FrozenSpec 使用 adaptive_search 时幂等应用准确决策。

        runtime 先预检 decision 中全部 expansion，再保留被退休候选的 Evidence、物化
        所有派生 workspace、创建 agent-session provenance，并返回每个 expansion 的 host
        launch payload。调用方仍须通过 Codex/Pi host 启动全部 worker 并绑定原生 handle；
        该工具不监督 worker 生命周期。
        """
        return tools.search_apply_allocation_decision(run_id, decision_id)

    @mcp.tool()
    @_debug_tool_response
    def search_select(run_id: str) -> dict[str, Any]:
        """按分数选择最佳已评估候选。在验证候选后调用。"""
        return tools.search_select(run_id)

    @mcp.tool()
    @_debug_tool_response
    def search_report(run_id: str) -> dict[str, str]:
        """生成最终 Markdown/HTML 报告；已链接的 Goal Plus 必须处于终态。"""
        return tools.search_report(run_id)

    @mcp.tool()
    @_debug_tool_response
    def search_promote(run_id: str, candidate_id: str) -> dict[str, Any]:
        """准备 provider-native publication，不改变主源码工作区。"""
        return tools.search_promote(run_id, candidate_id)

    @mcp.tool()
    @_debug_tool_response
    def search_apply_promotion(run_id: str) -> dict[str, Any]:
        """按 frozen promotion mode 将 publication 写回 Git repo 或 ThinkThread Root。"""
        return tools.search_apply_promotion(run_id)

    @mcp.tool()
    @_debug_tool_response
    def goal_plus_create(
        raw_goal: str,
        source_path: str | None = None,
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """已禁用的兼容入口；始终报错且绝不创建记录。

        只能用 Codex `$goal-plus` 或 Pi `/goal-plus` 由宿主入口创建 Goal Plus。
        """
        return goal_tools.goal_plus_create(raw_goal, source_path, policy)

    @mcp.tool()
    @_debug_tool_response
    def goal_plus_status(
        goal_plus_id: str, detail: bool = False,
    ) -> dict[str, Any]:
        """读取当前 revision、control、phase、待办和 next_action。

        默认不返回完整目标、草案、历史或审计日志。初始化、恢复会话或 revision 变化时
        使用 detail=true 读取完整 raw_goal、配置、policy、triage 和 spec_draft。
        历史详情用 goal_plus_history；恢复详情用 search_recovery_status。
        """
        return goal_tools.goal_plus_status(goal_plus_id, detail=detail)

    @mcp.tool()
    @_debug_tool_response
    def goal_plus_history(
        goal_plus_id: str,
        collection: Literal["goal_revisions", "work_items", "search_tasks", "final_checks", "events"],
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=GOAL_HISTORY_MAX_LIMIT)] = GOAL_HISTORY_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """按持久顺序分页读取 Goal 历史详情；next_offset 为 null 表示当前已读完。

        collection 指定 revision、dispatch（含完整证据）、Search 结果、final check
        或审计事件。每页默认 20 条、最多 100 条；不用于每轮状态轮询。
        """
        return goal_tools.goal_plus_history(goal_plus_id, collection, offset, limit)

    @mcp.tool()
    @_debug_tool_response
    def goal_plus_update_goal(
        goal_plus_id: str,
        raw_goal: str,
        expected_revision: int,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """已禁用的兼容入口；始终报错且绝不修改目标。

        只能用 Codex `$goal-plus edit` 或 Pi `/goal-plus edit` 由宿主入口修订。
        """
        return goal_tools.goal_plus_update_goal(
            goal_plus_id,
            raw_goal,
            expected_revision,
            reason,
        )

    @mcp.tool()
    @_debug_tool_response
    def goal_plus_record_triage(
        goal_plus_id: str,
        triage: GoalPlusTriage,
    ) -> dict[str, Any]:
        """记录目标应保留 goal 形态，还是向 Search 升级。"""
        return goal_tools.goal_plus_record_triage(goal_plus_id, triage)

    @mcp.tool()
    @_debug_tool_response
    def goal_plus_record_work_event(
        goal_plus_id: str,
        work_item_id: str,
        event: GoalPlusWorkEventKind,
        summary: Annotated[str, Field(min_length=1, max_length=4000)],
        host: str | None = None,
        task_name: str | None = None,
        agent_id: str | None = None,
        transcript_path: str | None = None,
        evidence: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        attempt_id: str | None = None,
        generation: int | None = None,
        launch_ttl_seconds: Annotated[int, Field(ge=1, le=3600)] = 120,
    ) -> dict[str, Any]:
        """记录主 Agent 与普通 subagent 的 fenced 派发生命周期。

        首次 dispatch 会创建轻量记录和 attempt/generation，原生启动后再 bind handle。
        仅保存任务摘要、host handle 和证据引用，不保存私有推理或完整对话正文。
        """
        return goal_tools.goal_plus_record_work_event(
            goal_plus_id,
            work_item_id,
            event,
            summary,
            host=host,
            task_name=task_name,
            agent_id=agent_id,
            transcript_path=transcript_path,
            evidence=evidence,
            metadata=metadata,
            attempt_id=attempt_id,
            generation=generation,
            launch_ttl_seconds=launch_ttl_seconds,
        )

    @mcp.tool()
    @_debug_tool_response
    def goal_plus_save_spec_draft(
        goal_plus_id: str,
        spec_draft: GoalPlusSpecDraftInput,
    ) -> dict[str, Any]:
        """在 search_freeze_spec 前保存发现的冻结 spec 候选。"""
        return goal_tools.goal_plus_save_spec_draft(goal_plus_id, spec_draft)

    @mcp.tool()
    @_debug_tool_response
    def goal_plus_link_search_run(
        goal_plus_id: str,
        frozen_spec_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        """把现有 Search MCP run 链接到 goal-plus 记录。"""
        return goal_tools.goal_plus_link_search_run(goal_plus_id, frozen_spec_id, run_id)

    @mcp.tool()
    @_debug_tool_response
    def goal_plus_record_search_result(
        goal_plus_id: str,
        run_id: str,
        selected_candidate_id: str | None = None,
        report_path: str | None = None,
        promotion_artifact_path: str | None = None,
        summary: str | None = None,
    ) -> dict[str, Any]:
        """最终原始目标审计前，记录已选择/提升的 Search 证据。"""
        return goal_tools.goal_plus_record_search_result(
            goal_plus_id,
            run_id,
            selected_candidate_id,
            report_path,
            promotion_artifact_path,
            summary,
        )

    @mcp.tool()
    @_debug_tool_response
    def goal_plus_prepare_final_check(
        goal_plus_id: str,
        checker_host: Literal["codex", "pi"],
    ) -> dict[str, Any]:
        """创建或恢复必需的最终检查请求，并返回 host launch payload。"""
        return goal_tools.goal_plus_prepare_final_check(goal_plus_id, checker_host)

    @mcp.tool()
    @_debug_tool_response
    def goal_plus_submit_final_check(
        goal_plus_id: str,
        check_id: str,
        goal_revision: int,
        verdict: Literal["pass", "fail", "interrupted"],
        summary: str,
        findings: list[dict[str, Any]] | None = None,
        evidence: list[dict[str, Any]] | None = None,
        checker_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """为准确目标修订版记录独立最终检查结论。"""
        return goal_tools.goal_plus_submit_final_check(
            goal_plus_id,
            check_id,
            goal_revision,
            verdict,
            summary,
            findings,
            evidence,
            checker_metadata,
        )

    @mcp.tool()
    @_debug_tool_response
    def goal_plus_set_status(
        goal_plus_id: str,
        status: GoalPlusStatus,
        reason: str | None = None,
        evidence: list[dict[str, Any]] | None = None,
        next_action: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """根据证据设置终态；Search 未记录提升结果时不能 complete。"""
        return goal_tools.goal_plus_set_status(
            goal_plus_id,
            status,
            reason,
            evidence,
            next_action,
        )

    @mcp.tool()
    @_debug_tool_response
    def goal_plus_gate(
        goal_plus_id: str,
        event: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """为 goal-plus 流程控制返回适合 hook 使用的 allow/block 决策。"""
        return goal_tools.goal_plus_gate(goal_plus_id, event, context)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--root",
        default=DEFAULT_RUNTIME_ROOT,
        help="Search runtime storage directory",
    )
    parser.add_argument(
        "--goal-plus-stop-hook",
        action="store_true",
        help="Run the Goal Plus Stop hook instead of starting the MCP server",
    )
    parser.add_argument(
        "--goal-plus-host-hook",
        action="store_true",
        help="Run the Goal Plus host hook instead of starting the MCP server",
    )
    args = parser.parse_args()
    if args.goal_plus_stop_hook or args.goal_plus_host_hook:
        from goal_plus.goal_plus_stop_hook import main as hook_main

        raise SystemExit(hook_main())
    create_mcp(args.root).run(transport="stdio")


if __name__ == "__main__":
    main()
