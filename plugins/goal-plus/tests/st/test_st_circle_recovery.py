"""Real Pi workers on the Circle example; the test controller acts as Main."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from fastmcp import Client
import pytest

from goal_plus.goal_plus import FileGoalPlusRuntime
from goal_plus.host_scope import close_execution_scope, ensure_execution_scope, execution_scope_snapshot
from goal_plus.models import CandidateProposal, SearchSpec
from goal_plus.ports.scope import SCOPE_PROCESS_TERMINAL_STATUSES
from goal_plus.runtime import FileSearchRuntime
from goal_plus.server import create_host_mcp
from tests._runtime_helpers import start_host_verified_goal
from tests.st.hosts import st_model_for_host


@pytest.mark.st
@pytest.mark.st_pi_rpc
def test_circle_checkpoint_recovery_then_replacement(tmp_path, st_log_dir, isolated_pi_config, monkeypatch):
    checkout = Path(__file__).resolve().parents[2]
    example = checkout / "examples/circle-packing"
    source = tmp_path / "source"
    shutil.copytree(example / "workspace-template", source)
    for args in (("init", "-q"), ("add", "."), (
        "-c", "user.name=Goal Plus ST", "-c", "user.email=st@example.invalid",
        "commit", "-qm", "Circle recovery baseline",
    )):
        subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)
    baseline_hash = hashlib.sha256((source / "initial_program.py").read_bytes()).hexdigest()
    monkeypatch.setenv("GOAL_PLUS_PI_DEV_ROOT", str(checkout))
    monkeypatch.setenv("GOAL_PLUS_PYTHON", str(checkout / ".goal-plus-dev/venv/bin/python"))
    settings = json.loads((isolated_pi_config / "settings.json").read_text())
    model = st_model_for_host("pi") or settings["defaultProvider"] + "/" + settings["defaultModel"]
    timeout = float(os.environ.get("ST_SESSION_PROBE_TIMEOUT", "240"))
    root = tmp_path / ".gp"
    runtime = FileSearchRuntime(root)
    goal = start_host_verified_goal(root, source, agent_harness="pi")
    data = json.loads((example / "search-spec.json").read_text())
    data["source_path"] = str(source)
    data["budget"]["max_parallel"] = 1
    data["strategy"]["inner_agent"] = "common"
    frozen = runtime.freeze_spec(
        SearchSpec.model_validate(data), [source / ".goal-plus-verifiers/circle_packing_score.py"],
        goal_plus_id=goal.goal_plus_id,
    )
    run_id = runtime.create_run(frozen.frozen_spec_id)
    FileGoalPlusRuntime(root).link_search_run(goal.goal_plus_id, frozen.frozen_spec_id, run_id)
    plan = runtime.plan_next(run_id, requested_k=1)
    task, = runtime.start_batch(run_id, plan.plan_id, [CandidateProposal(
        intent="Verify Circle checkpoint recovery and session replacement with legal lattice packings",
    )])
    scope = ensure_execution_scope(
        root_dir=root, agent_harness="pi", owner_session_id=goal.active_session.session_id, run_id=run_id,
        activation_id=f"{goal.goal_plus_id}:{goal.control.version}",
        native_agent_config={"model_pattern": model, "thinking_level": "low"},
    )
    evidence = {
        "runtime_root": str(root), "run_id": run_id, "candidate_id": task.candidate_id,
        "model": model, "example": "examples/circle-packing", "calls": [], "checkpoints": [],
        "native_main_orchestration_verified": False, "verified": False,
    }
    hints = checkout / "examples/circle-packing-thinkthread-apply/candidate-hints"

    def record():
        # Reload from disk: the original failure only surfaced at deserialization.
        return FileSearchRuntime(root).candidate_creation.state.read_candidate(run_id, task.candidate_id)

    async def exercise():
        async with Client(create_host_mcp(root, agent_harness="pi", caller_role="main"), timeout=timeout + 60) as client:
            async def call(name, **args):
                result = await client.call_tool("goal_plus_" + name, args,
                    meta={"threadId": goal.active_session.session_id})
                return json.loads(result.content[0].text)

            async def launch(call_id, prompt, *, new_session=False):
                opened = await call("session_run", run_id=run_id, candidate_id=task.candidate_id,
                    call_id=call_id, prompt=prompt, timeout_seconds=timeout, new_session=new_session)
                evidence["calls"].append({"call_id": call_id, "opened": opened})
                return opened

            async def finish(opened, call_id):
                result = await call("session_wait_any", calls=[{
                    "session_id": opened["session_id"], "call_id": call_id,
                }], timeout_seconds=timeout + 30)
                assert len(result["finished"]) == 1, result
                finished = result["finished"][0]
                evidence["calls"][-1]["finished"] = finished
                return finished

            async def verify(call_id, prompt, *, new_session=False):
                before = record()
                opened = await launch(call_id, prompt + " Run goal_plus_search_run_verifier exactly once, "
                    "report the score and stop. Do not modify the verifier or runtime state.", new_session=new_session)
                finished = await finish(opened, call_id)
                assert finished["state"] == "completed", finished
                after = record()
                assert len(after.iterations) == len(before.iterations) + 1
                assert after.iterations[:-1] == before.iterations
                assert after.iterations[-1].agent_session_id == opened["session_id"]
                assert after.iterations[-1].process_passed
                assert "supplemental" not in after.iterations[-1].hypothesis.lower()
                evidence["checkpoints"].append({
                    "call_id": call_id, "generation": after.execution_generation,
                    "recovery": after.recovery.model_dump(mode="json") if after.recovery else None,
                    "iteration": after.iterations[-1].model_dump(mode="json"),
                })
                print(f"Circle {call_id}: generation={after.execution_generation}, score={after.iterations[-1].score}", flush=True)
                return opened

            active = await verify("initial", "Replace initial_program.py with this exact legal packing:\n" +
                (hints / "rectangular.py").read_text())
            original = record()
            assert original.score_report.aggregate_score == pytest.approx(2.166664066666667)
            for generation in (1, 2):
                before = record()
                settled_code = (task.workspace / "initial_program.py").read_text()
                marker = f"RECOVERY_PARTIAL_{generation}"
                call_id = f"partial-{generation}"
                active = await launch(call_id,
                    f"Append a comment '# {marker}' to initial_program.py. Do not verify or commit. "
                    f"Then create .tmp/{marker} containing 'ready' and run sleep 120. "
                    "The test controller will interrupt this unfinished attempt. Do not undo the comment.")
                async with asyncio.timeout(timeout):
                    while not (task.workspace / ".tmp" / marker).exists():
                        await asyncio.sleep(0.5)
                assert marker in (task.workspace / "initial_program.py").read_text()
                assert record().iterations == before.iterations
                # Fence first so a late return cannot supplement/settle the abandoned edit.
                prepared = await call("search_prepare_recovery", run_id=run_id, candidate_id=task.candidate_id)
                await call("session_interrupt", session_id=active["session_id"], call_id=call_id, timeout_seconds=30)
                finished = await finish(active, call_id)
                assert finished["state"] == "failed" and "StaleAgentSession" in finished["error"], finished
                await call("session_close", session_id=active["session_id"])
                await call("search_recover_candidate", run_id=run_id, candidate_id=task.candidate_id,
                    recovery_id=prepared["recovery_id"])
                restored = record()
                assert restored.execution_generation == restored.recovery.generation == generation
                assert restored.recovery.state == "restored"
                assert restored.iterations == before.iterations
                assert restored.results_ledger == before.results_ledger
                assert (task.workspace / "initial_program.py").read_text() == settled_code
                active = await verify(f"recovered-{generation}",
                    "Inspect the restored initial_program.py. Keep it unchanged and verify this checkpoint.")

            before_replacement = record()
            previous_id = active["session_id"]
            active = await verify("replacement", "Replace initial_program.py with this exact improved packing:\n" +
                (hints / "staggered.py").read_text(), new_session=True)
            final = record()
            assert active["session_id"] != previous_id
            assert final.execution_generation == 3
            assert final.recovery == before_replacement.recovery
            assert final.recovery.generation == 2
            assert final.iterations[:len(original.iterations)] == original.iterations
            assert final.results_ledger[:len(original.results_ledger)] == original.results_ledger
            assert final.score_report.aggregate_score == pytest.approx(2.4389636673796344)
            assert runtime.recovery.queries.has_fresh_evidence(run_id, final)
            with pytest.raises(RuntimeError, match="StaleAgentSession"):
                runtime.get_agent_context(previous_id)
            history = runtime.get_global_evidence(active["session_id"])
            assert history
            evidence["shared_evidence"] = history
            assert FileGoalPlusRuntime(root).reconciliation(goal.goal_plus_id)["runs"][0]["candidates"][0]["next_step"] == "continue_search"
            await call("session_close", session_id=active["session_id"])
            evidence["selection"] = await call("search_select", run_id=run_id)
            evidence["promotion"] = await call("search_promote", run_id=run_id, candidate_id=task.candidate_id)
            assert runtime.status(run_id).state == "promoted"
            assert hashlib.sha256((source / "initial_program.py").read_bytes()).hexdigest() == baseline_hash
            assert subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True) == ""
            (st_log_dir / "candidate.json").write_text(final.model_dump_json(indent=2) + "\n")
            evidence["verified"] = True

    print(f"Circle recovery evidence: {st_log_dir}; runtime: {root}", flush=True)
    try:
        asyncio.run(exercise())
    finally:
        evidence["scope_close"] = close_execution_scope(root_dir=root, scope_id=scope["scope_id"], reason="Circle recovery ST finished")
        snapshot = execution_scope_snapshot(root_dir=root, scope_id=scope["scope_id"])
        evidence["cleanup"] = snapshot["cleanup"]
        evidence["processes"] = [{key: item.get(key) for key in ("pid", "label", "kind", "status")}
                                 for item in snapshot["processes"]]
        (st_log_dir / "circle-recovery.json").write_text(json.dumps(evidence, indent=2) + "\n")
    assert evidence["scope_close"]["state"] == "closed"
    assert snapshot["processes"] and all(item["status"] in SCOPE_PROCESS_TERMINAL_STATUSES for item in snapshot["processes"])
    assert snapshot["cleanup"]["active_count"] == 0
    assert not snapshot["cleanup"]["errors"]
