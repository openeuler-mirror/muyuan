from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from queue import Empty, Queue
from threading import Thread
from typing import Any, Callable, Protocol

from goal_plus.agent_pool import HostPoolContract
from goal_plus.execution.managed_process import ManagedProcess
from goal_plus.host_observability import (
    collect_codex_observability,
    collect_pi_observability,
)
from goal_plus.domain.agents import (
    AgentHostKind, INNER_AGENT_AUTORESEARCH, INNER_AGENT_COMMON,
    WORKSPACE_LEDGER_PROJECTION_NONE,
    WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV,
)
from goal_plus.ports.candidate_host import ResolvedInnerAgentBinding
from goal_plus.domain.worker_policy import resolved_inner_agent_id
from goal_plus.domain.search import AgentSessionRecord, FrozenSpec, CandidateTask
from goal_plus.domain.artifacts import WorkspaceBackend
from goal_plus.paths import DEFAULT_RUNTIME_ROOT


PORTABLE_STRATEGY_MODES = {
    "agent",
    "agent_guided",
    "default",
    "random",
    "random_mode",
}
CODEX_MIN_WAIT_SECONDS = 10
MODEL_DISCOVERY_STOP_SECONDS = 2.0



class UnsupportedHostCapability(RuntimeError):
    """Raised when a host cannot provide a requested worker capability."""


@dataclass(frozen=True)
class HostCapabilities:
    search_enabled: bool = True
    disabled_reason: str | None = None
    supports_soft_closeout: bool = False
    supports_model_discovery: bool = False
    supports_model_override: bool = False
    supports_reasoning_effort: bool = False
    supports_service_tier: bool = False
    supports_agent_profile: bool = False
    supports_usage_metadata: bool = False
    supports_process_kill: bool = False
    supported_workspace_backends: frozenset[WorkspaceBackend] = frozenset()
    supports_process_environment: bool = False
    supported_inner_agents: frozenset[str] = frozenset()
    pool: HostPoolContract = field(default_factory=HostPoolContract)


@dataclass(frozen=True)
class HostBinding:
    name: AgentHostKind
    adapter_version: str
    capabilities: HostCapabilities


class HostModelCatalog(Protocol):
    def list_available_models(self, query: str | None = None) -> list[dict[str, Any]]: ...


