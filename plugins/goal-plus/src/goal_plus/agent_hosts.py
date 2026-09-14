from __future__ import annotations

import json
import re
import subprocess
from queue import Empty, Queue
from threading import Thread
from typing import Any, Callable

from goal_plus.execution.managed_process import ManagedProcess
from goal_plus.host_observability import (
    collect_codex_observability,
    collect_pi_observability,
)
from goal_plus.domain.agents import AgentHarnessKind, AgentRuntimeProvider, validate_agent_execution
from goal_plus.domain.search import AgentSessionRecord
from goal_plus.infrastructure.host_bindings import (
    execution_binding as _execution_binding,
    require_execution_enabled as _require_execution_enabled,
)
from goal_plus.ports.agent_hosts import (
    CandidateLaunchAdapter, HostModelCatalog, UnsupportedHostCapability,
)
from goal_plus.paths import DEFAULT_RUNTIME_ROOT


MODEL_DISCOVERY_STOP_SECONDS = 2.0


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


WORKER_BOUNDARY = (
    "你是 Search 候选 worker，不是搜索编排器。首先使用提供的 agent_session_id "
    "调用 goal_plus_search_get_agent_context，以返回的 candidate_task 为当前工作契约：只在其中指定的 "
    "workspace 工作，遵守可修改范围和 instructions。独立选择候选内的技术方向；规划、创建"
    "其他 candidate、最终选择、报告和 promotion 属于父 agent。"
)


def _worker_contract(worker_prompt: str | None) -> str:
    """Keep the portable worker boundary even when agent metadata is hidden."""
    prompt = (worker_prompt or "").strip()
    return prompt or WORKER_BOUNDARY


def _budget_control(worker_budget: dict[str, Any] | None) -> dict[str, Any] | None:
    if not worker_budget:
        return None
    control = {
        "mode": "session_control",
        "max_runtime_seconds": worker_budget.get("max_runtime_seconds"),
        "on_exceed": worker_budget.get("on_exceed", "interrupt"),
    }
    if worker_budget.get("max_turns") is not None:
        control["max_turns_hint"] = worker_budget["max_turns"]
    return control


