"""Native Main consumes the shared MCP overview without GP control skills."""

import errno
import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

from goal_plus.candidate_host import candidate_host_queries
from goal_plus.domain.agents import WorkerBudget
from goal_plus.execution.managed_process import ManagedProcess
from goal_plus.host_scope import close_execution_scope
from goal_plus.runtime import FileSearchRuntime
from tests._runtime_helpers import make_project, spec_for
from tests.st.hosts import st_model_for_host


def assert_session_deliveries(sessions, evidence, *, circle, fork=False):
    expected_dispatch_counts = [1, 1] if fork else [1, 2]
    assert sorted(session.session_handle.metadata["dispatch_count"] for session in sessions) == expected_dispatch_counts
    deliveries = [
        [item for item in items if item["agent_session_id"] is not None]
        for items in evidence
    ]
    if circle:
        # A returned attempt can include a failed verifier followed by a repair.
        assert all(sum(item["process_passed"] for item in items) >= session.session_handle.metadata["dispatch_count"]
                   for session, items in zip(sessions, deliveries))
    else:
        assert sorted(len(items) for items in deliveries) == [1, 2]
    assert all(item["agent_session_id"] == session.agent_session_id
               for session, items in zip(sessions, deliveries) for item in items)


def assert_candidate_spec_revisions(runtime, run_id, sessions, st_log_dir):
    state = runtime.candidate_creation.state
    run = state.read_run(run_id)
    revised = state.read_candidate(run_id, "c001")
    peer = state.read_candidate(run_id, "c002")
    versions = [run.frozen_spec_id, *[item.frozen_spec_id for item in revised.spec_revisions]]
    assert len(versions) == len(set(versions)) == 3
    assert [item.parent_frozen_spec_id for item in revised.spec_revisions] == versions[:-1]
    frozen = [state.read_frozen_spec(version) for version in versions]
    assert [item.spec.metric_name for item in frozen] == ["combined_score", "revision_one", "revision_two"]
    assert [item.parent_frozen_spec_id for item in frozen[1:]] == versions[:-1]
    assert all(item.verifier_hashes == frozen[0].verifier_hashes for item in frozen)
    assert [item.spec.process_verifiers[0].command for item in frozen] == [
        ["python", "evaluator.py"],
        ["python", "evaluator.py", "--metric", "revision_one", "--scale", "10"],
        ["python", "evaluator.py", "--metric", "revision_two", "--scale", "100"],
    ]
    assert [item.frozen_spec_id for item in revised.iterations] == versions
    assert [item.score for item in revised.iterations] == [1.0, 20.0, 300.0]
    assert all(item.process_passed for item in revised.iterations)
    assert len(revised.results_ledger) == 3
    for iteration, ledger, contract in zip(revised.iterations, revised.results_ledger, frozen):
        assert iteration.settlement_receipt.frozen_spec_id == contract.frozen_spec_id
        assert ledger.frozen_spec_id == contract.frozen_spec_id
        assert ledger.spec_hash == contract.spec_hash
    assert revised.active_frozen_spec_id == versions[-1]
    assert not peer.spec_revisions and peer.active_frozen_spec_id in {None, versions[0]}
    assert len(peer.iterations) == 1 and peer.iterations[0].frozen_spec_id == versions[0]
    assert peer.iterations[0].score == 2.0
    assert {session.candidate_id: session.session_handle.metadata["dispatch_count"]
            for session in sessions} == {"c001": 3, "c002": 1}
    # Verify the actual peer-visible shared-plane response, persisted by the
    # production read operation, rather than reconstructing it after the run.
    trace = runtime.root_dir / "runs" / run_id / "candidate-traces" / "c002"
    observed = []
    for event_path in (trace / "events").glob("*.json"):
        event = json.loads(event_path.read_text())
        if event["kind"] == "evidence.global_returned" and event["payload"]["source"] == "explicit":
            snapshot = json.loads((trace / "snapshots" / f"{event['payload']['snapshot_id']}.json").read_text())
            entries = [entry for entry in snapshot["content"] if entry["candidate_id"] == "c001"]
            if len(entries) == 3:
                observed.append(entries)
    assert observed, "peer did not read all three versioned results through Global Evidence"
    entries = sorted(observed[-1], key=lambda entry: entry["iteration"])
    assert [entry["frozen_spec_id"] for entry in entries] == versions
    assert [entry["spec_hash"] for entry in entries] == [item.spec_hash for item in frozen]
    assert [entry["metric_name"] for entry in entries] == ["combined_score", "revision_one", "revision_two"]
    assert [entry["score"] for entry in entries] == [1.0, 20.0, 300.0]
    assert run.state == "promoted" and run.selected_candidate_id == "c001"
    assert run.selected_iteration == 3 and run.selected_frozen_spec_id == versions[-1]
    assert revised.promotion_evidence.passed
    assert revised.promotion_evidence.frozen_spec_id == versions[-1]
    assert revised.promotion_evidence.spec_hash == frozen[-1].spec_hash
    assert revised.promotion_evidence.selected_artifact_ref == run.selected_artifact_ref
    assert revised.promotion_report.aggregate_score == 300.0
    assert revised.promotion_report.frozen_spec_id == versions[-1]
    (st_log_dir / "spec-revisions.json").write_text(json.dumps({
        "run": run.model_dump(mode="json"),
        "contracts": [item.model_dump(mode="json") for item in frozen],
        "candidate": revised.model_dump(mode="json"),
        "peer": peer.model_dump(mode="json"), "peer_global_evidence": entries,
    }, indent=2))


