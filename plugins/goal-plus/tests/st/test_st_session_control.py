"""Native Pi extension loading and MCP wiring without model inference."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from goal_plus.execution.managed_process import ManagedProcess
from goal_plus.execution.process_output import PROCESS_OUTPUT_LIMIT_BYTES
from tests.st.hosts import ST_ACTIVE_ENV


class ProbeProcess:
    """Bounded JSON-line observation for the disposable Pi loader probe."""

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
