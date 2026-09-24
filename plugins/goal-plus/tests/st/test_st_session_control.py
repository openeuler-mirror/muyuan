"""MCP-to-native-session feasibility probe, not a product launch adapter."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

from fastmcp import Client, FastMCP
import pytest

from goal_plus.execution.managed_process import ManagedProcess
from goal_plus.execution.process_output import PROCESS_OUTPUT_LIMIT_BYTES
from tests.st.hosts import ST_ACTIVE_ENV, st_model_for_host


class ProbeProcess:
    """Bounded JSON-line observation for these two disposable host probes."""

    def __init__(self, process: subprocess.Popen, *, timeout: float):
        self.process = process
        self.deadline = time.monotonic() + timeout
        self.events: queue.Queue = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        size = 0
        try:
            while line := self.process.stdout.readline(PROCESS_OUTPUT_LIMIT_BYTES + 1):
                size += len(line)
                if size > PROCESS_OUTPUT_LIMIT_BYTES:
                    raise ValueError("session probe exceeded the process output limit")
                self.events.put(json.loads(line))
        except Exception as exc:
            self.events.put(exc)
        finally:
            self.events.put(EOFError("probe stdout closed"))

    def send(self, payload: dict):
        self.process.stdin.write((json.dumps(payload) + "\n").encode())
        self.process.stdin.flush()

    def receive(self) -> dict:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("session probe deadline expired")
        try:
            event = self.events.get(timeout=remaining)
        except queue.Empty as exc:
            raise TimeoutError("session probe timed out waiting for host") from exc
        if isinstance(event, Exception):
            raise event
        return event


class CodexSessionProbe(ProbeProcess):
    """One isolated app-server with two sequential turns."""

    def __init__(self, process: subprocess.Popen, *, timeout: float):
        super().__init__(process, timeout=timeout)
        self.completed: dict[tuple[str, str], dict] = {}
        self.messages: dict[tuple[str, str], list[dict]] = {}
        self.request_id = 0
        self.thread_id: str | None = None

    def receive(self) -> dict:
        event = super().receive()
        if "method" in event and "id" in event:
            # This text-only probe cannot approve or execute delegated tools.
            self.send({"id": event["id"], "error": {
                "code": -32601, "message": "Unsupported by session feasibility probe",
            }})
            raise RuntimeError(f"unexpected app-server request: {event['method']}")
        params = event.get("params") or {}
        if event.get("method") == "turn/completed":
            turn = params["turn"]
            self.completed[(params["threadId"], turn["id"])] = turn
        elif event.get("method") == "item/completed":
            key = (params["threadId"], params["turnId"])
            self.messages.setdefault(key, []).append(params["item"])
        return event

    def request(self, method: str, params: dict) -> dict:
        self.request_id += 1
        request_id = self.request_id
        self.send({"id": request_id, "method": method, "params": params})
        while True:
            event = self.receive()
            if event.get("id") != request_id:
                continue
            if "error" in event:
                raise RuntimeError(f"{method}: {event['error']}")
            return event["result"]

    def initialize(self):
        self.request("initialize", {
            "clientInfo": {"name": "goal_plus_session_probe", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        self.send({"method": "initialized", "params": {}})

    def run(self, prompt: str, session_id: str | None, workspace: Path) -> dict:
        if session_id is None:
            if self.thread_id is not None:
                raise ValueError("probe already owns a session")
            result = self.request("thread/start", {
                "model": st_model_for_host("codex"),
                "cwd": str(workspace),
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "ephemeral": True,
                "developerInstructions": "Answer the text task directly. Do not use tools.",
            })
            self.thread_id = result["thread"]["id"]
        elif session_id != self.thread_id:
            raise ValueError("session does not belong to this probe")
        turn = self.request("turn/start", {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": prompt}],
            "effort": os.environ.get("ST_CODEX_REASONING_EFFORT", "low"),
        })["turn"]
        key = (self.thread_id, turn["id"])
        # Notifications received before the turn/start response are retained.
        while key not in self.completed:
            self.receive()
        completed = self.completed.pop(key)
        if completed["status"] != "completed":
            raise RuntimeError(f"turn {turn['id']}: {completed}")
        items = self.messages.pop(key, [])
        return {
            "session_id": self.thread_id,
            "invocation_id": turn["id"],
            "status": completed["status"],
            "text": "\n".join(item["text"] for item in items if item["type"] == "agentMessage"),
        }


@pytest.mark.st
@pytest.mark.st_codex
def test_codex_session_two_turns_through_mcp(tmp_path: Path, st_log_dir: Path):
    timeout = float(os.environ.get("ST_SESSION_PROBE_TIMEOUT", "180"))
    marker = uuid4().hex
    stderr_path = st_log_dir / "app-server.stderr.log"
    print(f"session probe diagnostics: {stderr_path}")
    with stderr_path.open("wb") as stderr:
        process = subprocess.Popen(
            ["codex", "app-server", "--stdio"], cwd=tmp_path,
            env={**os.environ, ST_ACTIVE_ENV: "codex_session_mcp_probe"},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
            start_new_session=True,
        )
        managed = ManagedProcess(process)
        probe = None
        try:
            probe = CodexSessionProbe(process, timeout=timeout)
            probe.initialize()
            mcp = FastMCP("goal-plus-session-feasibility")

            @mcp.tool()
            def session_run(prompt: str, session_id: str | None = None) -> dict:
                return probe.run(prompt, session_id, tmp_path)

            async def exercise():
                async with Client(mcp, timeout=timeout) as client:
                    tools = await client.list_tools()
                    assert [tool.name for tool in tools] == ["session_run"]
                    assert tools[0].input_schema["required"] == ["prompt"]
                    first = await client.call_tool("session_run", {
                        "prompt": f"Remember marker {marker}. What is 17 + 25? Reply with the number only.",
                    })
                    first = first.data
                    assert first["text"].strip() == "42"
                    second = await client.call_tool("session_run", {
                        "session_id": first["session_id"],
                        "prompt": "Return the marker from my previous message and the value of 9 * 7.",
                    })
                    second = second.data
                    assert second["session_id"] == first["session_id"]
                    assert second["invocation_id"] != first["invocation_id"]
                    assert marker in second["text"] and "63" in second["text"]
                    return [first, second]

            results = asyncio.run(exercise())
        finally:
            managed.close()
            if probe is not None:
                probe.reader.join(timeout=managed.kill_timeout)
                assert not probe.reader.is_alive()
        assert process.returncode is not None
    (st_log_dir / "session-probe.json").write_text(json.dumps({
        "agent_harness": "codex", "mechanism": "isolated_app_server_thread",
        "model": st_model_for_host("codex"),
        "reasoning_effort": os.environ.get("ST_CODEX_REASONING_EFFORT", "low"),
        "results": results, "process_returncode": process.returncode,
        "native_collaboration_verified": False,
    }, indent=2) + "\n")


@pytest.mark.st
@pytest.mark.st_pi_rpc
def test_pi_extension_loads_and_calls_mcp_without_inference(tmp_path: Path, st_log_dir: Path):
    root = Path(__file__).resolve().parents[2]
    fixture = root / "tests/st/fixtures/mcp-bridge-probe.ts"
    stderr_path = st_log_dir / "pi.stderr.log"
    print(f"Pi MCP probe diagnostics: {stderr_path}")
    with stderr_path.open("wb") as stderr:
        process = subprocess.Popen([
            os.environ.get("ST_PI_BINARY", "pi"), "--mode", "rpc", "--no-session",
            "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-context-files",
            "--no-tools", "--extension", str(fixture),
        ], cwd=tmp_path, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
            start_new_session=True, env={
                **os.environ, ST_ACTIVE_ENV: "pi_mcp_bridge_probe",
                "GOAL_PLUS_PROBE_PYTHON": sys.executable,
                "PYTHONPATH": str(root / "src") + os.pathsep + str(root),
                "GOAL_PLUS_PROBE_ROOT": str(tmp_path / ".gp"),
            })
        managed = ManagedProcess(process)
        probe = None
        try:
            probe = ProbeProcess(process, timeout=60)
            probe.send({"id": "entries", "type": "get_entries"})
            while True:
                event = probe.receive()
                if event.get("type") == "response" and event.get("id") == "entries":
                    assert event["success"], event
                    entries = event["data"]["entries"]
                    break
            receipts = [entry["data"] for entry in entries
                        if entry.get("customType") == "goal-plus-mcp-probe"]
            assert len(receipts) == 1, entries
            receipt = receipts[0]
            assert receipt["server"]["name"] == "goal-plus"
            assert receipt["tool"]["name"] == "goal_plus_monitor_snapshot"
            assert receipt["tool"]["inputSchema"]["type"] == "object"
            assert receipt["result"]["isError"] is False
            assert receipt["result"]["content"]
        finally:
            managed.close()
            if probe is not None:
                probe.reader.join(timeout=managed.kill_timeout)
                assert not probe.reader.is_alive()
        assert process.returncode is not None
    (st_log_dir / "mcp-probe.json").write_text(json.dumps({
        **receipt, "process_returncode": process.returncode, "inference": False,
    }, indent=2) + "\n")
