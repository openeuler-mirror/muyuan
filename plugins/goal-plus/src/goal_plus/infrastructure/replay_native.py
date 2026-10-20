"""Verify and retain the model-visible Codex prefix at an Evidence return."""

from __future__ import annotations

from collections import Counter
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from goal_plus.domain.replay import ReplayForkPoint
from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.host_observability import discover_codex_session_file
from goal_plus.infrastructure.replay_capture import FileReplayCapture
from goal_plus.infrastructure.state.files import exclusive_file_lock, write_json


def _contains_snapshot(value: Any, snapshot_id: str) -> bool:
    if isinstance(value, str):
        try:
            return _contains_snapshot(json.loads(value), snapshot_id)
        except (ValueError, RecursionError):
            return False
    if isinstance(value, list):
        if sha256_text(canonical_json(value)) == snapshot_id:
            return True
        return any(_contains_snapshot(item, snapshot_id) for item in value)
    if isinstance(value, dict):
        return any(_contains_snapshot(item, snapshot_id) for item in value.values())
    return False


def _delivered_once(output: Any, snapshot_id: str) -> bool:
    """An outer exec result must contain exactly one substantive tool return."""
    if not isinstance(output, list):
        return False
    substantive = []
    for item in output:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            return False
        value = item["text"]
        if value.startswith(("Script completed\n", "Script running with cell ID ")):
            continue
        substantive.append(value)
    return len(substantive) == 1 and _contains_snapshot(substantive[0], snapshot_id)


def verified_turn_prefix(
    rollout: Path, point: ReplayForkPoint,
) -> tuple[list[dict[str, Any]], str]:
    """Return exact raw Responses items through one completed outer exec result."""
    active = False
    completed = False
    items: list[dict[str, Any]] = []
    calls: dict[str, dict[str, Any]] = {}
    matches: list[tuple[int, str]] = []
    with rollout.open("r", encoding="utf-8") as stream:
        for line in stream:
            event = json.loads(line)
            payload = event.get("payload") or {}
            if event.get("type") == "event_msg" and payload.get("type") == "task_started":
                if payload.get("turn_id") == point.native_turn_id:
                    if active:
                        raise RuntimeError("NativeTurnAmbiguous")
                    active = True
                    items = []
                    calls = {}
                elif active and not completed:
                    raise RuntimeError("NativeTurnInterleaved")
            elif active and event.get("type") == "event_msg" and payload.get("type") == "task_complete":
                if payload.get("turn_id") == point.native_turn_id:
                    completed = True
                    active = False
            elif active and event.get("type") == "compacted":
                raise RuntimeError("NativeCompactionInsideForkTurn")
            elif active and event.get("type") == "response_item":
                if not isinstance(payload, dict):
                    raise RuntimeError("NativeRawItemMissing")
                items.append(payload)
                kind = payload.get("type")
                call_id = payload.get("call_id")
                if kind in {"custom_tool_call", "function_call"} and isinstance(call_id, str):
                    calls[call_id] = payload
                if kind in {"custom_tool_call_output", "function_call_output"}:
                    if not isinstance(call_id, str):
                        continue
                    if _delivered_once(payload.get("output"), point.global_snapshot_id):
                        call = calls.get(call_id) or {}
                        if str(call.get("name") or "").split(".")[-1] != "exec":
                            raise RuntimeError("NativeEvidenceOuterCallMismatch")
                        code = call.get("input")
                        expected_tool = (
                            "goal_plus_search_get_global_evidence"
                            if point.source == "explicit"
                            else "goal_plus_search_run_verifier"
                        )
                        if (
                            not isinstance(code, str)
                            or len(re.findall(r"\btools\.[A-Za-z_][A-Za-z_0-9]*", code)) != 1
                            or expected_tool not in code
                            or any(token in code for token in ("eval(", "Function(", "Promise.all", "for(", "while("))
                        ):
                            raise RuntimeError("NativeOuterExecNotSingleEvidenceOperation")
                        matches.append((len(items), call_id))
    if not completed:
        raise RuntimeError("NativeTurnIncomplete")
    if len(matches) != 1:
        raise RuntimeError("NativeEvidenceResultMissingOrAmbiguous")
    count, call_id = matches[0]
    prefix = items[:count]
    if not prefix or prefix[0].get("type") != "message" or prefix[0].get("role") != "user":
        raise RuntimeError("NativeTurnInputMissing")
    return prefix, call_id


