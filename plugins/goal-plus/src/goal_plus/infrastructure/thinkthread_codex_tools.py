"""Codex Child MCP transport to its authenticated Parent's candidate tools."""

from typing import Any
from threading import Lock
from pathlib import Path
import hashlib
import os

from goal_plus.paths import DEFAULT_RUNTIME_ROOT, host_assets_root
from goal_plus.thinkthread_agent_posix import AgentPosixSdkClient, validate_thinkthread_root

# The wire request allows 120 seconds; reserve ten more for SDK startup and binding.
WORKER_BRIDGE_TIMEOUT_SECONDS = 130.0

# Authoritative projection of the host-declared execution identity to the TT Main.
# The managed Codex config carries no hooks or extensions, so the Main MCP
# instructions are the only channel that tells the model it runs inside a
# ThinkThread Root and which execution identity it must declare.
THINKTHREAD_MAIN_INSTRUCTIONS = (
    "本 MCP 由宿主在 ThinkThread Root 内启动。用户在 Root 给出的目标一律先经 "
    "goal_plus_host_start 启动（Root 内无 $goal-plus 命令、Stop-hook 续跑与 resume 入口）。"
    "准备 Search 时：goal_plus_list_models 与冻结一律使用 "
    'agent_harness="codex"、runtime_provider="thinkthread"，SearchSpec 必须 '
    'workspace.provider="thinkthread"；strategy.models 仅可引用 list_models 返回的精确模型，'
    "省略 reasoning_effort 与 service_tier。候选由宿主经 Message 协议分发与回收，"
    "候选中断后宿主按已 settle 快照重派。Goal 终结后调用 goal_plus_host_close_scopes "
    "关闭其 execution scope 完成资源清理（Root 内无 Stop-hook 自动回收）。"
)


def create_thinkthread_main_mcp(root_dir: str | Path = DEFAULT_RUNTIME_ROOT):
    from goal_plus.server import create_host_mcp

    configured = os.environ.get("GOAL_PLUS_ROOT")
    if not configured or not Path(configured).is_absolute():
        raise PermissionError("ThinkThread Main requires its host-declared absolute runtime root")
    root = Path(configured).resolve()
    if root.is_relative_to(Path.cwd().resolve()):
        raise PermissionError("ThinkThread runtime root must be outside the attached Root tree")
    if str(root_dir) != DEFAULT_RUNTIME_ROOT and Path(root_dir).resolve() != root:
        raise PermissionError("ThinkThread MCP root differs from its host-declared runtime root")
    validate_thinkthread_root(AgentPosixSdkClient(), agent_harness="codex")
    mcp = create_host_mcp(root, agent_harness="codex", caller_role="main")
    mcp.instructions = mcp.instructions + "\n" + THINKTHREAD_MAIN_INSTRUCTIONS
    _register_thinkthread_main_host_tools(mcp, root)
    return mcp


