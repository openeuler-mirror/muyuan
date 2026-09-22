from __future__ import annotations

import argparse
import asyncio
import json
import time
from contextlib import asynccontextmanager
from collections.abc import Callable, Mapping
from functools import wraps
from inspect import iscoroutinefunction, signature
from pathlib import Path
from typing import Annotated, Any, Literal, TYPE_CHECKING

from mcp.types import TextContent
from pydantic import BaseModel, Field, TypeAdapter

from goal_plus._version import __version__
from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.domain.agents import AgentHarnessKind, AgentRuntimeProvider
from goal_plus.domain.adaptive.models import AllocationDecisionStatus
from goal_plus.domain.search import CandidateProposal
from goal_plus.domain.search_input import SearchSpecInput
from goal_plus.tool_schema import compact_tool_schema
from goal_plus.domain.goal import (
    GoalPlusFinalCheckerIdentity,
    GoalPlusStatus,
    GoalPlusTriage,
    GoalPlusWorkEventKind,
)
from goal_plus.domain.shared_tools import ToolizationDecision
from goal_plus.paths import DEFAULT_RUNTIME_ROOT, host_assets_root
from goal_plus.runtime import FileSearchRuntime
from goal_plus.tools import GoalPlusTools, SearchTools, ToolCallerRole, require_tool_role
from goal_plus.infrastructure.api_debug import api_debug_enabled, log_api_response_size
from goal_plus.projections.goal_tools import GOAL_HISTORY_DEFAULT_LIMIT, GOAL_HISTORY_MAX_LIMIT
from goal_plus.projections.session_tools import session_call_receipt
from goal_plus.ports.session import FinalCheckRunner, SessionControlLookup, SessionLauncher, WorkItemRunner
from goal_plus.ports.thinkthread_verification import DeferredCandidateAttempt
from goal_plus.domain.tool_catalog import PUBLIC_TOOL_NAMES, CANDIDATE_CAPABILITIES

if TYPE_CHECKING:
    from fastmcp import FastMCP

VERIFIER_FAST_WAIT_SECONDS = 1.0

# Turn-boundary responsiveness: Pi queues user input until the agent turn
# ends; a single long wait blocks the only interruption path. 300s keeps
# polling cheap while bounding how long a user can be locked out.
SESSION_WAIT_MAX_SECONDS = 300.0

# Server-side wait-any poll cadence: the composition loop reads every listed
# controller with a zero-timeout wait and sleeps between scans. The tool runs
# in a worker thread, so the MCP loop keeps serving other control requests.
SESSION_WAIT_POLL_SECONDS = 0.1


class SessionCallRef(BaseModel):
    """One exact session/call pair observed by goal_plus_session_wait_any."""

    session_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)


def _wait_any_receipts(
    session_controls: SessionControlLookup,
    calls: list[SessionCallRef],
    timeout_seconds: float,
    *, detail: bool,
) -> dict[str, Any]:
    resolved = [
        (ref.session_id, ref.call_id, session_controls(ref.session_id, "wait"))
        for ref in calls
    ]
    deadline = time.monotonic() + timeout_seconds
    while True:
        finished: list[dict[str, Any]] = []
        running: list[dict[str, Any]] = []
        for session_id, call_id, control in resolved:
            snapshot = control.wait(call_id, timeout=0)
            entry = {"session_id": session_id, **session_call_receipt(snapshot, detail=detail)}
            (finished if snapshot.state != "running" else running).append(entry)
        if finished or timeout_seconds <= 0 or time.monotonic() >= deadline:
            return {"finished": finished, "running": running}
        time.sleep(min(SESSION_WAIT_POLL_SECONDS, max(0.0, deadline - time.monotonic())))


def _json_text(value: Any) -> TextContent:
    return TextContent(
        type="text",
        text=json.dumps(value, ensure_ascii=False, separators=(",", ":")),
    )


def _debug_tool_response(function):
    def finish(result):
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

    @wraps(function)
    def measured(*args, **kwargs):
        return finish(function(*args, **kwargs))

    @wraps(function)
    async def measured_async(*args, **kwargs):
        return finish(await function(*args, **kwargs))

    if iscoroutinefunction(function):
        return measured_async
    return measured


def native_mcp_caller() -> str:
    from fastmcp.server.dependencies import get_context

    request = get_context().request_context
    meta = request.meta if request is not None else None
    identity = meta.get("threadId") if isinstance(meta, Mapping) else getattr(meta, "threadId", None)
    if not isinstance(identity, str) or not identity.strip():
        raise PermissionError("MainSessionMismatch: MCP request has no native thread identity")
    return identity