@pytest.mark.st
@pytest.mark.parametrize("task_kind", [
    "ordinary", "search", "spec_revisions", "final_check", "delegation", "circle_artifact", "circle_apply", "circle_sandbox",
    "circle_fork", "circle_example_plain", "circle_example_adaptive", "circle_example_llm", "circle_example_replacement",
])
@pytest.mark.parametrize("agent_harness", [
    pytest.param("codex", marks=pytest.mark.st_codex),
    pytest.param("pi", marks=pytest.mark.st_pi_rpc),
])
def test_native_main_without_control_skills(
    tmp_path, st_log_dir, agent_harness, task_kind, isolated_pi_config, monkeypatch, request,
):
    checkout = Path(__file__).resolve().parents[2]
    circle = task_kind.startswith("circle_")
    fork = task_kind == "circle_fork"
    public_example = task_kind.startswith("circle_example_")
    sandbox_probe = task_kind == "circle_sandbox"
    if public_example and agent_harness != "pi":
        pytest.skip("Public Circle example acceptance is scoped to Pi")
    if public_example:
        monkeypatch.delenv("GOAL_PLUS_EVIDENCE_ANNOTATOR_DISABLED", raising=False)
        monkeypatch.delenv("GOAL_PLUS_LLM_VERIFIER_DISABLED", raising=False)
    apply_publication = public_example or task_kind in {"circle_apply", "circle_fork"}
    if circle:
        project = tmp_path / "circle"
        project.mkdir()
        example = checkout / "examples/circle-packing-git-apply"
        source = project / "workspace"
        subprocess.run([sys.executable, str(example / "prepare_workspace.py"), "--destination", str(source)],
                       check=True, capture_output=True, text=True)
        baseline_program = (source / "initial_program.py").read_bytes()
        verifier = source / ".goal-plus-verifiers/circle_packing_score.py"
        frozen_verifier = verifier.read_bytes()
        spec = json.loads((example / "search-spec.json").read_text())
        if not public_example:
            spec["source_path"] = str(source)
            spec["strategy"]["inner_agent"] = "common"
            # Search workers must use the same host-specific provider selected
            # for the Main ST process.  The example spec intentionally omits a
            # model, while the Pi/Codex hosts have different provider routes.
            model = st_model_for_host(agent_harness)
            if model:
                spec["strategy"]["worker_launch"] = {"model": model, "reasoning_effort": "low"}
                spec["strategy"]["models"] = [{"model": model, "reasoning_effort": "low", "count": 2}]
        if sandbox_probe:
            monkeypatch.setenv("GOAL_PLUS_DEBUG", "1")
            model = st_model_for_host(agent_harness)
            spec["strategy"]["worker_launch"] = {"model": model, "reasoning_effort": "low"}
            spec["strategy"]["models"] = [{"model": model, "reasoning_effort": "low", "count": 2}]
            # This is a bounded transport/write-boundary check, not an optimization run.
            spec["strategy"]["worker_budget"] = {"max_runtime_seconds": 180}
        spec["workspace"]["promotion_mode"] = "apply" if apply_publication else "artifact_only"
        if fork:
            spec["strategy"]["initial_context"] = "fork"
            spec["strategy"]["llm_validation"] = "required"
            spec["strategy"]["inner_agent"] = "common"
            spec["strategy"]["evidence_annotator"] = {"model": None, "reasoning_effort": "low", "timeout_seconds": 120}
        (project / "search-spec.json").write_text(json.dumps(spec, indent=2))
    else:
        project = make_project(tmp_path)
    (project / "proof.txt").write_text("unified-main-proof\n")
    root = project / ".gp"
    if sandbox_probe:
        outside = tmp_path / "outside.txt"
        outside.write_text("sandbox-outside-sentinel\n")
    timeout = float(os.environ.get("ST_UNIFIED_MAIN_TIMEOUT", "1200" if circle else
                                  "600" if task_kind in {"search", "spec_revisions", "delegation"} else "240"))
    task = (
        "Read proof.txt and verify its exact trimmed content is unified-main-proof. "
        "Complete this read-only task and report the result. Use ordinary Goal Mode, "
        "without Search or delegation."
    )
    goal_command = "goal-plus-with-final-check" if task_kind == "final_check" else "goal-plus"
    if task_kind == "delegation":
        task = (
            "Use ordinary Goal Mode and delegate one small implementation task: have a child write "
            "delegated.txt containing delegation-first followed by a newline. Review the result, then "
            "ask that same child to rework the same item so the file contains delegation-second followed "
            "by a newline. Verify the final file, accept the result, and complete this Goal. "
            "The child must perform both edits. Do not use Search."
            " Allow up to 180 seconds for each child turn; do not shorten this allowance."
        )
    if task_kind == "search":
        (project / "evaluator.py").write_text(
            "import json\nfrom pathlib import Path\nv = {}\n"
            "exec(Path('initial_program.py').read_text(), v)\n"
            "assert type(v['VALUE']) is int and 0 <= v['VALUE'] <= 3\n"
            "print(json.dumps({'combined_score': float(v['VALUE'])}))\n"
        )
        spec = spec_for(project, max_parallel=2)
        spec.objective = (
            "Verify bounded candidate delivery and same-session continuation: c001 first delivers "
            "VALUE=1, then VALUE=2 in the same session; c002 delivers VALUE=3. Each delivery "
            "makes one edit and records exact verifier evidence before returning."
        )
        spec.strategy.inner_agent = "common"
        spec.strategy.worker_budget = WorkerBudget(max_runtime_seconds=120)
        (project / "search-spec.json").write_text(spec.model_dump_json(indent=2))
        task = (
            "This acceptance task exercises the shared Goal Plus MCP interface. "
            "Use its Search tools and goal_plus_session_run with the related session controls for candidates. "
            "Run the bounded Search task in search-spec.json, using evaluator.py as the frozen verifier. "
            "Use exactly two independent candidates. Have one candidate first deliver VALUE=1, then "
            "reuse that same session for a second bounded attempt delivering VALUE=2. The other candidate "
            "should deliver VALUE=3. Each attempt makes one edit and verifies it before returning. "
            "After these three deliveries, close both sessions, review their Evidence, and promote "
            "the best verified artifact. Deliver artifact-only output, leave the source unchanged, "
            "complete this Goal and generate its report. This is a workflow check, so no further exploration is needed."
        )
    if task_kind == "spec_revisions":
        (project / "evaluator.py").write_text(
            "import argparse, json\nfrom pathlib import Path\n"
            "p = argparse.ArgumentParser()\np.add_argument('--metric', default='combined_score')\n"
            "p.add_argument('--scale', type=int, default=1)\na = p.parse_args()\nv = {}\n"
            "exec(Path('initial_program.py').read_text(), v)\n"
            "assert type(v['VALUE']) is int and 0 <= v['VALUE'] <= 3\n"
            "print(json.dumps({a.metric: float(v['VALUE'] * a.scale)}))\n"
        )
        frozen_verifier = (project / "evaluator.py").read_bytes()
        spec = spec_for(project, max_parallel=2)
        spec.objective = "Validate two candidate-owned evaluation revisions and versioned shared Evidence."
        spec.strategy.inner_agent = "common"
        spec.strategy.worker_budget = WorkerBudget(max_runtime_seconds=300)
        (project / "search-spec.json").write_text(spec.model_dump_json(indent=2))
        task = (
            "Use the shared Goal Plus Search and session tools to run search-spec.json unchanged, "
            "with exactly two candidates in fixed parallel mode. This bounded acceptance checks "
            "candidate-owned FrozenSpec revision, not Goal editing or creating successor runs. "
            "Assign c001 three separate bounded session_run invocations, reusing its same session. "
            "First: write VALUE=1 to initial_program.py, call the formal verifier once, then return. "
            "Second: ask c001 to call goal_plus_search_revise_spec using its current parent version "
            "and request_id=revision-one. Change metric_name to revision_one and process_verifiers "
            "to [{name:score, role:ranking_signal, command:[python,evaluator.py,--metric,revision_one,--scale,10], "
            "timeout_seconds:30}]. It must write VALUE=2, formally verify under this new contract, then return. "
            "Third: ask that same c001 to revise again with request_id=revision-two, parent=the first "
            "revision, metric_name=revision_two and the same verifier command with revision_two and "
            "scale 100. Write VALUE=3, formally verify once, then return its exact version ID. "
            "Each revision needs a meaningful reason. Preserve evaluator.py bytes, capabilities and "
            "the earlier Evidence. The child must perform both revision calls itself. "
            "Only after c001's three deliveries, run c002: require it to call "
            "goal_plus_search_get_global_evidence explicitly and inspect all three c001 results, "
            "including their distinct version IDs, hashes, metrics and scores 1,20,300. "
            "c002 must then write VALUE=2 and formally verify once under its unchanged original "
            "contract. Close both sessions, inspect all four results, select explicitly using c001's "
            "final frozen_spec_id. Then call goal_plus_search_run_verifier with scope=promotion "
            "for c001 and require aggregate_score=300 before promoting that exact artifact. Produce "
            "MD/HTML reports, and complete the Goal. Deliver artifact-only and leave the source "
            "unchanged. Allow 300 seconds per child invocation. No extra exploration is needed."
        )
    if circle:
        task = (
            "Complete the Circle Packing Search task described by search-spec.json. Maximize the valid "
            "sum of radii for 26 non-overlapping circles in the unit square. The frozen verifier is "
            "workspace/.goal-plus-verifiers/circle_packing_score.py. Use exactly two independent candidate "
            "lanes, each with a small implementation change and real verification. Reuse one of their "
            "sessions for a second bounded improvement based on the first result. Each turn should "
            "complete its bounded attempt and return. Review all three deliveries, select the best "
            "verified artifact, independently validate the publication and complete the Goal with MD/HTML "
            "reports. Keep the verifier unchanged. This bounded acceptance run needs no further exploration. "
            "Allow up to 300 seconds for each candidate session invocation; do not shorten this "
            "allowance, because a shorter per-invocation timeout turns slow provider turns into "
            "spurious recoveries. "
            + ("Apply the selected publication to workspace, verify the source with its verifier, and leave Git clean."
               if task_kind == "circle_apply" else
               "Deliver artifact-only output and keep the source workspace unchanged.")
        )
        if not fork and not sandbox_probe:
            task += (
                " Keep the spec's fixed parallel execution unchanged: do not freeze "
                "with initial_context fork and do not call "
                "goal_plus_search_fork; this task verifies the ordinary "
                "session-based candidate path."
                " This is a bounded workflow acceptance, so use these deterministic valid constructions "
                "instead of numerical optimization: candidate c001 first uses 25 circles at centers "
                "(0.1+0.2*i, 0.1+0.2*j) for i,j in range(5), all with radius 0.1, plus a zero-radius "
                "circle at (0,0); on its second bounded delivery, replace that zero-radius circle with "
                "one at (0.2,0.2) with radius 0.04. Candidate c002 independently uses the same 25-circle "
                "grid plus one circle at (0.2,0.2) with radius 0.1*(sqrt(2)-1)-1e-9. Keep exactly 26 "
                "circles, edit only initial_program.py, run the frozen verifier after each delivery, and "
                "return immediately after each bounded attempt. The Main must assign c001 and c002 these "
                "respective constructions and reuse c001's native session for its second delivery."
            )
    fork_prepare_task = None
    if fork:
        task = (
            "Run the Circle Packing Search task from search-spec.json using Fork Mode. "
            "Complete freeze, create, plan, and preflight first. Call goal_plus_search_start_batch "
            "once with exactly two candidate proposals, then immediately call "
            "goal_plus_search_fork(run_id) with its default latest_completed checkpoint. "
            "Do not call search_start_agent_session or redispatch for these candidates. "
            "Use the returned independent agent_session_id values with goal_plus_session_run; "
            "each branch must edit only workspace/initial_program.py, run the frozen Circle verifier, "
            "and return after one valid attempt. Close both sessions, wait until the required LLM audit "
            "records are complete, then select, promote, apply, verify the source, generate MD/HTML reports, "
            "and complete the Goal. Keep the frozen verifier unchanged and leave Git clean."
        )
        fork_prepare_task = (
            "Your overall Goal is the following full Circle Packing Search task:\n\n"
            + task
            + "\n\nThis is the preparation stage of that same Goal. In this turn, perform only the "
            "freeze, create, plan, preflight, and one goal_plus_search_start_batch call with "
            "exactly two proposals. Do not call goal_plus_search_fork or any candidate "
            "session tool in this turn. After the two-candidate batch is successfully created, "
            "call goal_plus_set_status with status=blocked, reason=\"fork checkpoint ready\" "
            "and evidence naming run_0001 and both candidates, then end the turn and report ready "
            "so the host can resume this same Main session at a completed-turn checkpoint. The "
            "full Goal above must remain the persisted objective for the resumed session. Do not "
            "attempt any later work after setting blocked."
        )
    public_prompt = None
    if sandbox_probe:
        task += (
            " Use the shared Goal Plus MCP tools for freeze/create/plan/start_batch and "
            "goal_plus_session_run for candidate execution. Ordinary native subagents do not "
            "exercise this Search boundary. Preserve the worker_launch and models in search-spec.json; "
            "they select this environment's available model. This is a bounded workflow acceptance, "
            "so use these reproducible valid constructions: c001 first uses a 5x5 grid with centers "
            "(0.1+0.2*i, 0.1+0.2*j), radii 0.1, plus a zero-radius circle at (0,0); on its second "
            "turn replace the zero circle by one at (0.2,0.2), radius 0.04. c002 independently uses "
            "the same 5x5 grid plus a circle at (0.2,0.2), radius 0.1*(sqrt(2)-1)-1e-9. "
            "Return these exact 26 centers/radii directly; no numeric optimization is needed. "
            "c001's initial plan intent and first session_run message must contain ONLY its first "
            "construction. Wait for that invocation to return, then invoke goal_plus_session_run "
            "again on the same agent_session_id with the second construction. Two verifier calls "
            "inside a single invocation do not satisfy this same-session continuation check. "
            "For EACH candidate, put this bounded acceptance contract in BOTH its plan intent and "
            "session_run message: implement only the specified exact construction, run the sandbox "
            "probe and real verifier, then immediately return. Do not perform numerical optimization "
            "or chase reference target scores. Preserve the 180-second worker budget in the spec. "
            " Before each candidate's first implementation change, have that candidate run "
            f"python {checkout / 'tests/st/fixtures/sandbox_probe.py'} "
            f"--outside {outside} --source {source / 'initial_program.py'}. "
            "This authorized sandbox acceptance probe must run inside each candidate's native shell, "
            "not Main. It attempts writes only to disposable test files and saves .tmp/sandbox-probe.json. "
            "Do not synthesize or edit that receipt. A kernel write denial is the expected result; "
            "continue the Circle implementation and real verifier after the probe succeeds."
        )
    if public_example:
        suffix = {"plain": "", "adaptive": "-adaptive-search", "llm": "-llm-verifier", "replacement": "-adaptive-search"}[task_kind.removeprefix("circle_example_")]
        public_prompt = (example / f"pi-goal-prompt{suffix}.md").read_text()
        if task_kind == "circle_example_replacement":
            public_prompt = public_prompt.replace("max_duration_seconds=300", "max_duration_seconds=900")
            public_prompt = public_prompt.replace("任务预算为 5 分钟，第 4 分钟开始收尾", "任务预算为 15 分钟，第 12 分钟开始收尾")
            public_prompt += (
                "\n本次是独立的有界替换验收，冻结前覆盖上述配置：Goal 预算 900 秒；"
                "max_parallel=2、max_candidates=3、allocation.policy=swe_search_uct/v1、"
                "verifier.scores=[progress_reward, allocation_utility]。"
                "等待两个初始 lane 各完成一次真实评分后，应用未完成 Decision，"
                "关闭退休 session，通过 goal_plus_session_run 启动派生 candidate 并取得真实 Evidence，"
                "随后完成选择、promotion、apply、报告与资源收尾。不要在运行中修改 FrozenSpec。\n"
            )
        (st_log_dir / "prompt.md").write_text(public_prompt)
    if agent_harness == "codex":
        home = request.getfixturevalue("installed_codex_config")
        skills = [path for path in (home / "plugins/cache").rglob("SKILL.md")
                  if path.parent.name in {"goal-plus", "search", "goal-plus-with-final-check"}]
        assert not skills
        command = [
            "codex", "exec", "--skip-git-repo-check", "--json",
            "--sandbox", "workspace-write", "-c", "approval_policy=\"never\"",
            "-c", f"model_reasoning_effort=\"{os.environ.get('ST_CODEX_REASONING', 'low')}\"",
            "-m", st_model_for_host("codex"), "$" + goal_command + " " + task,
        ]
    else:
        package = Path(os.environ.get("ST_PI_PACKAGE", str(checkout)))
        if package == checkout:
            monkeypatch.setenv("GOAL_PLUS_PI_DEV_ROOT", str(checkout))
            monkeypatch.setenv("GOAL_PLUS_PYTHON", os.environ.get(
                "GOAL_PLUS_PYTHON", str(checkout / ".goal-plus-dev/venv/bin/python")))
        else:
            monkeypatch.delenv("GOAL_PLUS_PI_DEV_ROOT", raising=False)
            monkeypatch.delenv("GOAL_PLUS_PYTHON", raising=False)
        model = st_model_for_host("pi")
        binary = os.environ.get("ST_PI_BINARY") or (
            str(checkout / ".goal-plus-dev/bin/pi-goal-plus") if public_example else "pi")
        command = [
            binary, "--approve", "-p", "--mode", "json", "--no-skills",
            "--no-context-files",
            "--session-dir", str(tmp_path / "sessions"), "--thinking", "low",
        ]
        if not public_example and not os.environ.get("ST_PI_BINARY"):
            command.extend(["--no-extensions", "-e", str(package / "assets/pi/extensions/goal-plus.ts")])
        if model:
            command.extend(["--model", model])
        command.append(public_prompt if public_example else "/" + goal_command + " " + task)
    log = st_log_dir / "native-main.log"
    (st_log_dir / "build.json").write_text(json.dumps({
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip(),
        "source_diff_sha256": hashlib.sha256(subprocess.check_output(
            ["git", "diff", "HEAD", "--", "src", "assets", "scripts"], cwd=checkout,
        )).hexdigest(),
        "platform": platform.platform(),
        "checkout": str(checkout), "project": str(project), "agent_harness": agent_harness,
        "requested_model": st_model_for_host(agent_harness), "command": command,
    }, indent=2))
    print(f"Native {agent_harness} Main without control skills: {log}", flush=True)
    completed = False
    cleanup: list[dict[str, object]] = []
    with log.open("w") as output:
        if fork:
            assert fork_prepare_task is not None
            prepare_prefix = "$goal-plus" if agent_harness == "codex" else "/goal-plus"
            prepare_command = [*command[:-1], prepare_prefix + " " + fork_prepare_task]
            prepare_log = st_log_dir / "native-main-prepare.log"
            with prepare_log.open("w") as prepare_output:
                prepare_process = subprocess.Popen(
                    prepare_command, cwd=project, env=dict(os.environ), stdout=prepare_output,
                    stderr=subprocess.STDOUT, text=True, start_new_session=True,
                )
                prepare_managed = ManagedProcess(prepare_process)
                try:
                    assert prepare_process.wait(timeout=timeout) == 0, prepare_log
                finally:
                    prepare_managed.close()
            prepared_goals = list((root / "goal-plus").glob("gp_*/goal.json"))
            assert len(prepared_goals) == 1, prepare_log
            prepared_record = json.loads(prepared_goals[0].read_text())
            main_session_id = prepared_record["active_session"]["session_id"]
            if agent_harness == "codex":
                resume_options: list[str] = []
                skip_next = False
                for option in command[2:-1]:
                    if skip_next:
                        skip_next = False
                        continue
                    if option == "--sandbox":
                        skip_next = True
                        continue
                    resume_options.append(option)
                command = [
                    command[0], command[1], "resume", *resume_options, main_session_id,
                    "$goal-plus resume " + task,
                ]
            else:
                command = list(command)
                # The preparation turn intentionally persisted a blocked
                # checkpoint.  Resume with an explicit post-checkpoint task;
                # passing the preparation instructions again makes Pi repeat
                # start_batch/set_status instead of forking the candidates.
                resume_task = task
                if fork:
                    resume_task = (
                        "The preparation stage is complete: run_0001 already has exactly two candidates "
                        "and the Goal is paused at the completed-turn checkpoint. Resume this same Main "
                        "session by immediately calling goal_plus_search_fork(run_id=run_0001) with "
                        "the default latest_completed checkpoint. Do not call freeze, create, plan, "
                        "start_batch, goal_plus_set_status, or any search_start_agent_session tool again. "
                        "Then continue the Fork task: use the returned branch sessions for one valid "
                        "Circle attempt each, close both sessions, wait for required LLM audits, select, "
                        "promote, apply, verify the source, write reports, and complete the Goal."
                    )
                command[-1] = "/goal-plus resume " + resume_task
                command.insert(command.index("-p"), "--session")
                command.insert(command.index("--session") + 1, main_session_id)
        process = subprocess.Popen(
            command, cwd=project, env=dict(os.environ), stdout=output,
            stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )
        managed = ManagedProcess(process)
        try:
            assert process.wait(timeout=timeout) == 0, log
            completed = True
        finally:
            managed.close()
            cleanup = [close_execution_scope(
                root_dir=root, scope_id=path.parent.name, reason="Main ST finished",
            ) for path in (root / "host-scopes").glob("scope_*/scope.json")]
            (st_log_dir / "cleanup.json").write_text(json.dumps({
                "natural_exit": completed, "scopes": cleanup,
            }, indent=2))
    goals = list((root / "goal-plus").glob("gp_*/goal.json"))
    assert len(goals) == 1, log
    record = json.loads(goals[0].read_text())
    assert record["entrypoint_status"] == "host_verified", log
    assert record["status"] == "complete", log
    if sandbox_probe and agent_harness == "pi":
        # Host lifecycle callbacks must export without a wrapper or manual CLI.
        export_dir = root / "host-logs/agent-transcript"
        assert (export_dir / "index.html").is_file(), log
        transcript_events = [json.loads(line) for line in (export_dir / "events.jsonl").read_text().splitlines()]
        assert any(event["role"] == "main" for event in transcript_events), log
        assert len({event["session_id"] for event in transcript_events if event["role"] == "candidate"}) == 2, log
        for report in root.glob("runs/*/report.html"):
            assert "../../host-logs/agent-transcript/index.html" in report.read_text(), log
    if task_kind == "delegation":
        assert len(record["work_items"]) == 1, log
        item = record["work_items"][0]
        assert item["status"] == "accepted" and item["generation"] == 2, log
        assert item["metadata"]["transport"] == ("app-server" if agent_harness == "codex" else "native-rpc"), log
        assert item["agent_id"] == item["metadata"]["native_invocation"]["native_session_id"], log
        events = [json.loads(line) for line in (goals[0].parent / "events.jsonl").read_text().splitlines()]
        work_events = [event["payload"] for event in events if event["event_type"] == "execution_work_event"]
        deliveries = [event for event in work_events if event["event"] == "result"]
        assert len(deliveries) == 2, log
        assert {event["agent_id"] for event in deliveries} == {item["agent_id"]}, log
        assert len([event for event in work_events if event["event"] == "rework"]) == 1, log
        assert (project / "delegated.txt").read_text() == "delegation-second\n", log
        assert not record["search_tasks"], log
    else:
        assert not record["work_items"], log
    if task_kind in {"ordinary", "final_check"}:
        assert record["phase"] == "goal", log
        assert record["triage"] is None, log
        assert not record["search_tasks"], log
        if task_kind == "final_check":
            assert record["policy"]["final_check"]["mode"] == "required", log
            check = record["final_checks"][-1]
            assert check["status"] == "passed" and check["evidence"], log
            assert check["checker_metadata"]["native_session_id"], log
            assert check["checker_metadata"]["scope_id"], log
    elif task_kind in {"search", "spec_revisions"} or circle:
        assert len(record["search_tasks"]) == 1, log
        linked = record["search_tasks"][0]
        assert linked["result_recorded_at"] and linked["report_path"], log
        runtime = FileSearchRuntime(root)
        prepared_sessions = candidate_host_queries(root).sessions(linked["run_id"])
        launched_ids = {
            process["label"].split(":", 1)[1]
            for scope in cleanup for process in scope.get("processes", [])
            if process.get("run_id") == linked["run_id"]
            and process.get("label", "").startswith(("native-session:", "pi-rpc:"))
        }
        sessions = [session for session in prepared_sessions if session.agent_session_id in launched_ids]
        assert {session.agent_session_id for session in sessions} == launched_ids, log
        assert len(sessions) >= 2 if public_example else len(sessions) == 2, log
        assert all(session.session_handle.metadata.get("transport") == (
            "app-server" if agent_harness == "codex" else "native-rpc"
        ) for session in sessions), log
        evidence = [runtime.list_iterations(session.run_id, session.candidate_id) for session in sessions]
        if public_example:
            assert all(items and any(item["process_passed"] for item in items) for items in evidence), log
            frozen = runtime.search_run.state.read_frozen_spec(linked["frozen_spec_id"])
            records = runtime.list_verifier_records(linked["run_id"])
            decisions = runtime.list_allocation_decisions(linked["run_id"]) if frozen.spec.strategy.allocation else []
            if task_kind == "circle_example_plain":
                assert frozen.spec.strategy.initial_context == "fresh"
                assert not records and not decisions
                assert frozen.spec.strategy.selection.ranking_keys == ["hard_score"]
            else:
                assert records and "quality_score" in frozen.spec.strategy.selection.ranking_keys
                assert all(item.invocation.agent_harness == "pi" for item in records)
                if task_kind == "circle_example_llm":
                    assert all(item.scores is None for item in records)
                    assert not decisions
                else:
                    assert all(item.scores is not None for item in records)
                    if task_kind == "circle_example_replacement":
                        assert any(item.status == "applied" for item in decisions)
                    for decision in decisions:
                        if decision.status == "applied":
                            for expansion in decision.expansions:
                                child = next(item for item in sessions if item.candidate_id == expansion.new_candidate_id)
                                assert runtime.list_iterations(child.run_id, child.candidate_id)
            (st_log_dir / "search-evidence.json").write_text(json.dumps({
                "run_id": linked["run_id"], "frozen_spec_id": frozen.frozen_spec_id,
                "sessions": [item.model_dump(mode="json") for item in sessions],
                "prepared_sessions": [item.model_dump(mode="json") for item in prepared_sessions],
                "verifier_records": [item.model_dump(mode="json") for item in records],
                "decisions": [item.model_dump(mode="json") for item in decisions],
            }, indent=2))
        elif task_kind == "spec_revisions":
            assert_candidate_spec_revisions(runtime, linked["run_id"], sessions, st_log_dir)
            assert (project / "evaluator.py").read_bytes() == frozen_verifier
        else:
            assert_session_deliveries(sessions, evidence, circle=circle, fork=fork)
        if sandbox_probe:
            receipts = []
            for session in sessions:
                receipt = json.loads((session.workspace / ".tmp/sandbox-probe.json").read_text())
                assert receipt["workspace"] == str(session.workspace)
                assert receipt["inside_write"] == "ok"
                assert receipt["temporary_write"] == "ok"
                if agent_harness == "pi":
                    assert not Path(receipt["temporary_directory"]).exists(), "closed session scratch leaked"
                assert set(receipt["denied"]) == {"outside", "source", "symlink", "child"}
                assert all(code in {errno.EROFS, errno.EACCES, errno.EPERM}
                           for code in receipt["denied"].values()), receipt
                receipts.append(receipt)
            assert outside.read_text() == "sandbox-outside-sentinel\n"
            (st_log_dir / "sandbox.json").write_text(json.dumps(receipts, indent=2))
        if circle:
            assert runtime.status(linked["run_id"]).state == "promoted"
            run = runtime.search_run.state.read_run(linked["run_id"])
            if fork:
                assert run.fork is not None
                assert all(
                    branch.audit_state == "completed" and branch.audit_record_id
                    for branch in run.fork.branches
                )
            selected = runtime.candidate_creation.state.read_candidate(run.run_id, run.selected_candidate_id)
            assert selected.promotion_evidence is not None and selected.promotion_evidence.passed
            assert selected.promotion_evidence.selected_artifact_ref == run.selected_artifact_ref
            assert run.selection_trace[-1]["winner_subject"]["candidate_id"] == run.selected_candidate_id
            assert run.selection_trace[-1]["winner_subject"]["iteration"] == run.selected_iteration
            (st_log_dir / "selection.json").write_text(run.model_dump_json(indent=2))
            (st_log_dir / "promotion-evidence.json").write_text(selected.promotion_evidence.model_dump_json(indent=2))
            assert Path(linked["report_path"]).is_file() and Path(linked["html_report_path"]).is_file()
            assert verifier.read_bytes() == frozen_verifier
            scored = subprocess.run([sys.executable, str(verifier)], cwd=source,
                                    capture_output=True, text=True, check=True)
            score = json.loads(scored.stdout)
            assert score["valid"] and score["sum_radii"] > 0
            assert score["sum_radii"] / score["avg_radius"] == pytest.approx(26)
            clean = subprocess.run(["git", "status", "--porcelain"], cwd=source,
                                   capture_output=True, text=True, check=True)
            assert not clean.stdout
            publication = runtime.promotion_record(linked["run_id"]).model_dump(mode="json")
            assert publication["selected_artifact_ref"] == run.selected_artifact_ref.model_dump(mode="json")
            if apply_publication:
                assert publication["state"] == "applied"
            else:
                assert (source / "initial_program.py").read_bytes() == baseline_program
            (st_log_dir / "publication.json").write_text(json.dumps(publication, indent=2))
            (st_log_dir / "source-verifier.json").write_text(scored.stdout)
        else:
            assert (project / "initial_program.py").read_text() == "VALUE = 0\n"
    assert (project / "proof.txt").read_text() == "unified-main-proof\n"
    assert all(scope["state"] == "closed" for scope in cleanup)
    (st_log_dir / "goal.json").write_text(json.dumps(record, indent=2))