def verify_injected_prefix(thread_id: str, prefix: list[dict[str, Any]]) -> None:
    home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    sessions = home if home.name == "sessions" else home / "sessions"
    files = list(sessions.rglob(f"*{thread_id}.jsonl"))
    if len(files) != 1:
        raise RuntimeError("ForkRolloutMissingOrAmbiguous")
    session_id = None
    items = []
    with files[0].open("r", encoding="utf-8") as stream:
        for line in stream:
            event = json.loads(line)
            if event.get("type") == "session_meta":
                session_id = (event.get("payload") or {}).get("id")
            elif event.get("type") == "response_item":
                items.append(event.get("payload"))
    def normalized(item: Any) -> Any:
        if isinstance(item, dict) and item.get("type") == "reasoning" and item.get("content") is None:
            return {key: value for key, value in item.items() if key != "content"}
        return item

    if (
        session_id != thread_id
        or [normalized(item) for item in items[-len(prefix):]]
        != [normalized(item) for item in prefix]
    ):
        raise RuntimeError("NativeInjectedPrefixReadbackMismatch")


def finalize_turn(root_dir: Path | str, thread_id: str, turn_id: str, *, completed: bool) -> None:
    capture = FileReplayCapture(root_dir)
    paths = sorted(capture.base.glob("fork_*/manifest.json"))
    points = [
        ReplayForkPoint.model_validate(json.loads(path.read_text(encoding="utf-8")))
        for path in paths
    ]
    counts = Counter(
        item.global_snapshot_id for item in points
        if (item.native_thread_id, item.native_turn_id) == (thread_id, turn_id)
    )
    duplicated = {
        snapshot_id for snapshot_id, count in counts.items() if count > 1
    }
    for path, point in zip(paths, points, strict=True):
        with exclusive_file_lock(path.parent / "fork.lock"):
            point = ReplayForkPoint.model_validate(json.loads(path.read_text(encoding="utf-8")))
            if (point.native_thread_id, point.native_turn_id, point.state) != (
                thread_id, turn_id, "preparing"
            ):
                continue
            try:
                if not completed:
                    raise RuntimeError("NativeTurnFailed")
                if point.global_snapshot_id in duplicated:
                    raise RuntimeError("NativeEvidenceCallbackAmbiguous")
                session = capture.queries.read_agent_session(point.agent_session_id, point.run_id)
                if session.session_handle.external_id != thread_id:
                    raise RuntimeError("NativeThreadIdentityMismatch")
                rollout = discover_codex_session_file(session)
                if rollout is None:
                    raise RuntimeError("NativeRolloutUnavailable")
                for attempt in range(10):
                    try:
                        prefix, outer_id = verified_turn_prefix(rollout, point)
                        break
                    except RuntimeError as exc:
                        if str(exc) != "NativeTurnIncomplete" or attempt == 9:
                            raise
                        time.sleep(0.1)
                prefix_digest = sha256_text(canonical_json(prefix))
                write_json(path.parent / "prefix.json", prefix)
                point = point.model_copy(update={
                    "state": "ready", "native_prefix_digest": prefix_digest,
                    "native_outer_call_id": outer_id,
                })
            except (OSError, ValueError, RuntimeError) as exc:
                point = point.model_copy(update={
                    "state": "unavailable", "reason": f"{type(exc).__name__}: {exc}",
                })
            write_json(path, point.model_dump(mode="json"))
