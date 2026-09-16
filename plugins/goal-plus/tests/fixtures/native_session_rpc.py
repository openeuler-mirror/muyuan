"""Controlled native wire peer; never launches a model or imports product code."""

import json
import os
from pathlib import Path
import sys
import time


agent_harness, scenario, marker = sys.argv[1:]
Path(marker).touch()
active_marker = Path(marker).with_name("active")
turn = 0
active = False
tool_reply_count = 0
tool_output = None
entries = []
state_path = Path(os.environ["NATIVE_FIXTURE_STATE"]) if os.environ.get("NATIVE_FIXTURE_STATE") else None
if state_path and os.environ.get("NATIVE_FIXTURE_RESUME") == "1":
    saved = json.loads(state_path.read_text())
    turn, entries = saved["turn"], saved["entries"]


def save_state():
    if state_path:
        state_path.write_text(json.dumps({"turn": turn, "entries": entries}))


save_state()


def emit(value):
    print(json.dumps(value), flush=True)


def codex_event(method, **params):
    emit({"method": method, "params": {"threadId": "session", **params}})


def complete(state="completed"):
    global active
    active = False
    active_marker.unlink(missing_ok=True)
    if agent_harness == "codex":
        codex_event("thread/tokenUsage/updated", turnId=str(turn), tokenUsage={"total": {
            "inputTokens": turn * 100, "cachedInputTokens": turn * 20,
            "outputTokens": turn * 10, "totalTokens": turn * 110,
        }})
        codex_event("thread/tokenUsage/updated", turnId="stale", tokenUsage={"total": {
            "inputTokens": 9999, "outputTokens": 999, "totalTokens": 10998,
        }})
        codex_event("item/completed", turnId=str(turn), item={
            "type": "agentMessage", "text": tool_output or f"answer-{turn}",
        })
        codex_event("turn/completed", turn={"id": str(turn), "status": state})
    else:
        for index in (1, 2):
            entries.append({"id": f"{turn}-{index}", "type": "message", "message": {
                "role": "assistant", "usage": {"input": turn * index, "output": index},
            }})
        emit({"type": "message_end", "message": {
            "role": "assistant", "usage": {"input": turn},
        }})
        emit({"type": "agent_end"})
        if scenario != "end_without_settled":
            emit({"type": "agent_settled"})
    save_state()


if scenario == "eof":
    sys.exit(0)
if scenario == "no_read":
    time.sleep(60)
    sys.exit(0)
if scenario == "callback":
    emit({"id": "server-request", "method": "permission/request", "params": {}})

for line in sys.stdin:
    request = json.loads(line)
    identity = request.get("id")
    method = request.get("method", request.get("type"))
    if identity is None:
        continue
    if agent_harness == "codex":
        if method is None and "result" in request:
            tool_reply_count += 1
            tool_output = json.dumps(request["result"])
            if scenario == "tool_repeat" and tool_reply_count == 1:
                emit(tool_call)
            elif active:
                complete()
            continue
        result = {}
        if method in {"thread/start", "thread/resume"}:
            if scenario.startswith("tool"):
                assert request["params"]["dynamicTools"][0]["name"] == "fixture_echo"
            result = {"thread": {"id": "session"}}
        elif method == "turn/start":
            turn += 1
            save_state()
            active = True
            active_marker.touch()
            codex_event("turn/completed", turn={"id": "stale", "status": "failed"})
            result = {"turn": {"id": str(turn)}}
            if scenario.startswith("tool"):
                codex_event("turn/started", turn={"id": str(turn)})
                tool_call = {"id": "tool-request", "method": "item/tool/call", "params": {
                    "threadId": "foreign" if scenario == "tool_foreign" else "session",
                    "turnId": "stale" if scenario == "tool_stale" else str(turn),
                    "callId": "call-1", "tool": "fixture_echo", "arguments": {"text": "from-mcp"},
                }}
                emit(tool_call)
            elif scenario != "interrupt":
                complete()
        elif method == "turn/interrupt":
            complete("interrupted")
        emit({"id": identity, "result": result})
    else:
        data = {}
        if method == "get_state":
            data = {"sessionId": "session", "isStreaming": active,
                    "isCompacting": False, "pendingMessageCount": 0}
        elif method == "prompt":
            turn += 1
            save_state()
            active = True
            active_marker.touch()
            emit({"type": "agent_settled"})
            emit({"type": "agent_start"})
            if scenario != "interrupt":
                complete()
        elif method == "abort":
            complete("interrupted")
        elif method == "get_last_assistant_text":
            data = {"text": f"answer-{turn}"}
        elif method == "get_entries":
            since = request.get("since")
            offset = next((index + 1 for index, entry in enumerate(entries) if entry["id"] == since), 0)
            data = {"entries": entries[offset:]}
        elif method == "get_session_stats":
            data = {"assistantMessages": len(entries)}
        emit({"type": "response", "id": identity, "command": method,
              "success": True, "data": data})