class CandidateLaunchAdapter(Protocol):
    def build_launch_payload(
        self,
        *,
        native_agent_type: str | None,
        candidate_id: str,
        agent_session_id: str,
        short_intent: str,
        one_paragraph_idea: str,
        worker_budget: dict[str, Any] | None = None,
        worker_launch: dict[str, Any] | None = None,
        root: str | None = None,
        cwd: str | None = None,
        worker_prompt: str | None = None,
        process_environment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        ...

    def build_continue_payload(
        self,
        *,
        native_agent_type: str | None,
        candidate_id: str,
        agent_session_id: str,
        external_id: str | None,
        task_name: str | None,
        short_intent: str,
        one_paragraph_idea: str,
        root: str | None = None,
        cwd: str | None = None,
        worker_prompt: str | None = None,
        worker_budget: dict[str, Any] | None = None,
        worker_launch: dict[str, Any] | None = None,
        host_metadata: dict[str, Any] | None = None,
        process_environment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        ...


def _normalize_mode(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def portable_strategy_mode(value: str) -> bool:
    return _normalize_mode(value) in PORTABLE_STRATEGY_MODES


def _send_json_line(process: subprocess.Popen[str], payload: dict[str, Any]) -> None:
    if process.stdin is None:
        raise RuntimeError("host model discovery process has no stdin")
    process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
    process.stdin.flush()


def _read_json_line_until(
    process: subprocess.Popen[str],
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    if process.stdout is None:
        raise RuntimeError("host model discovery process has no stdout")

    responses: Queue[dict[str, Any] | Exception | None] = Queue(maxsize=1)

    def read_until_match() -> None:
        try:
            while True:
                line = process.stdout.readline()
                if not line:
                    responses.put(None)
                    return
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and predicate(payload):
                    responses.put(payload)
                    return
        except Exception as exc:  # pragma: no cover - defensive subprocess guard
            responses.put(exc)

    Thread(target=read_until_match, daemon=True).start()
    try:
        response = responses.get(timeout=timeout_seconds)
    except Empty as exc:
        raise RuntimeError(
            "host model discovery timed out or exited without a response"
        ) from exc
    if response is None:
        raise RuntimeError("host model discovery exited without a response")
    if isinstance(response, Exception):
        raise RuntimeError("host model discovery response reader failed") from response
    return response


def _filter_available_models(
    models: list[dict[str, Any]], query: str | None
) -> list[dict[str, Any]]:
    normalized = (query or "").strip().casefold()
    if not normalized:
        return models
    return [
        model
        for model in models
        if normalized
        in " ".join(
            str(model.get(key) or "")
            for key in ("model", "model_id", "provider", "display_name")
        ).casefold()
    ]


def _codex_task_name(agent_session_id: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", agent_session_id.lower()).strip("_")
    return f"search_{normalized or 'agent'}"


def _soft_closeout_seconds(max_runtime_seconds: int) -> int:
    return min(45, max(5, int(max_runtime_seconds) // 5))


def _pi_soft_closeout_seconds(
    max_runtime_seconds: int,
    min_runtime_seconds: int | None,
) -> int:
    if min_runtime_seconds:
        return max(1, max_runtime_seconds - min_runtime_seconds)
    return _soft_closeout_seconds(max_runtime_seconds)


CODEX_CLOSEOUT_MESSAGE = (
    "Worker 的截止时间临近。停止启动新工作；如有需要，最后运行一次 "
    "search_run_verifier，更新 .tmp/handoff.json，并返回简洁摘要。"
)

WORKER_BOUNDARY = (
    "你是 Search 候选 worker，不是搜索编排器。首先使用提供的 agent_session_id "
    "调用 search_get_agent_context，以返回的 candidate_task 为当前工作契约：只在其中指定的 "
    "workspace 工作，遵守可修改范围和 instructions。独立选择候选内的技术方向；规划、创建"
    "其他 candidate、最终选择、报告和 promotion 属于父 agent。"
)


def _worker_contract(worker_prompt: str | None) -> str:
    """Keep the portable worker boundary even when agent metadata is hidden."""
    prompt = (worker_prompt or "").strip()
    return prompt or WORKER_BOUNDARY


def _codex_budget_control(
    target: str,
    worker_budget: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not worker_budget:
        return None
    max_runtime_seconds = worker_budget.get("max_runtime_seconds")
    budget_control: dict[str, Any] = {
        "mode": "parent_watchdog",
        "max_runtime_seconds": max_runtime_seconds,
        "on_exceed": worker_budget.get("on_exceed", "interrupt"),
        "interrupt_tool": "interrupt_agent",
        "interrupt_target": target,
    }
    if max_runtime_seconds is not None:
        soft_closeout_seconds = max(
            CODEX_MIN_WAIT_SECONDS,
            _soft_closeout_seconds(int(max_runtime_seconds)),
        )
        initial_wait_seconds = int(max_runtime_seconds) - soft_closeout_seconds
        if initial_wait_seconds < CODEX_MIN_WAIT_SECONDS:
            raise ValueError(
                "codex worker_budget.max_runtime_seconds must accommodate "
                "two native wait intervals"
            )
        min_runtime_seconds = worker_budget.get("min_runtime_seconds")
        if (
            min_runtime_seconds is not None
            and int(min_runtime_seconds) >= initial_wait_seconds
        ):
            raise ValueError(
                "codex worker_budget.min_runtime_seconds must end before the "
                "parent watchdog soft-closeout point; increase "
                "max_runtime_seconds to reserve worker closeout time"
            )
        budget_control.update(
            {
                "initial_wait_timeout_ms": initial_wait_seconds * 1000,
                "soft_closeout_seconds": soft_closeout_seconds,
                "closeout_tool": "send_message",
                "closeout_target": target,
                "closeout_message": CODEX_CLOSEOUT_MESSAGE,
                "final_wait_timeout_ms": soft_closeout_seconds * 1000,
            }
        )
        if min_runtime_seconds is not None or worker_budget.get(
            "min_verifier_runs"
        ) is not None:
            budget_control["autoresearch_lease"] = {
                "mode": "subagent_stop",
                "min_runtime_seconds": int(min_runtime_seconds or 0),
                "min_verifier_runs": int(
                    worker_budget.get("min_verifier_runs") or 1
                ),
                "start_event": "native_child_session",
                "release_before_parent_closeout": True,
            }
    if worker_budget.get("max_turns") is not None:
        budget_control["max_turns_hint"] = worker_budget["max_turns"]
    return budget_control


class CodexModelCatalog:
    name: AgentHostKind = "codex"

    def list_available_models(
        self,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            process = subprocess.Popen(
                ["codex", "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise UnsupportedHostCapability(
                "codex model discovery requires the `codex` executable"
            ) from exc
        try:
            _send_json_line(
                process,
                {
                    "method": "initialize",
                    "id": "goal-plus-initialize",
                    "params": {
                        "clientInfo": {
                            "name": "goal_plus",
                            "title": "Goal Plus",
                            "version": host_binding(self.name).adapter_version,
                        }
                    },
                },
            )
            initialized = _read_json_line_until(
                process,
                lambda payload: payload.get("id") == "goal-plus-initialize",
            )
            if initialized.get("error") is not None:
                raise RuntimeError(
                    f"codex app-server initialize failed: {initialized['error']}"
                )
            _send_json_line(process, {"method": "initialized", "params": {}})
            _send_json_line(
                process,
                {
                    "method": "model/list",
                    "id": "goal-plus-model-list",
                    "params": {"limit": 100, "includeHidden": False},
                },
            )
            response = _read_json_line_until(
                process,
                lambda payload: payload.get("id") == "goal-plus-model-list",
            )
            if response.get("error") is not None:
                raise RuntimeError(
                    f"codex model/list failed: {response['error']}"
                )
            data = response.get("result", {}).get("data", [])
            models = [
                {
                    "model": str(item.get("model") or item["id"]),
                    "model_id": str(item["id"]),
                    "provider": "codex",
                    "display_name": item.get("displayName") or item.get("id"),
                    "reasoning": bool(item.get("supportedReasoningEfforts")),
                    "reasoning_efforts": [
                        effort.get("reasoningEffort")
                        for effort in item.get("supportedReasoningEfforts", [])
                        if effort.get("reasoningEffort")
                    ],
                    "input_modalities": item.get("inputModalities")
                    or ["text", "image"],
                    "source": "codex_app_server_model_list",
                }
                for item in data
                if isinstance(item, dict) and item.get("id")
            ]
            return _filter_available_models(models, query)
        finally:
            ManagedProcess(
                process, term_timeout=MODEL_DISCOVERY_STOP_SECONDS,
                kill_timeout=MODEL_DISCOVERY_STOP_SECONDS,
            ).terminate()


class CodexCandidateLaunch:
    name: AgentHostKind = "codex"

    def build_launch_payload(
        self,
        *,
        native_agent_type: str | None,
        candidate_id: str,
        agent_session_id: str,
        short_intent: str,
        one_paragraph_idea: str,
        worker_budget: dict[str, Any] | None = None,
        worker_launch: dict[str, Any] | None = None,
        root: str | None = None,
        cwd: str | None = None,
        worker_prompt: str | None = None,
        process_environment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        task_name = _codex_task_name(agent_session_id)
        worker_contract = _worker_contract(worker_prompt)
        payload = {
            "tool": "spawn_agent",
            "task_name": task_name,
            "agent_type": "default",
            "fork_turns": "none",
            "message": (
                f"{worker_contract}\n\n"
                f"agent_session_id={agent_session_id}; "
                f"candidate_id={candidate_id}; "
                f"assigned_worker_budget={worker_budget or 'host 默认值'}; "
                f"思路：{one_paragraph_idea}"
            ),
        }
        # The default worker contract is already embedded in ``message``. Map
        # it to Codex's built-in no-config role: selecting the project-local
        # role reloads config after model inheritance and can discard a
        # runtime-only parent model before service-tier validation. An explicit
        # ``default`` also prevents the orchestrator from inventing that custom
        # role when projecting the returned payload. Non-default roles remain
        # an explicit opt-in.
        if native_agent_type and native_agent_type != "search_candidate_agent":
            payload["agent_type"] = native_agent_type
        if worker_launch:
            payload.update(
                {
                    key: value
                    for key, value in worker_launch.items()
                    if key in {"model", "reasoning_effort", "service_tier"}
                    and value is not None
                }
            )
        budget_control = _codex_budget_control(task_name, worker_budget)
        if budget_control:
            payload["budget_control"] = budget_control
        return payload

    def build_continue_payload(
        self,
        *,
        native_agent_type: str | None,
        candidate_id: str,
        agent_session_id: str,
        external_id: str | None,
        task_name: str | None,
        short_intent: str,
        one_paragraph_idea: str,
        root: str | None = None,
        cwd: str | None = None,
        worker_prompt: str | None = None,
        worker_budget: dict[str, Any] | None = None,
        worker_launch: dict[str, Any] | None = None,
        host_metadata: dict[str, Any] | None = None,
        process_environment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        target = task_name or external_id
        if not target:
            raise UnsupportedHostCapability(
                "codex continuation requires a bound task name or agent id"
            )
        payload: dict[str, Any] = {
            "tool": "followup_task",
            "target": target,
            "message": (
                "continue_existing_agent_session=true; "
                f"agent_session_id={agent_session_id}; "
                f"candidate_id={candidate_id}; "
                "继续同一个 candidate 和 workspace；"
                f"指令：{one_paragraph_idea}"
            ),
        }
        budget_control = _codex_budget_control(target, worker_budget)
        if budget_control:
            payload["budget_control"] = budget_control
        return payload


class PiRpcModelCatalog:
    name: AgentHostKind = "pi-rpc"

    def list_available_models(
        self,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            process = subprocess.Popen(
                [
                    "pi",
                    "--mode",
                    "rpc",
                    "--no-session",
                    "--no-extensions",
                    "--no-skills",
                    "--no-context-files",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise UnsupportedHostCapability(
                "pi-rpc model discovery requires the `pi` executable"
            ) from exc
        try:
            _send_json_line(
                process,
                {"id": "goal-plus-model-list", "type": "get_available_models"},
            )
            response = _read_json_line_until(
                process,
                lambda payload: (
                    payload.get("id") == "goal-plus-model-list"
                    and payload.get("type") == "response"
                ),
            )
            if not response.get("success"):
                raise RuntimeError(
                    "pi get_available_models failed: "
                    + str(response.get("error") or response)
                )
            data = response.get("data", {}).get("models", [])
            models = [
                {
                    "model": f"{item['provider']}/{item['id']}",
                    "model_id": str(item["id"]),
                    "provider": str(item["provider"]),
                    "display_name": item.get("name") or item.get("id"),
                    "reasoning": bool(item.get("reasoning")),
                    "input_modalities": item.get("input") or ["text"],
                    "context_window": item.get("contextWindow"),
                    "max_tokens": item.get("maxTokens"),
                    "source": "pi_rpc_get_available_models",
                }
                for item in data
                if isinstance(item, dict) and item.get("provider") and item.get("id")
            ]
            return _filter_available_models(models, query)
        finally:
            ManagedProcess(
                process, term_timeout=MODEL_DISCOVERY_STOP_SECONDS,
                kill_timeout=MODEL_DISCOVERY_STOP_SECONDS,
            ).terminate()


class PiRpcCandidateLaunch:
    name: AgentHostKind = "pi-rpc"

    def _budget_control(
        self,
        worker_budget: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not worker_budget:
            return None
        max_runtime_seconds = worker_budget.get("max_runtime_seconds")
        min_runtime_seconds = worker_budget.get("min_runtime_seconds")
        budget_control: dict[str, Any] = {
            "mode": "pi_rpc_process_watchdog",
            "continuation": "native_session",
            "max_runtime_seconds": max_runtime_seconds,
            "on_exceed": worker_budget.get("on_exceed", "interrupt"),
        }
        if max_runtime_seconds is not None:
            budget_control["soft_closeout_seconds"] = _pi_soft_closeout_seconds(
                int(max_runtime_seconds),
                int(min_runtime_seconds) if min_runtime_seconds is not None else None,
            )
        min_verifier_runs = worker_budget.get("min_verifier_runs")
        if min_runtime_seconds is not None or min_verifier_runs is not None:
            budget_control["autoresearch_lease"] = {
                "mode": "pool_supervisor",
                "min_runtime_seconds": int(min_runtime_seconds or 0),
                "min_verifier_runs": int(min_verifier_runs or 1),
                "start_event": "initial_pool_dispatch",
                "cumulative_across_dispatches": True,
            }
        if worker_budget.get("max_turns") is not None:
            budget_control["max_turns_hint"] = worker_budget["max_turns"]
        return budget_control

    def _base_prompt(
        self,
        *,
        worker_prompt: str | None,
        agent_session_id: str,
        candidate_id: str,
        one_paragraph_idea: str,
        worker_budget: dict[str, Any] | None = None,
        resume: bool = False,
    ) -> str:
        labels = (
            f"agent_session_id={agent_session_id}; "
            f"candidate_id={candidate_id}; "
            f"assigned_worker_budget={worker_budget or 'host 默认值'}; "
            f"思路：{one_paragraph_idea}"
        )
        if resume:
            labels = "continue_existing_agent_session=true; " + labels
            return (
                "继续当前 Candidate 会话。沿用已加载的运行时上下文和 Evidence；"
                "严格执行本次 continuation 的思路，不改变 frozen inner-agent binding。"
                f"\n\nLaunch 标签：{labels}"
            )
        header = _worker_contract(worker_prompt)
        return f"{header}\n\nLaunch 标签：{labels}"

    def build_launch_payload(
        self,
        *,
        native_agent_type: str | None,
        candidate_id: str,
        agent_session_id: str,
        short_intent: str,
        one_paragraph_idea: str,
        worker_budget: dict[str, Any] | None = None,
        worker_launch: dict[str, Any] | None = None,
        root: str | None = None,
        cwd: str | None = None,
        worker_prompt: str | None = None,
        process_environment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool": "pi_rpc_worker",
            "agent_session_id": agent_session_id,
            "candidate_id": candidate_id,
            "session_id": agent_session_id,
            "root": root or DEFAULT_RUNTIME_ROOT,
            "cwd": cwd or ".",
            "description": f"{candidate_id} {short_intent}",
            "continuation": "native_session",
            "session_persistence": "cross_process",
            "prompt": self._base_prompt(
                worker_prompt=worker_prompt,
                agent_session_id=agent_session_id,
                candidate_id=candidate_id,
                one_paragraph_idea=one_paragraph_idea,
                worker_budget=worker_budget,
            ),
        }
        del native_agent_type
        if process_environment:
            payload["environment"] = process_environment
        if worker_launch:
            if worker_launch.get("model") is not None:
                payload["model_pattern"] = worker_launch["model"]
            if worker_launch.get("reasoning_effort") is not None:
                payload["thinking_level"] = worker_launch["reasoning_effort"]
        budget_control = self._budget_control(worker_budget)
        if budget_control:
            payload["budget_control"] = budget_control
        return payload

    def build_continue_payload(
        self,
        *,
        native_agent_type: str | None,
        candidate_id: str,
        agent_session_id: str,
        external_id: str | None,
        task_name: str | None,
        short_intent: str,
        one_paragraph_idea: str,
        root: str | None = None,
        cwd: str | None = None,
        worker_prompt: str | None = None,
        worker_budget: dict[str, Any] | None = None,
        worker_launch: dict[str, Any] | None = None,
        host_metadata: dict[str, Any] | None = None,
        process_environment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        session_id = (external_id or "").strip()
        if not session_id:
            raise UnsupportedHostCapability(
                "pi-rpc continuation requires a bound native session id"
            )
        payload: dict[str, Any] = {
            "tool": "pi_rpc_worker",
            "agent_session_id": agent_session_id,
            "candidate_id": candidate_id,
            "session_id": session_id,
            "root": root or DEFAULT_RUNTIME_ROOT,
            "cwd": cwd or ".",
            "description": f"{candidate_id} {short_intent}",
            "continuation": "native_session",
            "session_persistence": "cross_process",
            "prompt": self._base_prompt(
                worker_prompt=worker_prompt,
                agent_session_id=agent_session_id,
                candidate_id=candidate_id,
                one_paragraph_idea=one_paragraph_idea,
                worker_budget=worker_budget,
                resume=True,
            ),
        }
        del native_agent_type
        if process_environment:
            payload["environment"] = process_environment
        if worker_launch:
            if worker_launch.get("model") is not None:
                payload["model_pattern"] = worker_launch["model"]
            if worker_launch.get("reasoning_effort") is not None:
                payload["thinking_level"] = worker_launch["reasoning_effort"]
        metrics = (host_metadata or {}).get("pi_metrics")
        if isinstance(metrics, dict):
            payload["metrics_baseline"] = {
                "last_entry_id": metrics.get("final_last_entry_id"),
                "entry_count": metrics.get("final_entry_count"),
                "usage_total": metrics.get("usage_total"),
                "duration_seconds": metrics.get("duration_seconds"),
                "started_at": metrics.get("started_at"),
            }
        budget_control = self._budget_control(worker_budget)
        if budget_control:
            payload["budget_control"] = budget_control
        return payload


class ThinkThreadModelCatalog:
    name: AgentHostKind = "thinkthread"

    def list_available_models(
        self,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        require_host_enabled(self.name)
        client = thinkthread_host_client()
        client.preflight()
        profiles = client.self_view().get("profiles")
        if not isinstance(profiles, list):
            raise RuntimeError("Agent POSIX self view omitted profiles")
        discovered: dict[str, dict[str, Any]] = {}
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            alias = profile.get("alias")
            revision = profile.get("modelCatalogRevision")
            allowed = profile.get("allowedModels")
            if not isinstance(allowed, list):
                continue
            for raw_model in allowed:
                if not isinstance(raw_model, dict):
                    continue
                provider = raw_model.get("provider")
                model_id = raw_model.get("model")
                if not isinstance(provider, str) or not isinstance(model_id, str):
                    continue
                exact_ref = f"{provider}/{model_id}"
                entry = discovered.setdefault(
                    exact_ref,
                    {
                        "model": exact_ref,
                        "model_id": model_id,
                        "provider": provider,
                        "display_name": model_id,
                        "reasoning": None,
                        "input_modalities": ["text"],
                        "source": "thinkthread_agent_posix_self",
                        "profile_aliases": [],
                        "model_catalog_revisions": [],
                    },
                )
                if isinstance(alias, str) and alias not in entry["profile_aliases"]:
                    entry["profile_aliases"].append(alias)
                if (
                    isinstance(revision, str)
                    and revision not in entry["model_catalog_revisions"]
                ):
                    entry["model_catalog_revisions"].append(revision)
        return _filter_available_models(
            [discovered[key] for key in sorted(discovered)], query
        )


class ThinkThreadCandidateLaunch:
    name: AgentHostKind = "thinkthread"

    @staticmethod
    def _validate_worker_launch(
        worker_launch: dict[str, Any] | None,
    ) -> dict[str, str] | None:
        if not worker_launch:
            return None
        for field_name in ("reasoning_effort", "service_tier"):
            if worker_launch.get(field_name) is not None:
                raise UnsupportedHostCapability(
                    f"thinkthread does not support {field_name}"
                )
        model = worker_launch.get("model")
        if model is None:
            return None
        if not isinstance(model, str) or "/" not in model:
            raise UnsupportedHostCapability(
                "thinkthread model selection requires exact provider/model"
            )
        provider, model_id = model.split("/", 1)
        if not provider or not model_id:
            raise UnsupportedHostCapability(
                "thinkthread model selection requires exact provider/model"
            )
        return {"provider": provider, "model": model_id}

    @staticmethod
    def _budget_control(
        worker_budget: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not worker_budget:
            return None
        max_runtime_seconds = worker_budget.get("max_runtime_seconds")
        control: dict[str, Any] = {
            "mode": "thinkthread_child_watchdog",
            "max_runtime_seconds": max_runtime_seconds,
            "on_exceed": worker_budget.get("on_exceed", "interrupt"),
            "interrupt_sequence": ["INT", "TERM"],
            "continuation": "retained_child_session",
        }
        if max_runtime_seconds is not None:
            control["soft_closeout_seconds"] = _pi_soft_closeout_seconds(
                int(max_runtime_seconds),
                (
                    int(worker_budget["min_runtime_seconds"])
                    if worker_budget.get("min_runtime_seconds") is not None
                    else None
                ),
            )
        if (
            worker_budget.get("min_runtime_seconds") is not None
            or worker_budget.get("min_verifier_runs") is not None
        ):
            control["autoresearch_lease"] = {
                "mode": "thinkthread_pool_controller",
                "min_runtime_seconds": int(
                    worker_budget.get("min_runtime_seconds") or 0
                ),
                "min_verifier_runs": int(
                    worker_budget.get("min_verifier_runs") or 1
                ),
                "start_event": "initial_child_dispatch",
                "cumulative_across_wakes": True,
            }
        if worker_budget.get("max_turns") is not None:
            control["max_turns_hint"] = worker_budget["max_turns"]
        return control

    @staticmethod
    def _message(
        *,
        worker_prompt: str | None,
        agent_session_id: str,
        candidate_id: str,
        one_paragraph_idea: str,
        worker_budget: dict[str, Any] | None,
        resume: bool,
    ) -> str:
        if resume:
            return (
                "继续同一个 retained ThinkThread Child Session 和 private branch。"
                "这是一个新的 candidate dispatch，不是上一轮的完成通知；沿用已加载的工作契约、"
                "运行时上下文和 Evidence，不要重新执行首次启动流程。\n\n"
                f"continue_existing_agent_session=true; "
                f"agent_session_id={agent_session_id}; candidate_id={candidate_id}; "
                f"assigned_worker_budget={worker_budget or 'host 默认值'}; "
                f"本轮目标：{one_paragraph_idea}"
            )
        header = (worker_prompt or "首先调用 search_get_agent_context。").strip()
        return (
            f"{header}\n\n"
            "continue_existing_agent_session=false; "
            f"agent_session_id={agent_session_id}; candidate_id={candidate_id}; "
            f"assigned_worker_budget={worker_budget or 'host 默认值'}; "
            f"思路：{one_paragraph_idea}"
        )

    def build_launch_payload(
        self,
        *,
        native_agent_type: str | None = None,
        candidate_id: str = "",
        agent_session_id: str = "",
        short_intent: str = "",
        one_paragraph_idea: str = "",
        worker_budget: dict[str, Any] | None = None,
        worker_launch: dict[str, Any] | None = None,
        root: str | None = None,
        cwd: str | None = None,
        worker_prompt: str | None = None,
        process_environment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        del native_agent_type, cwd, process_environment
        require_host_enabled(self.name)
        model = self._validate_worker_launch(worker_launch)
        payload: dict[str, Any] = {
            "tool": "thinkthread_child",
            "agent_session_id": agent_session_id,
            "candidate_id": candidate_id,
            "session_id": agent_session_id,
            "root": root or DEFAULT_RUNTIME_ROOT,
            "description": f"{candidate_id} {short_intent}",
            "profile": "self",
            "fs": "private",
            "capabilities": ["thinkthread.message"],
            "continuation": "retained_child_session",
            "message": self._message(
                worker_prompt=worker_prompt,
                agent_session_id=agent_session_id,
                candidate_id=candidate_id,
                one_paragraph_idea=one_paragraph_idea,
                worker_budget=worker_budget,
                resume=False,
            ),
        }
        if model is not None:
            payload["model"] = model
        budget_control = self._budget_control(worker_budget)
        if budget_control:
            payload["budget_control"] = budget_control
        return payload

    def build_continue_payload(
        self,
        *,
        native_agent_type: str | None,
        candidate_id: str,
        agent_session_id: str,
        external_id: str | None,
        task_name: str | None,
        short_intent: str,
        one_paragraph_idea: str,
        root: str | None = None,
        cwd: str | None = None,
        worker_prompt: str | None = None,
        worker_budget: dict[str, Any] | None = None,
        worker_launch: dict[str, Any] | None = None,
        host_metadata: dict[str, Any] | None = None,
        process_environment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        del native_agent_type, task_name, cwd, host_metadata, process_environment
        require_host_enabled(self.name)
        if not external_id:
            raise UnsupportedHostCapability(
                "thinkthread continuation requires a bound Child id"
            )
        model = self._validate_worker_launch(worker_launch)
        payload: dict[str, Any] = {
            "tool": "thinkthread_child",
            "agent_session_id": agent_session_id,
            "candidate_id": candidate_id,
            "thinkthread_id": external_id,
            "root": root or DEFAULT_RUNTIME_ROOT,
            "description": f"{candidate_id} {short_intent}",
            "continuation": "retained_child_session",
            "wake": True,
            "message": self._message(
                worker_prompt=worker_prompt,
                agent_session_id=agent_session_id,
                candidate_id=candidate_id,
                one_paragraph_idea=one_paragraph_idea,
                worker_budget=worker_budget,
                resume=True,
            ),
        }
        if model is not None:
            payload["model"] = model
        budget_control = self._budget_control(worker_budget)
        if budget_control:
            payload["budget_control"] = budget_control
        return payload


_HOST_BINDINGS: dict[AgentHostKind, HostBinding] = {
    "codex": HostBinding("codex", "codex-app-server-v1", HostCapabilities(
        supports_soft_closeout=True,
        supports_model_discovery=True,
        supports_model_override=True,
        supports_reasoning_effort=True,
        supports_service_tier=True,
        supports_usage_metadata=True,
        supported_workspace_backends=frozenset({"copy", "git_worktree"}),
        supported_inner_agents=frozenset(
            {INNER_AGENT_AUTORESEARCH, INNER_AGENT_COMMON}
        ),
        pool=HostPoolContract(
            launch_mode="async",
            wait_mode="wait_any",
            continuation_mode="same_worker",
            deadline_mode="parent_watchdog",
            recovery_mode="host_resident",
            completion_stage="candidate_ready",
            submit_tool="spawn_agent",
            wait_tool="wait_agent",
            snapshot_tool="list_agents",
            continue_tool="followup_task",
            closeout_tool="send_message",
            interrupt_tool="interrupt_agent",
        ),
    )),
    "pi-rpc": HostBinding("pi-rpc", "pi-rpc-v1", HostCapabilities(
        supports_soft_closeout=True,
        supports_model_discovery=True,
        supports_model_override=True,
        supports_reasoning_effort=True,
        supports_usage_metadata=True,
        supports_process_kill=True,
        supported_workspace_backends=frozenset({"copy", "git_worktree"}),
        supports_process_environment=True,
        supported_inner_agents=frozenset(
            {INNER_AGENT_AUTORESEARCH, INNER_AGENT_COMMON}
        ),
        pool=HostPoolContract(
            launch_mode="async",
            wait_mode="wait_any",
            continuation_mode="native_session",
            deadline_mode="worker_watchdog",
            recovery_mode="supervisor_persisted",
            completion_stage="candidate_ready",
            open_tool="pi_search_pool_open",
            wait_tool="pi_search_pool_wait_any",
            snapshot_tool="pi_search_pool_snapshot",
            continue_tool="pi_search_pool_continue",
            closeout_tool="pi_search_pool_close",
            interrupt_tool="pi_search_pool_close",
        ),
    )),
    "thinkthread": HostBinding("thinkthread", "thinkthread-v3-pi", HostCapabilities(
        search_enabled=True,
        supports_agent_profile=True,
        supported_workspace_backends=frozenset({"thinkthread"}),
        supported_inner_agents=frozenset(
            {INNER_AGENT_AUTORESEARCH, INNER_AGENT_COMMON}
        ),
        pool=HostPoolContract(
            launch_mode="async",
            wait_mode="wait_any",
            continuation_mode="native_session",
            deadline_mode="worker_watchdog",
            recovery_mode="supervisor_persisted",
            completion_stage="candidate_ready",
            open_tool="thinkthread_search_pool_open",
            wait_tool="thinkthread_search_pool_wait_any",
            snapshot_tool="thinkthread_search_pool_snapshot",
            continue_tool="thinkthread_search_pool_continue",
            closeout_tool="thinkthread_search_pool_close",
            interrupt_tool="thinkthread_search_pool_close",
        ),
    )),
}
_MODEL_CATALOGS: dict[AgentHostKind, HostModelCatalog] = {
    "codex": CodexModelCatalog(), "pi-rpc": PiRpcModelCatalog(),
    "thinkthread": ThinkThreadModelCatalog(),
}
_CANDIDATE_LAUNCH: dict[AgentHostKind, CandidateLaunchAdapter] = {
    "codex": CodexCandidateLaunch(), "pi-rpc": PiRpcCandidateLaunch(),
    "thinkthread": ThinkThreadCandidateLaunch(),
}


def host_binding(host: AgentHostKind) -> HostBinding:
    return _HOST_BINDINGS[host]


def host_model_catalog(host: AgentHostKind) -> HostModelCatalog:
    return _MODEL_CATALOGS[host]


def candidate_launch_adapter(host: AgentHostKind) -> CandidateLaunchAdapter:
    return _CANDIDATE_LAUNCH[host]


def require_host_enabled(host: AgentHostKind) -> None:
    binding = host_binding(host)
    if not binding.capabilities.search_enabled:
        raise UnsupportedHostCapability(binding.capabilities.disabled_reason or f"{host} Search is disabled")


def thinkthread_host_client():
    from goal_plus.thinkthread_agent_posix import AgentPosixSdkClient
    return AgentPosixSdkClient()


def _collect_thinkthread_observability(
    session: AgentSessionRecord,
) -> dict[str, Any]:
    require_host_enabled("thinkthread")
    child_id = session.host_handle.external_id
    if not child_id:
        return {
            "host": "thinkthread",
            "available": False,
            "reason": "agent session is not bound to a ThinkThread Child",
        }
    try:
        child = thinkthread_host_client().invoke("thinkthread.get", {"id": child_id})
    except Exception as exc:
        return {
            "host": "thinkthread",
            "available": False,
            "reason": str(exc),
            "error_code": getattr(exc, "code", None),
        }
    return {
        "host": "thinkthread",
        "available": True,
        "thinkthread_id": child.get("thinkthreadId"),
        "agent_state": child.get("agentState"),
        "execution_state": child.get("executionState"),
        "pending_wake": child.get("pendingWake"),
        "completion": child.get("completion"),
        "last_wake_outcome": child.get("lastWakeOutcome"),
        "model": child.get("model"),
        "fs": child.get("fs"),
    }


def collect_host_observability(session: AgentSessionRecord) -> dict[str, Any]:
    if session.host == "codex":
        return collect_codex_observability(session)
    if session.host == "pi-rpc":
        return collect_pi_observability(session)
    if session.host == "thinkthread":
        return _collect_thinkthread_observability(session)
    raise ValueError(f"unsupported observation host: {session.host}")


def resolve_inner_agent_binding(
    host: AgentHostKind,
    inner_agent: str,
    *,
    legacy_native_agent_type: str | None = None,
) -> ResolvedInnerAgentBinding:
    """Resolve a logical inner-agent id without exposing host roles in specs."""

    adapter = host_binding(host)
    inner_agent_id = inner_agent.strip()
    if inner_agent_id not in adapter.capabilities.supported_inner_agents:
        supported = ", ".join(sorted(adapter.capabilities.supported_inner_agents))
        raise UnsupportedHostCapability(
            f"{host} worker_host does not support inner_agent={inner_agent_id!r}; "
            f"supported inner agents: {supported or 'none'}"
        )

    prompt_assets: dict[tuple[AgentHostKind, str], str] = {
        (
            "codex",
            INNER_AGENT_AUTORESEARCH,
        ): ".codex/agents/search_candidate_agent.toml",
        (
            "codex",
            INNER_AGENT_COMMON,
        ): ".codex/agents/search_candidate_common_agent.toml",
        (
            "pi-rpc",
            INNER_AGENT_AUTORESEARCH,
        ): ".pi/prompts/search-candidate-worker.md",
        (
            "pi-rpc",
            INNER_AGENT_COMMON,
        ): ".pi/prompts/search-candidate-common.md",
        (
            "thinkthread",
            INNER_AGENT_AUTORESEARCH,
        ): ".pi/prompts/search-candidate-worker.md",
    }
    native_agent_type = (
        legacy_native_agent_type.strip()
        if host == "codex" and legacy_native_agent_type
        else None
    )
    return ResolvedInnerAgentBinding(
        inner_agent_id=inner_agent_id,
        adapter_version=(
            "builtin-autoresearch-git-v1"
            if inner_agent_id == INNER_AGENT_AUTORESEARCH
            else f"builtin-{inner_agent_id}-v1"
        ),
        worker_host=host,
        host_adapter_version=adapter.adapter_version,
        prompt_asset=prompt_assets.get((host, inner_agent_id)),
        native_agent_type=native_agent_type,
        continuation_mode=adapter.capabilities.pool.continuation_mode,
        workspace_ledger_projection=(
            WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV
            if inner_agent_id == INNER_AGENT_AUTORESEARCH
            else WORKSPACE_LEDGER_PROJECTION_NONE
        ),
    )


def candidate_inner_agent_binding(
    frozen: FrozenSpec,
    candidate_task: CandidateTask,
) -> ResolvedInnerAgentBinding:
    worker_policy = candidate_task.strategy_metadata.get("worker_policy", {})
    inner_agent = str(
        worker_policy.get("inner_agent")
        or resolved_inner_agent_id(frozen.spec.strategy)
    )
    legacy_native_agent_type = worker_policy.get("legacy_worker_agent_type")
    if legacy_native_agent_type is None:
        legacy_native_agent_type = worker_policy.get("worker_agent_type")
    return resolve_inner_agent_binding(
        frozen.spec.strategy.worker_host,
        inner_agent,
        legacy_native_agent_type=(
            str(legacy_native_agent_type)
            if legacy_native_agent_type is not None
            else frozen.spec.strategy.worker_agent_type
        ),
    )
