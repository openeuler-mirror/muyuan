"""A short real Main conversation explicitly compacts without control skills."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import threading
import subprocess
import sys

import pytest

from goal_plus.execution.native_session import CodexSession, PiSession
from goal_plus.execution.session_rpc import SessionRpcConnection
from goal_plus.host_scope import close_execution_scope
from goal_plus.ports.process import ProcessRegistration
from goal_plus.ports.session import NativeSessionInput, SessionProcessInput
from tests._runtime_helpers import make_project
from tests.st.hosts import st_model_for_host


@pytest.mark.st
@pytest.mark.parametrize("agent_harness", [
    pytest.param("codex", marks=pytest.mark.st_codex),
    pytest.param("pi", marks=pytest.mark.st_pi_rpc),
])
def test_native_main_instructions_survive_compaction(
    tmp_path, st_log_dir, agent_harness, isolated_pi_config, monkeypatch, request,
):
    checkout = Path(__file__).resolve().parents[2]
    project = make_project(tmp_path)
    (project / "proof.txt").write_text("compaction-proof\n")
    timeout = float(os.environ.get("ST_COMPACTION_TIMEOUT", "240"))
    configuration = {"sandbox_mode": "workspace-write"}
    observer_log = st_log_dir / "instructions.jsonl"
    observer_log.unlink(missing_ok=True)
    if agent_harness == "codex":
        home = request.getfixturevalue("installed_codex_config")
        skills = [path for path in (home / "plugins/cache").rglob("SKILL.md")
                  if path.parent.name in {"goal-plus", "search", "goal-plus-with-final-check"}]
        assert not skills
        command = ("codex", "app-server", "--stdio")
    else:
        monkeypatch.setenv("GOAL_PLUS_DEBUG", "1")
        settings_path = isolated_pi_config / "settings.json"
        settings = json.loads(settings_path.read_text())
        # Keep only a short final exchange so explicit compaction has older input.
        settings["compaction"] = {"keepRecentTokens": 256}
        settings_path.write_text(json.dumps(settings))
        monkeypatch.setenv("GOAL_PLUS_PI_DEV_ROOT", str(checkout))
        monkeypatch.setenv("GOAL_PLUS_PYTHON", str(checkout / ".goal-plus-dev/venv/bin/python"))
        monkeypatch.setenv("GP_ST_OVERVIEW_FILE", str(checkout / "assets/goal-plus-instructions.txt"))
        monkeypatch.setenv("GP_ST_INSTRUCTIONS_LOG", str(observer_log))
        command = (
            "pi", "--approve", "--mode", "rpc", "--no-skills", "--no-context-files", "--no-extensions",
            "-e", str(checkout / "assets/pi/extensions/goal-plus.ts"),
            "-e", str(Path(__file__).parent / "fixtures/main-instructions-observer.ts"),
            "--session-dir", str(tmp_path / "sessions"),
        )
    events = []
    compacted = threading.Event()
    actor = CodexSession if agent_harness == "codex" else PiSession

    class ObservedSession(actor):
        compact_turn_id = None

        def _event(self, event):
            events.append(event)
            params = event.get("params") or {}
            if event.get("method") == "thread/compacted" or (
                event.get("method") == "item/completed"
                and (params.get("item") or {}).get("type") == "contextCompaction"
            ):
                self.compact_turn_id = params.get("turnId")
            if (
                event.get("method") == "turn/completed" and self.compact_turn_id is not None
                and (params.get("turn") or {}).get("id") == self.compact_turn_id
            ):
                compacted.set()
            super()._event(event)

    session = None
    diagnostics = []
    connection_init = SessionRpcConnection.__init__

    def observe_diagnostics(self, process, on_event, **kwargs):
        connection_init(self, process, on_event, on_diagnostic=diagnostics.append)

    monkeypatch.setattr(SessionRpcConnection, "__init__", observe_diagnostics)
    root = project / ".gp"
    print(f"Native {agent_harness} compaction evidence: {st_log_dir}", flush=True)
    try:
        session = ObservedSession(
            NativeSessionInput(
                agent_harness, SessionProcessInput(command, project, dict(os.environ)),
                model=st_model_for_host("codex" if agent_harness == "codex" else "pi"),
                reasoning_effort="low", configuration=configuration,
            ),
            ProcessRegistration(register=lambda pid: "compaction-main", finish=lambda token: None),
            timeout=timeout,
        )
        prefix = "$goal-plus" if agent_harness == "codex" else "/goal-plus"
        first = session.run(
            prefix + " Read proof.txt and verify its trimmed content is compaction-proof. "
            "Complete this ordinary read-only Goal without delegation or Search.", timeout=timeout,
        )
        assert first.state == "completed", first
        goals = list((root / "goal-plus").glob("gp_*/goal.json"))
        assert len(goals) == 1
        assert json.loads(goals[0].read_text())["status"] == "complete"
        if agent_harness == "codex":
            session._request("thread/compact/start", {"threadId": session.native_session_id}, timeout=timeout)
            assert compacted.wait(timeout), "native compaction never completed"
        else:
            compact = session._command("compact", timeout=timeout)
            assert compact["data"]["summary"]
        event_offset = len(events)
        second = session.run(
            "Use the available Goal Plus tool to read the status of the Goal just completed. "
            "Report its status without creating a new Goal or changing files.", timeout=timeout,
        )
        assert second.state == "completed", second
        assert "complete" in second.text.lower(), second.text
        assert "goal_plus_status" in json.dumps(events[event_offset:])
        if agent_harness == "codex":
            transcripts = list((home / "sessions").rglob(f"*{session.native_session_id}*.jsonl"))
            assert len(transcripts) == 1
        else:
            observations = [json.loads(line) for line in observer_log.read_text().splitlines()]
            assert len(observations) >= 2
            assert all(item["overview_present"] for item in observations)
            assert all("goal_plus_status" in item["tools"] for item in observations)
            transcripts = list((tmp_path / "sessions").glob(f"*{session.native_session_id}*.jsonl"))
            assert len(transcripts) == 1
        audit = subprocess.run([
            sys.executable, str(checkout / "scripts/check_session_instructions.py"), str(transcripts[0]),
            "--host", agent_harness, "--overview", str(checkout / "assets/goal-plus-instructions.txt"),
        ], capture_output=True, text=True, check=False)
        (st_log_dir / "instructions-audit.json").write_text(audit.stdout)
        assert audit.returncode == 0, audit.stdout + audit.stderr
        audit_report = json.loads(audit.stdout)
        assert audit_report["compactions"] >= 1
        assert any(item["compaction"] >= 1 for item in audit_report["checks"])
        assert json.loads(goals[0].read_text())["status"] == "complete"
        (st_log_dir / "results.json").write_text(json.dumps([asdict(first), asdict(second)], indent=2))
    finally:
        if session is not None:
            session.close()
            assert session.rpc.process.returncode is not None
        cleanup = [close_execution_scope(
            root_dir=root, scope_id=path.parent.name, reason="compaction ST finished",
        ) for path in (root / "host-scopes").glob("scope_*/scope.json")]
        (st_log_dir / "events.json").write_text(json.dumps(events, indent=2))
        (st_log_dir / "stderr.log").write_text("".join(diagnostics))
        (st_log_dir / "cleanup.json").write_text(json.dumps(cleanup, indent=2))
        assert all(item["state"] == "closed" for item in cleanup)