class CodexModelCatalog:
    name: AgentHarnessKind = "codex"

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
                            "version": _execution_binding(self.name).adapter_version,
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
    name: AgentHarnessKind = "codex"

    def build_launch_payload(
        self,
        *,
        candidate_id: str,
        agent_session_id: str,
        short_intent: str,
        one_paragraph_idea: str,
        worker_budget: dict[str, Any] | None = None,
        worker_launch: dict[str, Any] | None = None,
        root: str | None = None,
        worker_prompt: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "transport": "app-server",
            "continuation": "native_session",
            "agent_session_id": agent_session_id,
            "candidate_id": candidate_id,
            "message": (
                f"{_worker_contract(worker_prompt)}\n\n"
                f"agent_session_id={agent_session_id}; "
                f"candidate_id={candidate_id}; "
                f"assigned_worker_budget={worker_budget or 'host 默认值'}; "
                f"思路：{one_paragraph_idea}"
            ),
        }
        if worker_launch:
            payload.update({
                key: value for key, value in worker_launch.items()
                if key in {"model", "reasoning_effort", "service_tier"} and value is not None
            })
        budget_control = _budget_control(worker_budget)
        if budget_control:
            payload["budget_control"] = budget_control
        return payload

    def build_continue_payload(
        self,
        *,
        candidate_id: str,
        agent_session_id: str,
        external_id: str | None,
        short_intent: str,
        one_paragraph_idea: str,
        root: str | None = None,
        worker_prompt: str | None = None,
        worker_budget: dict[str, Any] | None = None,
        worker_launch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not external_id:
            raise UnsupportedHostCapability("codex continuation requires a bound native session id")
        payload: dict[str, Any] = {
            "transport": "app-server",
            "continuation": "native_session",
            "agent_session_id": agent_session_id,
            "candidate_id": candidate_id,
            "native_session_id": external_id,
            "message": (
                "continue_existing_agent_session=true; "
                f"agent_session_id={agent_session_id}; "
                f"candidate_id={candidate_id}; "
                "继续同一个 candidate 和 workspace；"
                f"指令：{one_paragraph_idea}"
            ),
        }
        budget_control = _budget_control(worker_budget)
        if budget_control:
            payload["budget_control"] = budget_control
        return payload


class PiRpcModelCatalog:
    name: AgentHarnessKind = "pi"

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
    name: AgentHarnessKind = "pi"

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
        candidate_id: str,
        agent_session_id: str,
        short_intent: str,
        one_paragraph_idea: str,
        worker_budget: dict[str, Any] | None = None,
        worker_launch: dict[str, Any] | None = None,
        root: str | None = None,
        worker_prompt: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "transport": "native-rpc",
            "agent_session_id": agent_session_id,
            "candidate_id": candidate_id,
            "session_id": agent_session_id,
            "description": f"{candidate_id} {short_intent}",
            "continuation": "native_session",
            "prompt": self._base_prompt(
                worker_prompt=worker_prompt,
                agent_session_id=agent_session_id,
                candidate_id=candidate_id,
                one_paragraph_idea=one_paragraph_idea,
                worker_budget=worker_budget,
            ),
        }
        if worker_launch:
            if worker_launch.get("model") is not None:
                payload["model_pattern"] = worker_launch["model"]
            if worker_launch.get("reasoning_effort") is not None:
                payload["thinking_level"] = worker_launch["reasoning_effort"]
        budget_control = _budget_control(worker_budget)
        if budget_control:
            payload["budget_control"] = budget_control
        return payload

    def build_continue_payload(
        self,
        *,
        candidate_id: str,
        agent_session_id: str,
        external_id: str | None,
        short_intent: str,
        one_paragraph_idea: str,
        root: str | None = None,
        worker_prompt: str | None = None,
        worker_budget: dict[str, Any] | None = None,
        worker_launch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session_id = (external_id or "").strip()
        if not session_id:
            raise UnsupportedHostCapability(
                "pi-rpc continuation requires a bound native session id"
            )
        payload: dict[str, Any] = {
            "transport": "native-rpc",
            "agent_session_id": agent_session_id,
            "candidate_id": candidate_id,
            "session_id": session_id,
            "description": f"{candidate_id} {short_intent}",
            "continuation": "native_session",
            "prompt": self._base_prompt(
                worker_prompt=worker_prompt,
                agent_session_id=agent_session_id,
                candidate_id=candidate_id,
                one_paragraph_idea=one_paragraph_idea,
                worker_budget=worker_budget,
                resume=True,
            ),
        }
        if worker_launch:
            if worker_launch.get("model") is not None:
                payload["model_pattern"] = worker_launch["model"]
            if worker_launch.get("reasoning_effort") is not None:
                payload["thinking_level"] = worker_launch["reasoning_effort"]
        budget_control = _budget_control(worker_budget)
        if budget_control:
            payload["budget_control"] = budget_control
        return payload


class ThinkThreadModelCatalog:
    name: AgentHarnessKind = "pi"
    runtime_provider: AgentRuntimeProvider = "thinkthread"

    def list_available_models(
        self,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        _require_execution_enabled(self.name, self.runtime_provider)
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
    name: AgentHarnessKind = "pi"
    runtime_provider: AgentRuntimeProvider = "thinkthread"

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
        header = (worker_prompt or "首先调用 goal_plus_search_get_agent_context。").strip()
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
        candidate_id: str = "",
        agent_session_id: str = "",
        short_intent: str = "",
        one_paragraph_idea: str = "",
        worker_budget: dict[str, Any] | None = None,
        worker_launch: dict[str, Any] | None = None,
        root: str | None = None,
        worker_prompt: str | None = None,
    ) -> dict[str, Any]:
        _require_execution_enabled(self.name, self.runtime_provider)
        model = self._validate_worker_launch(worker_launch)
        payload: dict[str, Any] = {
            "transport": "thinkthread",
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
        budget_control = _budget_control(worker_budget)
        if budget_control:
            payload["budget_control"] = budget_control
        return payload

    def build_continue_payload(
        self,
        *,
        candidate_id: str,
        agent_session_id: str,
        external_id: str | None,
        short_intent: str,
        one_paragraph_idea: str,
        root: str | None = None,
        worker_prompt: str | None = None,
        worker_budget: dict[str, Any] | None = None,
        worker_launch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require_execution_enabled(self.name, self.runtime_provider)
        if not external_id:
            raise UnsupportedHostCapability(
                "thinkthread continuation requires a bound Child id"
            )
        model = self._validate_worker_launch(worker_launch)
        payload: dict[str, Any] = {
            "transport": "thinkthread",
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
        budget_control = _budget_control(worker_budget)
        if budget_control:
            payload["budget_control"] = budget_control
        return payload


_MODEL_CATALOGS: dict[AgentHarnessKind, HostModelCatalog] = {
    "codex": CodexModelCatalog(), "pi": PiRpcModelCatalog(),
}
_CANDIDATE_LAUNCH: dict[AgentHarnessKind, CandidateLaunchAdapter] = {
    "codex": CodexCandidateLaunch(), "pi": PiRpcCandidateLaunch(),
}


def host_model_catalog(agent_harness: AgentHarnessKind, runtime_provider: AgentRuntimeProvider = "direct") -> HostModelCatalog:
    validate_agent_execution(agent_harness, runtime_provider)
    if runtime_provider == "thinkthread":
        return ThinkThreadModelCatalog()
    return _MODEL_CATALOGS[agent_harness]


def candidate_launch_adapter(agent_harness: AgentHarnessKind, runtime_provider: AgentRuntimeProvider = "direct") -> CandidateLaunchAdapter:
    validate_agent_execution(agent_harness, runtime_provider)
    if runtime_provider == "thinkthread":
        return ThinkThreadCandidateLaunch()
    return _CANDIDATE_LAUNCH[agent_harness]


def thinkthread_host_client():
    from goal_plus.thinkthread_agent_posix import AgentPosixSdkClient
    return AgentPosixSdkClient()


def _collect_thinkthread_observability(
    session: AgentSessionRecord,
) -> dict[str, Any]:
    _require_execution_enabled("pi", "thinkthread")
    child_id = session.session_handle.external_id
    if not child_id:
        return {
            "agent_harness": "pi",
            "runtime_provider": "thinkthread",
            "available": False,
            "reason": "agent session is not bound to a ThinkThread Child",
        }
    try:
        child = thinkthread_host_client().invoke("thinkthread.get", {"id": child_id})
    except Exception as exc:
        return {
            "agent_harness": "pi",
            "runtime_provider": "thinkthread",
            "available": False,
            "reason": str(exc),
            "error_code": getattr(exc, "code", None),
        }
    return {
        "agent_harness": "pi",
        "runtime_provider": "thinkthread",
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
    if session.runtime_provider == "thinkthread":
        return _collect_thinkthread_observability(session)
    if session.agent_harness == "codex":
        return collect_codex_observability(session)
    if session.agent_harness == "pi":
        return collect_pi_observability(session)
    raise ValueError(f"unsupported observation host: {session.agent_harness}")