def create_mcp(
    root_dir: str | Path = DEFAULT_RUNTIME_ROOT,
    *,
    caller_role: ToolCallerRole | None = None,
    caller_agent_session_id: str | None = None,
    caller_final_check: "GoalPlusFinalCheckerIdentity | None" = None,
    session_controls: SessionControlLookup | None = None,
    session_launcher: SessionLauncher | None = None,
    final_checker: FinalCheckRunner | None = None,
    work_items: WorkItemRunner | None = None,
    candidate_attempt: DeferredCandidateAttempt | None = None,
    candidate_tool_call: Callable[[str, dict[str, Any]], Any] | None = None,
) -> FastMCP:
    from fastmcp import FastMCP
    from fastmcp.tools import Tool
    from fastmcp.utilities.json_schema import dereference_refs

    if (caller_role == "final_checker") != (caller_final_check is not None):
        raise ValueError("final checker MCP requires its host-bound check identity")
    if caller_role == "candidate" and not caller_agent_session_id:
        raise ValueError("candidate MCP requires a host-bound agent session identity")
    if caller_agent_session_id is not None and caller_role != "candidate":
        raise ValueError("agent session binding requires the candidate MCP role")
    if candidate_attempt is not None and (
        caller_role != "candidate" or caller_agent_session_id != candidate_attempt.agent_session_id
    ):
        raise ValueError("deferred verification requires its host-bound candidate MCP identity")
    if session_controls is not None and caller_role != "main" and not (
        caller_role is None and session_launcher is session_controls
    ):
        raise ValueError("session control requires a trusted Main MCP binding")
    if session_launcher is not None and session_controls is None:
        raise ValueError("session launch requires session control")
    if candidate_tool_call is not None and (caller_role != "candidate" or candidate_attempt is not None):
        raise ValueError("remote candidate tools require a bound candidate without local settlement")
    runtime = FileSearchRuntime(root_dir) if candidate_tool_call is None else None
    goal_runtime = FileGoalPlusRuntime(root_dir) if candidate_tool_call is None else None
    tools = SearchTools(runtime, goal_runtime, candidate_attempt=candidate_attempt) if runtime is not None else None
    goal_tools = GoalPlusTools(goal_runtime) if goal_runtime is not None else None
    capabilities = None
    if caller_agent_session_id and runtime is not None:
        try:
            session = runtime.candidate_sessions.session(caller_agent_session_id)
        except FileNotFoundError:
            # Schema-only transports use an unbound template identity. Calls
            # still require the exact durable session in their domain owner.
            session = None
        if session is not None:
            frozen = runtime.queries.state.read_frozen_spec(
                runtime.queries.state.read_run(session.run_id).frozen_spec_id
            )
            capabilities = {"supplemental": frozen.supplemental_evaluation_enabled,
                            "shared_tools": frozen.spec.shared_dir.enabled}
    @asynccontextmanager
    async def session_lifespan(server):
        try:
            yield {}
        finally:
            import anyio

            # Transport disconnect cancels its task group before lifespan exit.
            # Resource shutdown must finish even in that cancelled scope.
            with anyio.CancelScope(shield=True):
                errors = []
                for resource in (session_launcher, final_checker, work_items):
                    if resource is not None:
                        try:
                            await anyio.to_thread.run_sync(resource.shutdown)
                        except Exception as exc:
                            errors.append(exc)
                if errors:
                    raise RuntimeError("native MCP resource cleanup failed") from errors[0]

    mcp = FastMCP(
        "goal-plus", version=__version__,
        dereference_schemas=False,
        instructions=(host_assets_root() / "assets/goal-plus-instructions.txt").read_text(encoding="utf-8").strip()
        if caller_role in {None, "main"} else None,
        lifespan=session_lifespan if any(item is not None for item in (session_launcher, final_checker, work_items)) else None,
    )

    verifier_calls: set[asyncio.Task] = set()

    def verifier_finished(call: asyncio.Task) -> None:
        verifier_calls.discard(call)
        if not call.cancelled():
            call.exception()

    def register_tool():
        def expose(function):
            tool = Tool.from_function(function)
            tool.parameters = compact_tool_schema(
                tool.parameters, prune_additional_properties=True,
            )
            if tool.output_schema is not None:
                tool.output_schema = dereference_refs(tool.output_schema)
            mcp.add_tool(tool)
            return function

        def register(function):
            if function.__name__ not in PUBLIC_TOOL_NAMES:
                return function
            capability = CANDIDATE_CAPABILITIES.get(function.__name__)
            if capability and capabilities is not None and not capabilities[capability]:
                return function
            if caller_role is None:
                return expose(function)
            try:
                require_tool_role(
                    caller_role, function.__name__,
                    caller_agent_session_id=caller_agent_session_id,
                    requested_agent_session_ids=(
                        [caller_agent_session_id] if caller_agent_session_id else []
                    ),
                )
            except PermissionError:
                return function
            tool_signature = signature(function)

            def check_call(*args, **kwargs):
                arguments = tool_signature.bind(*args, **kwargs).arguments
                requested_session = arguments.get("agent_session_id")
                if caller_final_check is not None:
                    if arguments.get("goal_plus_id") != caller_final_check.goal_plus_id or (
                        function.__name__ == "goal_plus_submit_final_check" and (
                            arguments.get("check_id") != caller_final_check.check_id
                            or arguments.get("goal_revision") != caller_final_check.goal_revision
                        )
                    ):
                        raise PermissionError("FinalCheckerIdentityMismatch: tool target does not match the bound check")
                    goal_runtime.goal_completion.require_native_checker(caller_final_check, native_mcp_caller())
                require_tool_role(
                    caller_role, function.__name__,
                    caller_agent_session_id=caller_agent_session_id,
                    requested_agent_session_ids=(
                        [requested_session] if isinstance(requested_session, str) else []
                    ),
                )
            if candidate_tool_call is not None:
                @wraps(function)
                def bound_call(*args, **kwargs):
                    check_call(*args, **kwargs)
                    arguments = tool_signature.bind(*args, **kwargs)
                    arguments.apply_defaults()
                    result = candidate_tool_call(
                        function.__name__, TypeAdapter(Any).dump_python(dict(arguments.arguments), mode="json"),
                    )
                    return (_json_text(result) if tool_signature.return_annotation in (TextContent, "TextContent")
                            else result)
            elif iscoroutinefunction(function):
                @wraps(function)
                async def bound_call(*args, **kwargs):
                    check_call(*args, **kwargs)
                    return await function(*args, **kwargs)
            else:
                @wraps(function)
                def bound_call(*args, **kwargs):
                    check_call(*args, **kwargs)
                    return function(*args, **kwargs)

            return expose(bound_call)
        return register

    @register_tool()
    async def goal_plus_get_tool_schema(tool_name: str) -> dict[str, Any]:
        """读取当前注册工具的完整 inputSchema 与说明，不创建或修改运行状态。

        宿主声明显示 unknown 或省略嵌套字段时，用此入口读取完整参数合同，无需查源码。
        tool_name 使用不带 MCP namespace 的名称，例如 goal_plus_search_freeze_spec。
        仅返回当前 MCP 注册中可见的工具；返回值与 tools/list 同源，不裁剪嵌套字段。
        """
        tool = next((tool for tool in await mcp.list_tools() if tool.name == tool_name), None)
        if tool is None:
            raise ValueError(f"ToolSchemaUnavailable: tool is not registered: {tool_name}")
        return {"name": tool.name, "description": tool.description, "inputSchema": tool.parameters}

    if session_controls is not None:
        if session_launcher is not None:
            @register_tool()
            async def goal_plus_session_run(
                run_id: str, candidate_id: str, call_id: Annotated[str, Field(min_length=1)],
                prompt: Annotated[str, Field(min_length=1)],
                timeout_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)],
                new_session: bool = False,
            ) -> dict[str, Any]:
                """Prepare, bind and run one candidate invocation; return its session/call state immediately.

                Reuse call_id for identical retries. Later calls reuse the current session;
                new_session closes idle resources and dispatches a new generation. Active calls
                accept steering through message. After an MCP restart, use a new call_id to continue
                the same native session and workspace; checkpoint recovery is a separate explicit action.
                The frozen budget and Goal deadline bound the execution allowance.
                """
                return await asyncio.to_thread(
                    session_launcher.run_candidate, run_id, candidate_id, call_id, prompt,
                    timeout_seconds, new_session,
                )

        @register_tool()
        async def goal_plus_session_wait_any(
            calls: Annotated[list[SessionCallRef], Field(
                min_length=1,
                description="Exact session_id/call_id pairs to observe. Parallel lanes list every active "
                "invocation once; duplicate pairs are rejected.",
            )],
            timeout_seconds: Annotated[float, Field(
                ge=0, allow_inf_nan=False,
                description="Maximum time this read waits for the first finished invocation; zero polls "
                "immediately. Does not change the execution allowance supplied to run or stop a running "
                "invocation.",
            )] = 0,
            detail: Annotated[bool, Field(
                description="Expand each finished entry's receipt with the invocation's full output. "
                "Applies to this tool only; leave false during lane polling.",
            )] = False,
        ) -> dict[str, Any]:
            """Observe every listed call and return at the first finished one; a wait timeout returns the roster without stopping execution.

            finished entries carry the exact call state, errors and result availability; running entries keep
            identity and state only. Completed means the invocation returned, not that verification passed.
            For Search progress read goal_plus_search_list_history's default view — it already carries
            rankings, scores and key metrics; its detail=true is only for host diagnostics and invalidation
            evidence. This tool's own detail=true merely expands finished receipts with the invocation's
            full output. Handle a finished lane with a new session_run call_id while the other lanes keep
            running; one wait covers the whole lane set, so per-lane waits are not needed.
            Scale the wait to the lanes: short waits (tens of seconds) while lanes are starting
            or settling; once every listed lane is running in stable exploration, longer waits
            up to SESSION_WAIT_MAX_SECONDS are appropriate — this wait returns early at the
            first finish, so the timeout only bounds idle time. When no call is finished, end
            the turn and let the host Stop continuation re-ignite you instead of blocking a
            single turn. User input cannot interrupt an in-flight wait: it queues until this
            turn ends and is then handled first; goal_plus_session_message can steer an active
            invocation.
            A restarted MCP cannot replay old call history; continue with session_run and a new call_id.
            """
            if timeout_seconds > SESSION_WAIT_MAX_SECONDS:
                raise ValueError(
                    f"goal_plus_session_wait_any timeout_seconds must be <= "
                    f"{int(SESSION_WAIT_MAX_SECONDS)}: a single long wait blocks the agent "
                    "turn and prevents user input from being delivered at turn "
                    "boundaries. Use short waits; when no call is finished, end "
                    "the turn and let the host Stop continuation re-ignite you."
                )
            pairs = [(ref.session_id, ref.call_id) for ref in calls]
            if len(set(pairs)) != len(pairs):
                raise ValueError(
                    "goal_plus_session_wait_any requires unique session_id/call_id pairs"
                )
            return await asyncio.to_thread(
                _wait_any_receipts, session_controls, calls, timeout_seconds,
                detail=detail,
            )

        @register_tool()
        async def goal_plus_session_message(
            session_id: str, call_id: str, prompt: str,
            timeout_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)],
        ) -> dict[str, Any]:
            """Steer the identified active invocation. Use session_run with a new call_id for an idle session."""
            await asyncio.to_thread(
                lambda: session_controls(session_id, "message").message(call_id, prompt, timeout=timeout_seconds),
            )
            return {"session_id": session_id, "call_id": call_id, "accepted": True}

        @register_tool()
        async def goal_plus_session_interrupt(
            session_id: str, call_id: str,
            timeout_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)],
        ) -> dict[str, Any]:
            """Interrupt the identified turn. Wait for its settled outcome before reusing the session."""
            await asyncio.to_thread(
                lambda: session_controls(session_id, "interrupt").interrupt(call_id, timeout=timeout_seconds),
            )
            return {"session_id": session_id, "call_id": call_id, "accepted": True}

        @register_tool()
        async def goal_plus_session_stop(session_id: str) -> dict[str, Any]:
            """Stop this native execution, preserving native history and recorded invocation outcomes."""
            await asyncio.to_thread(lambda: session_controls(session_id, "stop").stop())
            return {"session_id": session_id, "execution_stopped": True}

        @register_tool()
        async def goal_plus_session_close(session_id: str) -> dict[str, Any]:
            """Close the host's session controller and its execution resources. Does not delete artifacts or history."""
            await asyncio.to_thread(lambda: session_controls(session_id, "close").close())
            return {"session_id": session_id, "closed": True}

    @register_tool()
    @_debug_tool_response
    def goal_plus_search_freeze_spec(
        goal_plus_id: str,
        spec: SearchSpecInput,
        verifier_artifact_paths: list[str],
    ) -> dict[str, Any]:
        """为当前 active Goal 冻结 SearchSpec 与 verifier 文件，不代表候选已达标。

        Git source 必须是 clean HEAD。verifier_artifact_paths 相对 source_path，
        文件须位于源码内，不得放在 .gp 或 .search。Git 在 source 原位、Copy 在一次性副本执行
        process suite；ThinkThread 在 create 时验证 baseline。临时产物使用 GOAL_PLUS_VERIFIER_TMPDIR。
        stdout JSON outcome=valid/invalid/error 区分有效候选、确定无效候选和评估故障。
        invalid 可用退出码 0/1，保留原 error；可作基线但不能进入 best、selection 或 promotion。
        valid 或省略 outcome 须退出 0 且 error 缺失/null；valid=false 须显式分类为 invalid/error。
        从 stdout 最后一个可解析的单行 JSON 对象精确读取 spec.metric_name，不猜字段别名。
        ranking_signal 是唯一评分来源；省略时只允许一个非 diagnostic/anti_cheat verifier 产出分数。
        基线须有有限分数（可为零）；只将已确认的候选问题归为 invalid，未知故障归为 error，
        不得删掉原诊断或把所有异常转零分。promotion_verifiers 仅提升时执行，freeze 不证明其可用。
        budget.max_parallel 决定初始候选数；device_pools 只引用宿主已注册的资源池。
        candidate_acceptance 只给 worker 文字要求，不自动执行验收；constraints 不下发。
        成功返回 frozen_spec_id、baseline 摘要及提升检查名称；baseline=null 表示尚未验证。
        将 frozen_spec_id 交给 goal_plus_search_create；失败不生成 FrozenSpec。
        参数声明不完整时，用 goal_plus_get_tool_schema 读取完整合同。
        """
        return tools.goal_plus_search_freeze_spec(goal_plus_id, spec, verifier_artifact_paths)

    @register_tool()
    @_debug_tool_response
    def goal_plus_list_models(
        agent_harness: AgentHarnessKind,
        runtime_provider: AgentRuntimeProvider,
        query: str | None = None,
    ) -> dict[str, Any]:
        """列出指定 Codex/Pi host 当前可用的模型。

        指定 worker 模型时，在冻结 spec 前调用它。返回的 `model` 是可写入
        `strategy.models[].model` 的精确 host 引用；不存在或有歧义的请求会在冻结时
        直接失败。省略 `query` 返回完整清单。
        """
        return tools.goal_plus_list_models(agent_harness, runtime_provider, query)

    @register_tool()
    @_debug_tool_response
    def goal_plus_search_create(
        goal_plus_id: str,
        frozen_spec_id: str,
        request_id: Annotated[str, Field(min_length=1)],
        source_run_id: str | None = None,
    ) -> dict[str, str]:
        """从冻结 spec 创建并关联 Search run，返回 `run_id`；随后规划候选。

        request_id 标识可恢复的创建请求；同参数重试返回原 run，并自动关联当前 Goal revision。
        成功回执 state=linked；尚未关联的 run 不能用于候选操作。
        `goal_plus_id` 必须与 freeze 使用的当前 active Goal 相同。启动前复检冻结的
        agent_harness/runtime_provider 组合、workspace provider 和 source Git 状态；
        不支持的组合，或 Git source 在 freeze 后变 dirty，会在创建 run 或 candidate 前失败。
        创建时会在 `GOAL_PLUS_VERIFIER_RESOURCE_LOCK_DIR` 下独占冻结 spec 引用的 device pool。
        多个进程或容器共享设备时必须映射同一个 host 锁目录。只有无法避免新的不可变 run
        时才传入 `source_run_id`。新 run 会收到来源 frontier、
        限定范围的问题和特性账本的有界 snapshot。来源分数只是历史，必须在新契约下重新验证。
        """
        return tools.goal_plus_search_create(goal_plus_id, frozen_spec_id, request_id, source_run_id)

    @register_tool()
    @_debug_tool_response
    def goal_plus_search_status(run_id: str) -> dict[str, Any]:
        """返回 run 状态、预算使用和最佳分数的只读 snapshot。"""
        return tools.goal_plus_search_status(run_id)

    @register_tool()
    @_debug_tool_response
    def goal_plus_search_recovery_status(run_id: str, detail: bool = False) -> dict[str, Any]:
        """读取各 candidate 的结算恢复点、generation 和 session 概要，用于恢复分诊；不启动或回滚任何工作。

        默认只返回恢复决策所需字段（checkpoint、未结算变更、pending verification、
        recovery 状态与 session 的绑定概要）；detail=true 才附带完整 session_handle
        （含 progress handoff、host metadata）与完整 recovery 记录。
        """
        return tools.goal_plus_search_recovery_status(run_id, detail=detail)

    @register_tool()
    @_debug_tool_response
    def goal_plus_search_prepare_recovery(run_id: str, candidate_id: str) -> dict[str, Any]:
        """显式放弃 candidate 当前现场，固定结算恢复点并隔离旧 generation，返回 recovery_id。

        随后由 host 停止并确认旧 worker 结束，再调用 goal_plus_search_recover_candidate。
        """
        return tools.goal_plus_search_prepare_recovery(run_id, candidate_id)

    @register_tool()
    @_debug_tool_response
    def goal_plus_search_recover_candidate(run_id: str, candidate_id: str, recovery_id: str) -> dict[str, Any]:
        """恢复已结算 artifact，保留 Evidence 与中断现场备份；用同一 recovery_id 重试。

        恢复后由 host 启动并绑定 fresh worker，继续验证。
        """
        result = tools.goal_plus_search_recover_candidate(run_id, candidate_id, recovery_id)
        if session_launcher is not None:
            session_launcher.wake_internal_agents(run_id)
        return result

    @register_tool()
    @_debug_tool_response
    def goal_plus_search_invalidate_run(
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
        worker：调用后，主 agent 必须通过共同 session stop/close 停止所属候选并确认资源退出，
        再修复/冻结 verifier，并使用 `source_run_id` 创建新 run。
        """
        return tools.goal_plus_search_invalidate_run(run_id, reason, summary, evidence)

    @register_tool()
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

    @register_tool()
    @_debug_tool_response
    def goal_plus_search_list_history(
        run_id: str,
        top_n: int = 5,
        sort_by: str = "score",
        detail: bool = False,
    ) -> dict[str, Any]:
        """返回候选排名、验证结果、异常和当前 run 的研究摘要。

        默认视图含 objective、key_metrics、latest 异常与研究汇总，足以分析当前进展；
        detail=true 仅在需要宿主诊断或失效调查时使用，读取完整 session、日志路径、
        配置、研究与失效证据。完整 iteration 历史仍用 goal_plus_search_list_iterations。
        best 与 latest 验证结果不一致时单独返回 latest；未验证分数保持 null。
        top_n 限制排名中的候选数量，研究汇总仍覆盖当前 run 的所有候选。
        """
        return tools.goal_plus_search_list_history(run_id, top_n, sort_by, detail=detail)

    @register_tool()
    @_debug_tool_response
    def goal_plus_search_plan_next(
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
        """为已关联 Goal 的 run 取得唯一初始候选 plan，返回 plan_id 和 planned_k。

        `requested_k` 只适用于这一次初始规划。实际 `planned_k` 是 `requested_k` 与
        `budget.max_parallel` 剩余可用数量的最小值。标准流程必须令两者相等。
        下一步用 goal_plus_search_start_batch 一次性提交该 plan 的完整 proposals 列表。
        重复调用返回已有 plan；后续探索用 goal_plus_session_run 和新 call_id 继续同一 session。
        """
        return tools.goal_plus_search_plan_next(run_id, requested_k)

    @register_tool()
    @_debug_tool_response
    def goal_plus_search_start_batch(
        run_id: str,
        plan_id: str,
        proposals: Annotated[
            list[CandidateProposal] | None,
            Field(description=(
                "该 plan 要启动的完整候选列表，一次性提交。例如需要两个候选时在同一列表放两个 proposal。"
                "同 plan 的后续调用只返回已创建批次，不追加或替换候选。固定 work-order plan 省略此字段。"
            )),
        ] = None,
    ) -> list[dict[str, Any]]:
        """一次性物化 plan 的完整初始候选批次，返回 candidate ID、workspace provider 和实际选定模型。

        proposals 嵌套字段不完整时，先用 goal_plus_get_tool_schema 读取本工具的完整合同。
        在 goal_plus_search_plan_next 之后调用。需要 K=2 时将两个 proposal 放在同一次调用的
        proposals 列表中；分两次各传一个不会创建两个候选。成功后的重复调用是幂等读取，
        即使传入不同 proposals 也不会追加或替换。省略 proposals 只适用于已有固定 work orders 的 plan。
        完整工作合同由宿主交付 candidate，不回显 instructions 或 provider 内部句柄。
        物化后为每个 candidate 调用 goal_plus_session_run。
        """
        return tools.goal_plus_search_start_batch(run_id, plan_id, proposals)

    @register_tool()
    @_debug_tool_response
    def goal_plus_search_get_agent_observability(
        agent_session_id: str,
    ) -> dict[str, Any]:
        """读取一个 agent session 的规范化 host metric 和产物。

        schema 跨 host 共享。可用时 Codex 解析原生 session JSONL；Pi 规范化已绑定的
        `pi_metrics`。此调用只读，不会等待、继续或中断 worker。绝不返回 prompt、
        reasoning 或工具 payload 正文。
        """
        return tools.goal_plus_search_get_agent_observability(agent_session_id)

    @register_tool()
    @_debug_tool_response
    def goal_plus_search_get_agent_context(agent_session_id: str) -> TextContent:
        """Subagent 的首次调用，返回紧凑的权威工作合同。

        返回 run_id、candidate_id、workspace、candidate_task（含冻结验收合同与 candidate
        进程环境）、最佳/最近 iteration 摘要、canonical .gp ledger 来源和可选 results.tsv
        projection。由 subagent 调用，不由主
        agent 调用。subagent 必须把 prompt 提供的 id 只当作标签，并将此响应作为事实来源。
        """
        return _json_text(tools.goal_plus_search_get_agent_context(agent_session_id))

    @register_tool()
    @_debug_tool_response
    def goal_plus_search_get_global_evidence(
        agent_session_id: str,
    ) -> TextContent:
        """返回当前 run 的窄 Global Evidence 视图。

        每项只包含 candidate_id、iteration、score、keep/retain/discard/failure disposition、
        verifier attempt commit、可能延迟的客观 View，以及可选 supplemental evaluation
        的可用标记。任何 View 为 null 都不影响 verifier Evidence；worker 不需要等待，
        可先依据 Evidence 独立探索。
        """
        return _json_text(tools.goal_plus_search_get_global_evidence(agent_session_id))

    @register_tool()
    @_debug_tool_response
    def goal_plus_search_reference_open(
        agent_session_id: str,
        source_candidate_id: str,
        source_iteration: Annotated[int, Field(ge=1)],
    ) -> dict[str, Any]:
        """把一条已结算 Evidence checkout 到当前 worker 的 ref-workspace。

        每个 worker 只有一个稳定目录；再次调用会把同一目录切到新的精确 commit。
        返回的 root 可直接使用普通文件和命令工具查看，但不是 peer 的活动 workspace，
        实现修改仍应只发生在 candidate workspace。
        """
        return tools.goal_plus_search_reference_open(
            agent_session_id,
            source_candidate_id,
            source_iteration,
        )

    @register_tool()
    @_debug_tool_response
    def goal_plus_search_copy_shared_tool(
        agent_session_id: str,
        tool_id: str,
        snapshot_hash: str,
    ) -> dict[str, Any]:
        """按 Global Evidence 中的精确 id/hash 复制 shared-dir 快照到临时 inbox。

        receipt 由下一次 worker verifier 消费；具体采用契约见 candidate instructions。
        """
        return tools.goal_plus_search_copy_shared_tool(agent_session_id, tool_id, snapshot_hash)

    @register_tool()
    @_debug_tool_response
    def goal_plus_search_stage_shared_tool(
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
        return tools.goal_plus_search_stage_shared_tool(
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

    @register_tool()
    @_debug_tool_response
    def goal_plus_search_get_evidence_detail(
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
            tools.goal_plus_search_get_evidence_detail(
                agent_session_id,
                candidate_id,
                iteration,
            )
        )

    @register_tool()
    @_debug_tool_response
    async def goal_plus_search_run_verifier(
        run_id: str,
        candidate_id: str,
        scope: Literal["process", "promotion"] = "process",
        agent_session_id: str | None = None,
        hypothesis: str | None = None,
        toolization_decision: ToolizationDecision | None = None,
        attempt_id: Annotated[str, Field(min_length=1)] | None = None,
        retry: bool = False,
        timeout_seconds: Annotated[float, Field(ge=0, le=60, allow_inf_nan=False)] = 30,
    ) -> dict[str, Any]:
        """执行冻结 verifier 并记录 Evidence；process_passed 不代表最终验收达标。

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
        未提供 attempt_id 时提交修改；提供时只查询或等待该 process attempt，不重新提交，
        不再传 hypothesis 或 toolization_decision。timeout_seconds 限制查询等待，不取消验证。
        未完成时按 candidate_action 操作：wait_for_verifier 使用原 attempt_id 调用本工具；
        end_turn 结束当前 turn，由宿主在下一次 wake 交付结果。等待期间保持工作区不变。
        本地 attempt 中断且执行锁已释放时，使用原 attempt_id 和 retry=true 恢复验证与结算；
        复用完整结果或重验原提交快照，保留当前 workspace，不额外创建 iteration 或回滚 checkpoint。
        """
        loop = asyncio.get_running_loop()
        if attempt_id is not None and not retry:
            deadline = loop.time() + timeout_seconds
            while True:
                result = await asyncio.to_thread(
                    tools.goal_plus_search_run_verifier, run_id, candidate_id, scope,
                    agent_session_id, hypothesis, toolization_decision,
                    attempt_id=attempt_id, inject_evidence=False,
                )
                remaining = deadline - loop.time()
                if result.get("state") != "pending" or remaining <= 0:
                    return await asyncio.to_thread(tools.verifier_response, run_id, agent_session_id, result)
                await asyncio.sleep(min(1.0, remaining))
        submitted = loop.create_future()
        call = asyncio.create_task(asyncio.to_thread(
            tools.goal_plus_search_run_verifier, run_id, candidate_id, scope,
            agent_session_id, hypothesis, toolization_decision,
            inject_evidence=False,
            attempt_id=attempt_id, retry=retry,
            on_submitted=lambda pending: loop.call_soon_threadsafe(submitted.set_result, pending),
        ))
        verifier_calls.add(call)
        call.add_done_callback(verifier_finished)
        if scope != "process":
            return await asyncio.shield(call)
        # Never acknowledge an attempt before its immutable identity is durable.
        await asyncio.wait({call, submitted}, return_when=asyncio.FIRST_COMPLETED)
        await asyncio.wait({call}, timeout=VERIFIER_FAST_WAIT_SECONDS)
        if call.done():
            return await asyncio.to_thread(tools.verifier_response, run_id, agent_session_id, call.result())
        return {
            "run_id": run_id, "candidate_id": candidate_id,
            "state": "pending",
            "attempt_id": submitted.result().attempt_id,
            "verification": submitted.result().model_dump(mode="json"),
            "candidate_action": "wait_for_verifier",
            "message": "保持工作区不变，使用原 attempt_id 调用 goal_plus_search_run_verifier 等待本次结果。",
        }

    @register_tool()
    @_debug_tool_response
    def goal_plus_search_list_iterations(
        agent_session_id: str,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=GOAL_HISTORY_MAX_LIMIT)] = GOAL_HISTORY_DEFAULT_LIMIT,
    ) -> TextContent:
        """返回当前 agent session 所属候选可能很大的完整 iteration 历史。

        candidate worker 只应查询 agent context 中自己的 run/candidate，并且仅在紧凑
        context、可选 ledger projection 和 Git 无法回答旧轮次的准确 verifier 事实时调用。
        """
        return _json_text(tools.goal_plus_search_list_iterations(agent_session_id, offset, limit))

    @register_tool()
    @_debug_tool_response
    def goal_plus_search_list_allocation_decisions(
        run_id: str,
        status: AllocationDecisionStatus | None = None,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=GOAL_HISTORY_MAX_LIMIT)] = GOAL_HISTORY_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """仅当 FrozenSpec 使用 adaptive_search 时读取持久化资源分配决策。

        该工具只读；未传 status 时返回全部决策。Main 可显式恢复 interrupted Decision。
        """
        return tools.goal_plus_search_list_allocation_decisions(run_id, status, offset, limit)

    @register_tool()
    @_debug_tool_response
    def goal_plus_search_apply_allocation_decision(
        run_id: str,
        decision_id: str,
    ) -> dict[str, Any]:
        """仅当 FrozenSpec 使用 adaptive_search 时幂等应用准确决策。

        Executor 在 run lock 内完成准确 host 取消、派生物化和 native session dispatch，
        并持久化完整 Decision/candidate/session identity。
        """
        if session_launcher is None:
            raise PermissionError("allocation apply requires a trusted session host")
        return tools.goal_plus_search_apply_allocation_decision(
            run_id, decision_id, apply=session_launcher.apply_allocation_decision
        )

    @register_tool()
    @_debug_tool_response
    def goal_plus_search_select(run_id: str) -> dict[str, Any]:
        """按冻结 ranking keys/tolerance 选择 exact artifact；必需质量评分 pending 时等待队列完成。"""
        try:
            return tools.goal_plus_search_select(run_id)
        finally:
            if session_launcher is not None:
                session_launcher.wake_internal_agents(run_id)

    @register_tool()
    @_debug_tool_response
    def goal_plus_search_report(run_id: str) -> dict[str, str]:
        """生成最终 Markdown/HTML 报告；已链接的 Goal Plus 必须处于终态。"""
        return tools.goal_plus_search_report(run_id)

    @register_tool()
    @_debug_tool_response
    def goal_plus_search_promote(run_id: str, candidate_id: str) -> dict[str, Any]:
        """为已 select 的 artifact 准备 publication，不改变主源码工作区。

        自动重新执行已配置的 promotion_verifiers，无需先手动调用 promotion 验证。
        未配置时不会补出新的验收标准；写回源码另调 goal_plus_search_apply_promotion。
        """
        return tools.goal_plus_search_promote(run_id, candidate_id)

    @register_tool()
    @_debug_tool_response
    def goal_plus_search_apply_promotion(run_id: str) -> dict[str, Any]:
        """按 frozen promotion mode 将 publication 写回 Git repo 或 ThinkThread Root。"""
        return tools.goal_plus_search_apply_promotion(run_id)

    @register_tool()
    @_debug_tool_response
    def goal_plus_status(
        goal_plus_id: str, detail: bool = False,
    ) -> dict[str, Any]:
        """读取当前 revision、control、phase、待办和 next_action。

        默认不返回完整目标、草案、历史或审计日志。初始化、恢复会话或 revision 变化时
        使用 detail=true 读取完整 raw_goal、配置、policy、triage 和 spec_draft。
        历史详情用 goal_plus_history；恢复详情用 goal_plus_search_recovery_status。
        """
        return goal_tools.goal_plus_status(goal_plus_id, detail=detail)

    @register_tool()
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

    @register_tool()
    @_debug_tool_response
    def goal_plus_record_triage(
        goal_plus_id: str,
        triage: GoalPlusTriage,
    ) -> dict[str, Any]:
        """按需记录 Search 分流判断；普通 coding 无需调用。

        Goal 默认直接执行普通实现、文档、审查或调查。只有数值 metric、确定性 verifier、隔离
        edit surface 和有价值的探索方向齐备时才进入 search；优化目标尚缺评价合同则进入
        spec_discovery，并在 missing 中记录缺项。评价目标和适用范围由 Main 判断，显式
        command_config 优先。完整评价合同直接交给 goal_plus_search_freeze_spec。
        """
        return goal_tools.goal_plus_record_triage(goal_plus_id, triage)

    @register_tool()
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

    @register_tool()
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

    @register_tool()
    @_debug_tool_response
    async def goal_plus_run_work_item(
        goal_plus_id: str,
        work_item_id: str,
        task: Annotated[str, Field(min_length=1, max_length=4000)],
        max_runtime_seconds: Annotated[int, Field(ge=30, le=3600)] = 600,
    ) -> dict[str, Any]:
        """派发普通子任务并等待结果。同一 work_item_id 的 result_ready 可在同一 native session 返工；Main 审查后记录 accepted。"""
        if work_items is None:
            raise RuntimeError("native delegation requires a trusted Main host binding")
        return await asyncio.to_thread(work_items.run, goal_plus_id, work_item_id, task, max_runtime_seconds)

    @register_tool()
    @_debug_tool_response
    async def goal_plus_run_final_check(
        goal_plus_id: str,
    ) -> dict[str, Any]:
        """为当前 Goal revision 准备并运行独立只读检查，返回结论/findings 并关闭资源。失败后修复，再调用本工具。"""
        if final_checker is None:
            raise RuntimeError("native final checker requires a trusted Main host binding")
        return await asyncio.to_thread(final_checker.check, goal_plus_id)

    @register_tool()
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
            native_session_id=native_mcp_caller() if caller_final_check is not None else None,
        )

    @register_tool()
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

    return mcp


def create_host_mcp(
    root_dir: str | Path = DEFAULT_RUNTIME_ROOT, *, agent_harness: Literal["codex", "pi"],
    caller_role: ToolCallerRole | None = None, caller_agent_session_id: str | None = None,
    caller_final_check: "GoalPlusFinalCheckerIdentity | None" = None,
) -> FastMCP:
    if caller_role not in {None, "main"}:
        return create_mcp(root_dir, caller_role=caller_role, caller_agent_session_id=caller_agent_session_id,
                          caller_final_check=caller_final_check)
    from goal_plus.session_host import NativeCandidateHost
    from goal_plus.final_check_host import NativeFinalCheckHost
    from goal_plus.delegation_host import NativeDelegationHost

    sessions = NativeCandidateHost(root_dir, agent_harness=agent_harness, caller=native_mcp_caller)
    return create_mcp(
        root_dir, caller_role=caller_role, session_controls=sessions, session_launcher=sessions,
        final_checker=NativeFinalCheckHost(root_dir, agent_harness=agent_harness, caller=native_mcp_caller),
        work_items=NativeDelegationHost(root_dir, agent_harness=agent_harness, caller=native_mcp_caller),
    )


def create_codex_mcp(root_dir: str | Path = DEFAULT_RUNTIME_ROOT) -> FastMCP:
    import os
    from goal_plus.codex_hook_input import CODEX_CANDIDATE_SESSION_ENV, CODEX_ORDINARY_ROLE_ENV
    from goal_plus.infrastructure.final_check_binding import declared_final_checker

    candidate_id = os.environ.get(CODEX_CANDIDATE_SESSION_ENV)
    checker = declared_final_checker()
    ordinary = os.environ.get(CODEX_ORDINARY_ROLE_ENV)
    thinkthread_role = os.environ.get("GOAL_PLUS_THINKTHREAD_ROLE")
    if thinkthread_role is not None:
        if thinkthread_role not in {"main", "candidate", "annotator"} or any(item is not None for item in (candidate_id, checker, ordinary)):
            raise PermissionError("invalid or conflicting ThinkThread native role")
        if thinkthread_role == "main":
            from goal_plus.infrastructure.thinkthread_codex_tools import create_thinkthread_main_mcp

            return create_thinkthread_main_mcp(root_dir)
        if thinkthread_role == "annotator":
            from goal_plus.infrastructure.thinkthread_codex_tools import create_thinkthread_annotator_mcp

            return create_thinkthread_annotator_mcp()
        from goal_plus.infrastructure.thinkthread_codex_tools import create_thinkthread_candidate_mcp

        return create_thinkthread_candidate_mcp()
    if sum(item is not None for item in (candidate_id, checker, ordinary)) > 1:
        raise PermissionError("native process has conflicting role declarations")
    if ordinary is not None and ordinary != "ordinary":
        raise PermissionError("invalid ordinary native role declaration")
    if candidate_id is not None or checker is not None or ordinary is not None:
        root_dir = os.environ.get("GOAL_PLUS_SEARCH_ROOT")
        if not root_dir or not Path(root_dir).is_absolute():
            raise PermissionError("candidate MCP requires its host-declared absolute runtime root")
    return create_host_mcp(
        root_dir, agent_harness="codex", caller_role="candidate" if candidate_id is not None else "final_checker" if checker else "ordinary" if ordinary else "main",
        caller_agent_session_id=candidate_id, caller_final_check=checker,
    )


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
    create_codex_mcp(args.root).run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