def _register_thinkthread_main_host_tools(mcp, root: Path) -> None:
    """Attach the TT Main host entrypoints; never registered outside a Root."""

    @mcp.tool()
    def goal_plus_host_start(raw_goal: str) -> dict[str, Any]:
        """在 ThinkThread Root 内启动 Goal 的唯一宿主入口。

        Root 内没有 `$goal-plus` 命令、Stop-hook 续跑或 resume 入口；用户在 Root
        给出的目标必须先经本工具启动，将用户目标原文（含 `mode=`、`max_parallel=` 等
        配置前缀）逐字作为 `raw_goal` 传入。启动后按 goal_plus_status 的 next_action
        继续；同一 Root session 已持有未终结 Goal 时本调用失败，应继续既有 Goal 而不是
        新建。重试同一原文返回既有 Goal，不会重复创建。
        """
        from datetime import datetime, timezone

        from goal_plus.goal_plus import FileGoalPlusRuntime
        from goal_plus.server import native_mcp_caller
        from goal_plus.thinkthread_connectivity import preflight_thinkthread_codex

        # TT's Main hook path is intentionally absent. Validate the Child's
        # managed Codex wiring before creating any durable Goal state. The
        # first real turn is the provider availability proof.
        preflight_thinkthread_codex()
        caller = native_mcp_caller()
        identity = "\0".join((caller, raw_goal))
        invocation = {
            "agent_harness": "codex",
            "transport": "codex_user_prompt_submit",
            "command": "$goal-plus",
            "action": "start",
            "session_id": caller,
            "invocation_id": "codex-tt:" + hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            "raw_input_sha256": hashlib.sha256(raw_goal.encode("utf-8")).hexdigest(),
            "invoked_at": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        runtime = FileGoalPlusRuntime(root)
        record = runtime.start_from_host_command(raw_goal, invocation=invocation)
        record = runtime.activate_session(
            record.goal_plus_id,
            {
                "agent_harness": "codex",
                "session_id": caller,
                "metadata": {
                    "authorized_by": "thinkthread_main_mcp",
                    "host_invocation_id": invocation["invocation_id"],
                },
            },
        )
        return {
            "goal_plus_id": record.goal_plus_id,
            "goal_revision": record.goal_revision,
            "status": record.status,
            "entrypoint_status": record.entrypoint_status,
            "command_config": record.command_config.model_dump(mode="json", exclude_none=True),
        }

    @mcp.tool()
    def goal_plus_host_close_scopes(goal_plus_id: str) -> dict[str, Any]:
        """Goal 终结后关闭其全部宿主 execution scope 的唯一入口。

        Root 内没有 Stop-hook，session 结束时不会自动回收 scope；Goal 到达
        终结状态（complete/abandoned）后必须经本工具关闭其关联 Search run 的
        execution scope，回收候选手柄、annotation 与 verifier 资源。重复调用
        返回已关闭 scope，不会报错。
        """
        from goal_plus.domain.goal_authorization import TERMINAL_STATUSES
        from goal_plus.goal_plus import FileGoalPlusRuntime
        from goal_plus.host_scope import close_execution_scope, execution_scope_for_run
        from goal_plus.server import native_mcp_caller

        caller = native_mcp_caller()
        record = FileGoalPlusRuntime(root).status(goal_plus_id)
        binding = record.active_session
        if (
            binding is None
            or binding.agent_harness != "codex"
            or binding.session_id != caller
            or binding.state in {"detached", "stale"}
        ):
            raise PermissionError("ThinkThread Main does not own this Goal's active session")
        if record.status not in TERMINAL_STATUSES:
            raise RuntimeError("ThinkThread Main closes scopes only after the Goal is terminal")
        run_ids = {item.run_id for item in record.search_tasks if item.run_id}
        if record.linked_search is not None and record.linked_search.run_id:
            run_ids.add(record.linked_search.run_id)
        scopes = []
        for run_id in sorted(run_ids):
            scope = execution_scope_for_run(root_dir=root, run_id=run_id)
            if scope is None:
                continue
            if scope.get("agent_harness") != "codex" or scope.get("owner_session_id") != caller:
                raise PermissionError("ThinkThread Main cannot close another Main's execution scope")
            if scope.get("state") == "closed":
                scopes.append({"scope_id": scope["scope_id"], "run_id": run_id, "state": "closed"})
                continue
            closed = close_execution_scope(
                root_dir=root, scope_id=str(scope["scope_id"]),
                reason="thinkthread_main_goal_terminal",
            )
            scopes.append({
                "scope_id": closed.get("scope_id"), "run_id": run_id,
                "state": closed.get("state"), "cleanup": closed.get("cleanup"),
            })
        return {"goal_plus_id": goal_plus_id, "status": record.status, "scopes": scopes}


def _worker_bridge_transport() -> AgentPosixSdkClient:
    return AgentPosixSdkClient(
        bridge_path=host_assets_root() / "assets/codex/thinkthread-worker-bridge.mts",
    )


def _child_binding(transport: AgentPosixSdkClient) -> tuple[str, str | None]:
    binding = transport.invoke("binding")
    session_id = binding.get("agentSessionId")
    if not isinstance(session_id, str) or not session_id:
        raise PermissionError("ThinkThread Child has no session binding")
    return session_id, binding.get("thinkthreadRole")


def _child_mcp(transport: AgentPosixSdkClient, session_id: str):
    from goal_plus.server import create_mcp, native_mcp_caller

    native_thread: str | None = None
    identity_lock = Lock()

    def call(name: str, arguments: dict[str, Any]):
        nonlocal native_thread
        caller = native_mcp_caller()
        with identity_lock:
            if native_thread is not None and caller != native_thread:
                raise PermissionError("ThinkThread MCP native thread changed")
            native_thread = caller
        return transport.invoke("call", {
            "agent_session_id": session_id, "name": name, "arguments": arguments,
        }, timeout_seconds=WORKER_BRIDGE_TIMEOUT_SECONDS)

    return create_mcp(caller_role="candidate", caller_agent_session_id=session_id,
                      candidate_tool_call=call)


def create_thinkthread_candidate_mcp():
    """Serve a Candidate Child through its retained dispatch binding.

    The TT profile child environment cannot distinguish candidate from annotator
    Children (thinkthread.spawn carries no per-child env), so the dispatch
    message is the single source of truth for the Child role.
    """

    transport = _worker_bridge_transport()
    session_id, role = _child_binding(transport)
    if role == "annotator":
        return _annotator_mcp(transport, session_id)
    if role not in (None, "candidate"):
        raise PermissionError(f"ThinkThread Child has an unknown dispatch role: {role}")
    return _child_mcp(transport, session_id)


# Authoritative tool surface of a retained Evidence Annotation Child: the six
# Message-RPC operations the host annotation service settles, nothing else.
THINKTHREAD_ANNOTATOR_INSTRUCTIONS = (
    "本 MCP 由宿主在 ThinkThread Annotation Child 内启动。只处理 Runtime 已录取的"
    " annotation task：goal_plus_thinkthread_annotation_next 领取任务；只可使用"
    " goal_plus_thinkthread_artifact_view_list/stat/pread/diff 读取授权的"
    " target/peer immutable snapshots；最后调用 goal_plus_thinkthread_annotation_submit"
    " 发布 annotation，再领取下一个。不得运行命令、代码、测试或 verifier；不得修改"
    " Root、candidate branch、score 或 selection。"
)


def _annotator_mcp(transport: AgentPosixSdkClient, session_id: str):
    from typing import Annotated, Any as TypingAny

    from fastmcp import FastMCP
    from pydantic import Field

    from goal_plus._version import __version__
    from goal_plus.domain.annotation import SupplementalEvaluation, ToolViewOutput
    from goal_plus.server import native_mcp_caller

    native_thread: str | None = None
    identity_lock = Lock()

    def call(name: str, arguments: dict[str, Any]):
        nonlocal native_thread
        caller = native_mcp_caller()
        with identity_lock:
            if native_thread is not None and caller != native_thread:
                raise PermissionError("ThinkThread MCP native thread changed")
            native_thread = caller
        return transport.invoke("call", {
            "agent_session_id": session_id, "name": name, "arguments": arguments,
        }, timeout_seconds=WORKER_BRIDGE_TIMEOUT_SECONDS)

    mcp = FastMCP("goal-plus", version=__version__, instructions=THINKTHREAD_ANNOTATOR_INSTRUCTIONS)

    @mcp.tool()
    def goal_plus_thinkthread_annotation_next() -> dict[str, Any]:
        """领取下一个 fenced immutable Evidence annotation task。

        返回待 annotation 的 task（含授权的 target/peer Artifact View id）；没有更多
        task 时返回 idle。领取后必须先用 view 工具检查授权快照，再经
        goal_plus_thinkthread_annotation_submit 发布，然后才能领取下一个。
        """
        return call("goal_plus_thinkthread_annotation_next", {})

    @mcp.tool()
    def goal_plus_thinkthread_artifact_view_list(view_id: str, path: str = ".") -> dict[str, Any]:
        """列出授权 immutable Artifact View 的目录内容。

        view_id 必须逐字使用 annotation_next 返回的 artifact_views[].view_id；
        不得使用其中 artifact_ref.id（例如 fsnap-*）替代。
        """
        return call("goal_plus_thinkthread_artifact_view_list", {"view_id": view_id, "path": path})

    @mcp.tool()
    def goal_plus_thinkthread_artifact_view_stat(view_id: str, path: str = ".") -> dict[str, Any]:
        """读取授权 Artifact View 中路径的元数据（类型、大小、哈希）。

        view_id 必须是 annotation_next 返回的 view_id，不是 artifact_ref.id。
        """
        return call("goal_plus_thinkthread_artifact_view_stat", {"view_id": view_id, "path": path})

    @mcp.tool()
    def goal_plus_thinkthread_artifact_view_pread(
        view_id: str, path: str, offset: int = 0, length: int = 64 * 1024,
    ) -> dict[str, Any]:
        """按 offset/length 读取授权 Artifact View 中文件的内容片段。

        view_id 必须是 annotation_next 返回的 view_id，不是 artifact_ref.id。
        """
        return call(
            "goal_plus_thinkthread_artifact_view_pread",
            {"view_id": view_id, "path": path, "offset": offset, "length": length},
        )

    @mcp.tool()
    def goal_plus_thinkthread_artifact_view_diff(base_view_id: str, target_view_id: str) -> dict[str, Any]:
        """读取两个授权 Artifact View 之间的受控 diff（peer 比较基线）。

        两个参数都必须逐字使用 annotation_next 返回的 view_id；不得传入
        artifact_ref.id（例如 fsnap-*）。
        """
        return call(
            "goal_plus_thinkthread_artifact_view_diff",
            {"base_view_id": base_view_id, "target_view_id": target_view_id},
        )

    @mcp.tool()
    def goal_plus_thinkthread_annotation_submit(
        description: Annotated[str, Field(min_length=1, max_length=1000)],
        supplemental_evaluation: SupplementalEvaluation | None = None,
        tool_views: list[ToolViewOutput] | None = None,
    ) -> dict[str, Any]:
        """发布当前 task 的 annotation（单行客观 description 与可选补充评价）。"""
        return call("goal_plus_thinkthread_annotation_submit", {
            "description": description,
            "supplemental_evaluation": (
                supplemental_evaluation.model_dump(mode="json")
                if supplemental_evaluation is not None else None
            ),
            "tool_views": [
                item.model_dump(mode="json") for item in (tool_views or [])
            ],
        })

    return mcp


def create_thinkthread_annotator_mcp():
    """Serve an Annotation Child; its dispatch binding must declare the role."""

    transport = _worker_bridge_transport()
    session_id, role = _child_binding(transport)
    if role != "annotator":
        raise PermissionError("ThinkThread annotator MCP requires its annotator dispatch binding")
    return _annotator_mcp(transport, session_id)
