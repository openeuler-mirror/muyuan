from __future__ import annotations

import calendar
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from goal_plus.domain.goal_commands import (EXPLORATION_MODE_LINES, GOAL_PLUS_COMMAND_CONFIG_KEYS, GOAL_PLUS_COMMAND_OPTIONS)
from goal_plus.goal_plus import (FileGoalPlusRuntime)
from goal_plus.models import (
    GoalPlusCommandConfig,
    GoalPlusHostCommandInvocation,
    GoalPlusRecord,
    GoalPlusSpecDraft,
    GoalPlusTriage,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("paused", [False, True])
def test_gate_does_not_apply_goal_policy_to_foreign_tool_names(tmp_path, paused):
    runtime = FileGoalPlusRuntime(tmp_path / ".gp")
    record = runtime.start_from_host_command(
        "Identity fixture", invocation=_codex_invocation("identity-start"),
    )
    runtime.activate_session(record.goal_plus_id, {"agent_harness": "codex", "session_id": "session-test"})
    if paused:
        runtime.control_from_host_command(record.goal_plus_id, invocation=_codex_invocation(
            "identity-pause", action="pause",
        ))
    for action in ("goal_plus_search_create", "goal_plus_search_prepare_recovery", "goal_plus_set_status", "bash"):
        gate = runtime.gate(record.goal_plus_id, event="pre_tool_use", context={
            "tool_name": f"mcp__foreign__{action}", "input": {"run_id": "run_external"},
        })
        assert gate.decision == "allow", gate.reason
    owned = runtime.gate(record.goal_plus_id, event="pre_tool_use", context={
        "tool_name": "mcp__goal_plus__goal_plus_search_create", "input": {"goal_plus_id": record.goal_plus_id},
    })
    assert owned.decision == ("block" if paused else "allow")


def test_checker_task_name_does_not_bypass_goal_gate(tmp_path):
    runtime = FileGoalPlusRuntime(tmp_path / ".gp")
    record = runtime.create_goal(
        "Complete the independent review before editing",
        policy={"final_check": {"mode": "required"}},
    )
    runtime.prepare_final_check(record.goal_plus_id, "codex")
    result = runtime.gate(record.goal_plus_id, event="pre_tool_use", context={
        "tool_name": "write", "task_name": "goal_plus_final_check_external",
    })
    assert result.decision == "block"


def _codex_invocation(
    invocation_id: str,
    *,
    action: str = "start",
    raw_input: str = "$goal-plus Test",
    session_id: str = "session-test",
) -> GoalPlusHostCommandInvocation:
    return GoalPlusHostCommandInvocation(
        agent_harness="codex",
        transport="codex_user_prompt_submit",
        command="$goal-plus",
        action=action,
        session_id=session_id,
        native_entry_id=f"turn-{invocation_id}",
        invocation_id=invocation_id,
        raw_input_sha256=hashlib.sha256(raw_input.encode()).hexdigest(),
        invoked_at="2026-08-28T00:00:00Z",
    )


def _write_search_run(root, run_id: str, frozen_spec_id: str) -> None:
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(
        json.dumps({"run_id": run_id, "frozen_spec_id": frozen_spec_id}),
        encoding="utf-8",
    )


def _complete_search_spec() -> dict:
    return {
        "objective": "minimize latency",
        "metric_name": "avg_latency_ms",
        "metric_direction": "minimize",
        "source_path": ".",
        "edit_surface": {"allow": ["kernel.py"], "deny": ["verify.py"]},
        "budget": {"max_parallel": 1},
        "process_verifiers": [
            {
                "name": "latency",
                "role": "ranking_signal",
                "command": ["python", "verify.py"],
            }
        ],
    }


def _goal_domain_state(record: GoalPlusRecord) -> dict:
    return record.model_dump(
        mode="json",
        exclude={"hook_counters", "created_at", "updated_at"},
    )


def test_goal_plus_runtime_defaults_to_gp_root(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    runtime = FileGoalPlusRuntime()

    assert runtime.root_dir == tmp_path / ".gp"
    assert runtime.goals_dir == tmp_path / ".gp" / "goal-plus"
    assert not runtime.root_dir.exists()


@pytest.mark.parametrize("same_invocation", [True, False])
def test_concurrent_start_keeps_replay_conflict_and_creation_in_one_lock(tmp_path, same_invocation):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    requests = 8
    barrier = Barrier(requests)
    root = tmp_path / ".gp"

    def start(index):
        runtime = FileGoalPlusRuntime(root)
        command = _codex_invocation(f"start-{0 if same_invocation else index}")
        barrier.wait(timeout=10)
        try:
            return runtime.start_from_host_command("Concurrent start", invocation=command)
        except RuntimeError as exc:
            assert "native host session already owns" in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=requests) as executor:
        results = list(executor.map(start, range(requests)))
    successful = [result for result in results if result is not None]
    assert len(successful) == (requests if same_invocation else 1)
    assert {result.goal_plus_id for result in successful} == {"gp_0001"}
    records = list((root / "goal-plus").glob("*/goal.json"))
    assert len(records) == 1
    events = FileGoalPlusRuntime(root).list_events("gp_0001")
    assert len([event for event in events if event["event_type"] == "created"]) == 1


def test_create_goal_plus_record_writes_state_and_event(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")

    record = runtime.create_goal(
        raw_goal="Improve the README examples",
        source_path=".",
    )

    assert record.goal_plus_id == "gp_0001"
    assert record.raw_goal == (
        "Improve the README examples\n\n"
        + EXPLORATION_MODE_LINES["autonomous"]
    )
    assert record.command_config.mode is None
    assert record.status == "active"
    assert record.phase == "goal"
    assert record.triage is None
    assert not hasattr(record, "mode_hint")
    assert record.next_action.kind == "execute_goal"  # type: ignore[union-attr]
    assert record.entrypoint_status == "legacy_unverified"

    loaded = runtime.status(record.goal_plus_id)
    assert loaded.goal_plus_id == record.goal_plus_id
    assert runtime.list_events(record.goal_plus_id)[0]["event_type"] == "created"


def test_host_command_start_is_verified_and_idempotent(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    invocation = _codex_invocation("codex:test:start")

    first = runtime.start_from_host_command(
        "Improve the README examples",
        invocation=invocation,
        source_path=".",
    )
    second = runtime.start_from_host_command(
        "Improve the README examples",
        invocation=invocation,
        source_path=".",
    )

    assert first.goal_plus_id == second.goal_plus_id
    assert first.entrypoint_status == "host_verified"
    assert [item.invocation_id for item in first.host_command_invocations] == [
        "codex:test:start"
    ]
    assert len(list((runtime.goals_dir).glob("gp_*/goal.json"))) == 1
    assert runtime.require_host_verified_active_goal().goal_plus_id == first.goal_plus_id


def test_host_command_start_scopes_nonterminal_goal_to_native_session(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    first = runtime.start_from_host_command(
        "Improve the README examples",
        invocation=_codex_invocation(
            "codex:test:first", session_id="session-first"
        ),
    )
    second = runtime.start_from_host_command(
        "Start a different task",
        invocation=_codex_invocation(
            "codex:test:second", session_id="session-second"
        ),
    )

    with pytest.raises(RuntimeError, match="native host session already owns"):
        runtime.start_from_host_command(
            "Replace the first task",
            invocation=_codex_invocation(
                "codex:test:third", session_id="session-first"
            ),
        )

    assert sorted(
        path.parent.name for path in runtime.goals_dir.glob("gp_*/goal.json")
    ) == [first.goal_plus_id, second.goal_plus_id]
    assert runtime.require_active_tool_target(
        "goal_plus_record_triage",
        {"goal_plus_id": second.goal_plus_id},
    ).goal_plus_id == second.goal_plus_id


def test_legacy_unverified_goal_cannot_authorize_search_bootstrap(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    runtime.create_goal("Legacy test fixture")

    with pytest.raises(RuntimeError, match="GoalPlusEntrypointRequired"):
        runtime.require_host_verified_active_goal()


def test_host_command_invocation_rejects_wrong_host_prefix() -> None:
    with pytest.raises(ValidationError, match="requires the \\$ command prefix"):
        GoalPlusHostCommandInvocation(
            agent_harness="codex",
            transport="codex_user_prompt_submit",
            command="/goal-plus",
            action="start",
            session_id="session-test",
            invocation_id="codex:test:wrong-prefix",
            raw_input_sha256="0" * 64,
            invoked_at="2026-08-28T00:00:00Z",
        )


def test_native_session_continuation_state_machine(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    record = runtime.start_from_host_command(
        "Improve the README examples",
        invocation=_codex_invocation("codex:test:session-state"),
    )
    runtime.activate_session(
        record.goal_plus_id,
        {"agent_harness": "codex", "session_id": "session-test"},
    )

    codex_start = runtime.handle_host_session_event(
        record.goal_plus_id,
        agent_harness="codex",
        session_id="session-test",
        event="session_start",
        delivery="next_user_turn",
        interaction="interactive",
    )
    assert codex_start.decision == "wait_for_user"
    assert codex_start.reason == "host_requires_user_turn"

    interrupted = runtime.handle_host_session_event(
        record.goal_plus_id,
        agent_harness="codex",
        session_id="session-test",
        event="interrupt",
        delivery="next_user_turn",
        interaction="interactive",
    )
    assert interrupted.session_state == "attached"
    assert interrupted.decision == "stop"
    assert runtime.status(record.goal_plus_id).control.state == "paused"

    resumed_by_user = runtime.handle_host_session_event(
        record.goal_plus_id,
        agent_harness="codex",
        session_id="session-test",
        event="user_turn",
        delivery="next_user_turn",
        interaction="interactive",
    )
    assert resumed_by_user.decision == "wait_for_user"
    assert resumed_by_user.session_state == "attached"
    assert runtime.status(record.goal_plus_id).active_session.state == "attached"

    pi_auto = runtime.handle_host_session_event(
        record.goal_plus_id,
        agent_harness="codex",
        session_id="session-test",
        event="session_start",
        delivery="auto",
        interaction="headless",
    )
    assert pi_auto.decision == "wait_for_user"

    mismatch = runtime.handle_host_session_event(
        record.goal_plus_id,
        agent_harness="codex",
        session_id="session-other",
        event="session_start",
        delivery="auto",
        interaction="headless",
    )
    assert mismatch.decision == "stop"
    assert mismatch.reason == "session_mismatch"


@pytest.mark.parametrize("agent_harness", ["codex", "pi"])
@pytest.mark.parametrize(
    "state,status,event,interaction,decision",
    [
        ("attached", "active", "session_start", "interactive", "wait_for_user"),
        ("paused", "active", "session_start", "interactive", "wait_for_user"),
        ("paused", "active", "session_start", "headless", "wait_for_user"),
        ("attached", "needs_user", "session_start", "interactive", "wait_for_user"),
        ("attached", "needs_user", "session_start", "headless", "wait_for_user"),
        ("paused", "active", "user_turn", "headless", "wait_for_user"),
        ("paused", "active", "continue_confirmed", "interactive", "wait_for_user"),
        ("detached", "active", "user_turn", "interactive", "stop"),
        ("stale", "active", "continue_confirmed", "interactive", "stop"),
        ("attached", "complete", "user_turn", "interactive", "stop"),
        ("attached", "blocked", "session_start", "interactive", "wait_for_user"),
        ("attached", "abandoned", "continue_confirmed", "interactive", "stop"),
    ],
)
def test_native_continuation_host_parity(
    tmp_path, agent_harness, state, status, event, interaction, decision,
) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".gp")
    record = runtime.create_goal(raw_goal="Read the original README")
    record = runtime.activate_session(
        record.goal_plus_id, {"agent_harness": agent_harness, "session_id": "native-id", "state": state},
    )
    # Seed domain preconditions, independent of status-transition prerequisites.
    record = record.model_copy(update={"status": status})
    if state == "paused":
        from goal_plus.models import GoalPlusControl
        record = record.model_copy(update={"control": GoalPlusControl(state="paused", reason="legacy_pause")})
    runtime.state.write_goal(record)
    for _ in range(2):
        outcome = runtime.handle_host_session_event(
            record.goal_plus_id, agent_harness=agent_harness, session_id="native-id", event=event,
            delivery="auto", interaction=interaction,
        )
        assert outcome.decision == decision
        after = runtime.status(record.goal_plus_id)
        assert after.raw_goal == record.raw_goal
        assert after.goal_revision == record.goal_revision
        assert after.host_command_invocations == record.host_command_invocations
        assert after.status == status
        if decision == "stop":
            assert after.active_session == record.active_session


def test_create_goal_normalizes_explicit_exploration_mode(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")

    record = runtime.create_goal("mode=probe Determine whether approach A is viable")

    assert record.raw_goal == (
        "Determine whether approach A is viable\n\n"
        + EXPLORATION_MODE_LINES["probe"]
    )
    assert record.command_config.mode == "probe"
    with pytest.raises(ValueError, match="unsupported Goal Plus exploration mode"):
        runtime.create_goal("mode=fast Try approach A")


def test_host_command_config_is_typed_persisted_and_removed_from_goal(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")

    record = runtime.start_from_host_command(
        "max_parallel=2 max_duration_seconds=300 workspace_provider=git_worktree "
        "promotion_mode=artifact_only strategy=agent_guided inner_agent=common "
        "Optimize latency",
        invocation=_codex_invocation("codex:test:config"),
    )

    assert record.raw_goal.startswith("Optimize latency")
    assert "max_parallel=" not in record.raw_goal
    assert record.command_config.model_dump(exclude_none=True) == {
        "max_parallel": 2,
        "max_duration_seconds": 300,
        "workspace_provider": "git_worktree",
        "promotion_mode": "artifact_only",
        "strategy": "agent_guided",
        "inner_agent": "common",
    }
    assert record.goal_revisions[-1].command_config == record.command_config
    assert record.outer_deadline_at is not None
    created_epoch = calendar.timegm(
        time.strptime(record.created_at, "%Y-%m-%dT%H:%M:%SZ")
    )
    deadline_epoch = calendar.timegm(
        time.strptime(record.outer_deadline_at, "%Y-%m-%dT%H:%M:%SZ")
    )
    assert deadline_epoch - created_epoch == 300
    created = runtime.list_events(record.goal_plus_id)[0]
    assert created["payload"]["command_config"]["promotion_mode"] == "artifact_only"


def test_host_command_config_registry_is_the_public_single_source() -> None:
    accepted_keys = {
        key for option in GOAL_PLUS_COMMAND_OPTIONS for key in option.accepted_keys
    }
    canonical_fields = {option.model_field for option in GOAL_PLUS_COMMAND_OPTIONS}

    assert accepted_keys == GOAL_PLUS_COMMAND_CONFIG_KEYS
    assert canonical_fields == set(GoalPlusCommandConfig.model_fields) - {
        "main",
        "worker_directive",
    }
    assert len(accepted_keys) == sum(
        len(option.accepted_keys) for option in GOAL_PLUS_COMMAND_OPTIONS
    )
    assert all(option.default_description for option in GOAL_PLUS_COMMAND_OPTIONS)


def test_legacy_main_command_config_remains_readable() -> None:
    config = GoalPlusCommandConfig.model_validate({"main": "legacy/model"})

    assert config.main == "legacy/model"
    assert "main" not in GOAL_PLUS_COMMAND_CONFIG_KEYS


def test_host_command_config_generated_assets_are_current() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "generate_command_config_docs.py"),
            "--check",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_edit_preserves_goal_deadline_from_original_creation(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    record = runtime.start_from_host_command(
        "max_duration_seconds=300 Optimize latency",
        invocation=_codex_invocation("codex:test:deadline-start"),
    )
    runtime.activate_session(record.goal_plus_id, {"agent_harness": "codex", "session_id": "session-test"})
    deadline = record.outer_deadline_at

    edited = runtime.edit_from_host_command(
        record.goal_plus_id,
        "Optimize latency and memory",
        expected_revision=record.goal_revision,
        invocation=_codex_invocation(
            "codex:test:deadline-edit",
            action="edit",
        ),
    )

    assert edited.command_config.max_duration_seconds == 300
    assert edited.outer_deadline_at == deadline


def test_partial_command_config_only_constrains_search_fields_user_supplied(
    tmp_path,
) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")

    record = runtime.start_from_host_command(
        "max_parallel=2 Optimize latency",
        invocation=_codex_invocation("codex:test:partial-config"),
    )

    assert record.command_config.model_dump(exclude_none=True) == {
        "max_parallel": 2,
    }
    search_spec = _complete_search_spec()
    search_spec["budget"]["max_parallel"] = 2
    search_spec["workspace"] = {
        "provider": "git_worktree",
        "promotion_mode": "apply",
    }
    search_spec["strategy"] = {"name": "random"}

    saved = runtime.save_spec_draft(
        record.goal_plus_id,
        GoalPlusSpecDraft(
            baseline={},
            metric={"name": "avg_latency_ms"},
            correctness_gate={},
            edit_surface={"allow": ["kernel.py"]},
            search_spec=search_spec,
            promotion_rule="lowest valid latency",
            confidence="high",
        ),
    )

    assert saved.spec_draft is not None
    assert saved.spec_draft.search_spec is not None
    assert saved.spec_draft.search_spec.workspace.provider == "git_worktree"
    assert saved.spec_draft.search_spec.strategy.name == "random"


def test_empty_command_config_leaves_search_fields_open_if_main_routes_to_search(
    tmp_path,
) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")

    record = runtime.start_from_host_command(
        "Optimize latency",
        invocation=_codex_invocation("codex:test:no-config"),
    )

    assert record.command_config.model_dump(exclude_none=True) == {}
    search_spec = _complete_search_spec()
    search_spec["budget"]["max_parallel"] = 3
    search_spec["workspace"] = {
        "provider": "git_worktree",
        "promotion_mode": "apply",
    }
    search_spec["strategy"] = {"name": "agent_guided"}

    saved = runtime.save_spec_draft(
        record.goal_plus_id,
        GoalPlusSpecDraft(
            baseline={},
            metric={"name": "avg_latency_ms"},
            correctness_gate={},
            edit_surface={"allow": ["kernel.py"]},
            search_spec=search_spec,
            promotion_rule="lowest valid latency",
            confidence="high",
        ),
    )

    assert saved.spec_draft is not None
    assert saved.spec_draft.search_spec is not None
    assert saved.spec_draft.search_spec.budget.max_parallel == 3
    assert saved.spec_draft.search_spec.workspace.provider == "git_worktree"
    assert saved.spec_draft.search_spec.strategy.name == "agent_guided"


def test_search_command_config_may_remain_unused_when_main_finishes_in_goal_mode(
    tmp_path,
) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")

    record = runtime.start_from_host_command(
        "max_parallel=2 workspace_provider=git_worktree "
        "promotion_mode=artifact_only strategy=random "
        "inner_agent=common "
        "annotator=unused/annotator workers=unused/a,unused/b "
        "Fix the documentation typo",
        invocation=_codex_invocation("codex:test:unused-search-config"),
    )
    triaged = runtime.record_triage(
        record.goal_plus_id,
        {
            "is_optimization": False,
            "confidence": "high",
            "recommended_phase": "goal",
            "reasons": ["qualitative documentation task"],
        },
    )
    completed = runtime.set_status(
        record.goal_plus_id,
        "complete",
        reason="Main completed the non-Search task directly.",
        evidence=[{"kind": "review", "result": "passed"}],
    )

    assert triaged.phase == "goal"
    assert completed.status == "complete"
    assert completed.command_config.model_dump(exclude_none=True) == {
        "max_parallel": 2,
        "workspace_provider": "git_worktree",
        "promotion_mode": "artifact_only",
        "strategy": "random",
        "inner_agent": "common",
        "annotator": "unused/annotator",
        "workers": "unused/a,unused/b",
        "worker_directive": "workers",
    }
    assert completed.spec_draft is None
    assert completed.search_tasks == []


def test_set_status_rejects_unknown_value_without_mutating_goal(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    record = runtime.create_goal("Keep state valid")
    before = record.model_dump(mode="json")

    with pytest.raises(ValidationError):
        runtime.set_status(record.goal_plus_id, "completed")  # type: ignore[arg-type]

    assert runtime.status(record.goal_plus_id).model_dump(mode="json") == before


@pytest.mark.parametrize(
    "raw_goal",
    [
        "unknown=value Optimize latency",
        "MAX_PARALLEL=2 Optimize latency",
        "max-parallel=2 Optimize latency",
        "main=gpt-5.6-sol Optimize latency",
        "max_parallel=zero Optimize latency",
        "max_duration_seconds=zero Optimize latency",
        "max_duration_seconds=0 Optimize latency",
        "workers=A*1,B Optimize latency",
    ],
)
def test_host_command_config_rejects_invalid_directives(tmp_path, raw_goal: str) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")

    with pytest.raises((ValueError, ValidationError)):
        runtime.create_goal(raw_goal)


def test_explicit_command_config_enforces_ready_search_spec(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    record = runtime.create_goal(
        "max_parallel=2 promotion_mode=artifact_only inner_agent=common "
        "Optimize latency"
    )
    search_spec = _complete_search_spec()

    with pytest.raises(ValueError, match="max_parallel=2"):
        runtime.save_spec_draft(
            record.goal_plus_id,
            GoalPlusSpecDraft(
                baseline={},
                metric={"name": "avg_latency_ms"},
                correctness_gate={},
                edit_surface={"allow": ["kernel.py"]},
                search_spec=search_spec,
                promotion_rule="lowest valid latency",
                confidence="high",
            ),
        )

    search_spec["budget"]["max_parallel"] = 2
    with pytest.raises(ValueError, match="promotion_mode=artifact_only"):
        runtime.save_spec_draft(
            record.goal_plus_id,
            GoalPlusSpecDraft(
                baseline={},
                metric={"name": "avg_latency_ms"},
                correctness_gate={},
                edit_surface={"allow": ["kernel.py"]},
                search_spec=search_spec,
                promotion_rule="lowest valid latency",
                confidence="high",
            ),
        )

    search_spec["workspace"] = {"promotion_mode": "artifact_only"}
    with pytest.raises(ValueError, match="inner_agent=common"):
        runtime.save_spec_draft(
            record.goal_plus_id,
            GoalPlusSpecDraft(
                baseline={},
                metric={"name": "avg_latency_ms"},
                correctness_gate={},
                edit_surface={"allow": ["kernel.py"]},
                search_spec=search_spec,
                promotion_rule="lowest valid latency",
                confidence="high",
            ),
        )

    search_spec["strategy"] = {"inner_agent": "common"}
    saved = runtime.save_spec_draft(
        record.goal_plus_id,
        GoalPlusSpecDraft(
            baseline={},
            metric={"name": "avg_latency_ms"},
            correctness_gate={},
            edit_surface={"allow": ["kernel.py"]},
            search_spec=search_spec,
            promotion_rule="lowest valid latency",
            confidence="high",
        ),
    )

    assert saved.spec_draft is not None


def test_update_goal_preserves_or_explicitly_changes_exploration_mode(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    record = runtime.create_goal("mode=probe Try approach A")

    preserved = runtime.update_goal(
        record.goal_plus_id,
        raw_goal="Try approaches A and B",
        expected_revision=1,
    )
    assert preserved.raw_goal.endswith(EXPLORATION_MODE_LINES["probe"])
    assert preserved.command_config.mode == "probe"

    changed = runtime.update_goal(
        record.goal_plus_id,
        raw_goal="mode=autonomous Try approaches A, B, and C",
        expected_revision=2,
    )
    assert changed.raw_goal.endswith(EXPLORATION_MODE_LINES["autonomous"])
    assert changed.command_config.mode == "autonomous"


def test_update_goal_creates_revision_and_supersedes_pending_check(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    record = runtime.create_goal(
        "Implement the first requirement",
        policy={"final_check": {"mode": "required"}},
    )
    runtime.record_triage(
        record.goal_plus_id,
        {
            "is_optimization": False,
            "confidence": "high",
            "recommended_phase": "goal",
        },
    )
    request = runtime.prepare_final_check(record.goal_plus_id, "codex")

    updated = runtime.update_goal(
        record.goal_plus_id,
        raw_goal="Implement both the first and second requirements",
        expected_revision=1,
        reason="user added a requirement after interruption",
    )

    assert updated.goal_plus_id == record.goal_plus_id
    assert updated.goal_revision == 2
    assert [revision.revision for revision in updated.goal_revisions] == [1, 2]
    assert updated.goal_revisions[-1].raw_goal == updated.raw_goal
    assert updated.final_checks[0].check_id == request["check"]["check_id"]
    assert updated.final_checks[0].status == "superseded"
    assert updated.phase == "goal"
    assert updated.triage is None
    assert updated.next_action.kind == "execute_goal"  # type: ignore[union-attr]
    assert runtime.gate(
        record.goal_plus_id, event="pre_tool_use", context={"tool_name": "edit"},
    ).decision == "allow"
    with pytest.raises(RuntimeError, match="revision conflict"):
        runtime.update_goal(
            record.goal_plus_id,
            raw_goal="stale edit",
            expected_revision=1,
        )


def test_required_final_check_blocks_stop_and_completes_only_after_pass(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    record = runtime.create_goal(
        "Ship a complete implementation",
        source_path=".",
        policy={"final_check": {"mode": "required"}},
    )
    gate = runtime.gate(record.goal_plus_id, event="stop", context={})
    assert gate.decision == "block"
    assert "独立最终检查" in gate.continuation_prompt
    with pytest.raises(RuntimeError, match="requires a passing final check"):
        runtime.set_status(record.goal_plus_id, status="complete")

    first = runtime.prepare_final_check(record.goal_plus_id, "codex")
    resumed = runtime.prepare_final_check(record.goal_plus_id, "codex")
    assert resumed["check"]["check_id"] == first["check"]["check_id"]
    assert resumed["launch"] == first["launch"]
    assert first["launch"] == {"tool": "goal_plus_run_final_check", "goal_plus_id": record.goal_plus_id,
                               "check_id": first["check"]["check_id"], "goal_revision": record.goal_revision}

    failed = runtime.submit_final_check(
        record.goal_plus_id,
        check_id=first["check"]["check_id"],
        goal_revision=1,
        verdict="fail",
        summary="One explicit requirement is still missing.",
        findings=[{"requirement": "tests", "problem": "missing interruption coverage"}],
        evidence=[{"kind": "test_inventory", "result": "missing"}],
    )
    assert failed.status == "active"
    assert failed.next_action.kind == "address_final_check_findings"  # type: ignore[union-attr]

    second = runtime.prepare_final_check(record.goal_plus_id, "codex")
    assert second["check"]["check_id"] != first["check"]["check_id"]
    assert second["launch"]["check_id"] != first["launch"]["check_id"]
    completed = runtime.submit_final_check(
        record.goal_plus_id,
        check_id=second["check"]["check_id"],
        goal_revision=1,
        verdict="pass",
        summary="Every requirement is implemented and covered.",
        evidence=[{"kind": "pytest", "result": "passed"}],
    )
    assert completed.status == "complete"
    assert completed.final_checks[-1].status == "passed"
    assert runtime.gate(record.goal_plus_id, event="stop", context={}).decision == "allow"


def test_final_checker_receives_only_git_tracked_task_local_skills(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    task_skill = workspace / ".agents" / "skills" / "precision-guard" / "SKILL.md"
    bundled_skill = workspace / "plugins" / "goal-plus" / "skills" / "goal-plus" / "SKILL.md"
    codex_skill = workspace / ".codex" / "skills" / "goal-plus" / "SKILL.md"
    untracked_skill = workspace / ".agents" / "skills" / "untracked" / "SKILL.md"
    for path in (task_skill, bundled_skill, codex_skill, untracked_skill):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {path.parent.name}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(
        [
            "git",
            "add",
            ".agents/skills/precision-guard",
            ".codex/skills/goal-plus",
            "plugins/goal-plus/skills/goal-plus",
        ],
        cwd=workspace,
        check=True,
    )

    runtime = FileGoalPlusRuntime(tmp_path / ".gp")
    record = runtime.create_goal(
        "Optimize without changing precision",
        source_path=str(workspace),
        policy={"final_check": {"mode": "required"}},
    )
    runtime.record_triage(
        record.goal_plus_id,
        {
            "is_optimization": False,
            "confidence": "high",
            "recommended_phase": "goal",
        },
    )

    runtime.prepare_final_check(record.goal_plus_id, "codex")
    inputs = runtime.goal_completion.artifacts.final_check_inputs(runtime.state.read_goal(record.goal_plus_id))
    assert list(inputs.task_skill_paths) == [".agents/skills/precision-guard/SKILL.md"]


def test_interrupted_final_check_requires_fresh_host_review(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    record = runtime.create_goal(
        "Ship after an independent review",
        source_path=".",
        policy={"final_check": {"mode": "required"}},
    )
    runtime.record_triage(
        record.goal_plus_id,
        {
            "is_optimization": False,
            "confidence": "high",
            "recommended_phase": "goal",
        },
    )
    first = runtime.prepare_final_check(record.goal_plus_id, "pi")
    assert first["launch"]["tool"] == "goal_plus_run_final_check"

    interrupted = runtime.submit_final_check(
        record.goal_plus_id,
        check_id=first["check"]["check_id"],
        goal_revision=1,
        verdict="interrupted",
        summary="Pi reviewer timed out before recording a verdict.",
        checker_metadata={"timed_out": True},
    )
    assert interrupted.status == "active"
    assert interrupted.final_checks[-1].status == "interrupted"
    assert interrupted.next_action.kind == "retry_final_check"  # type: ignore[union-attr]
    assert runtime.gate(record.goal_plus_id, event="stop", context={}).decision == "block"

    second = runtime.prepare_final_check(record.goal_plus_id, "pi")
    assert second["check"]["check_id"] != first["check"]["check_id"]
    assert second["check"]["status"] == "pending"


def test_pending_final_checker_can_stop_without_blocking_parent_cleanup(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    record = runtime.create_goal(
        "Review me",
        policy={"final_check": {"mode": "required"}},
    )
    runtime.record_triage(
        record.goal_plus_id,
        {
            "is_optimization": False,
            "confidence": "high",
            "recommended_phase": "goal",
        },
    )
    runtime.prepare_final_check(record.goal_plus_id, "codex")

    gate = runtime.gate(
        record.goal_plus_id,
        event="subagent_stop",
        context={"goal_plus_subagent_role": "final_checker"},
    )
    assert gate.decision == "allow"


def test_goal_update_keeps_old_search_result_historical(tmp_path) -> None:
    root = tmp_path / ".search"
    runtime = FileGoalPlusRuntime(root)
    record = runtime.create_goal("Optimize revision one")
    _write_search_run(root, "run_001", "spec_abc")
    linked = runtime.link_search_run(record.goal_plus_id, "spec_abc", "run_001")
    assert linked.search_tasks[0].goal_revision == 1

    revised = runtime.update_goal(
        record.goal_plus_id,
        "Optimize a different revision two objective",
        expected_revision=1,
    )
    assert revised.linked_search is None

    run_dir = root / "runs" / "run_001"
    (run_dir / "report.md").write_text("# report\n", encoding="utf-8")
    promotion = run_dir / "promotion" / "c001.patch"
    promotion.parent.mkdir(parents=True)
    promotion.write_text("patch\n", encoding="utf-8")
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "frozen_spec_id": "spec_abc",
                "state": "promoted",
                "selected_candidate_id": "c001",
            }
        ),
        encoding="utf-8",
    )
    late = runtime.record_search_result(record.goal_plus_id, "run_001")
    assert late.goal_revision == 2
    assert late.phase == "goal"
    assert late.linked_search is None
    assert late.search_tasks[0].result_recorded_at is not None


@pytest.mark.pi
def test_final_check_launch_is_revision_bound(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".gp")
    record = runtime.create_goal(
        "Verify the Pi delivery",
        source_path=".",
        policy={"final_check": {"mode": "required"}},
    )
    runtime.record_triage(
        record.goal_plus_id,
        {
            "is_optimization": False,
            "confidence": "high",
            "recommended_phase": "goal",
        },
    )
    request = runtime.prepare_final_check(record.goal_plus_id, "pi")
    launch = request["launch"]
    assert launch == {"tool": "goal_plus_run_final_check", "goal_plus_id": record.goal_plus_id,
                      "check_id": request["check"]["check_id"], "goal_revision": 1}

    runtime.update_goal(
        record.goal_plus_id,
        "Verify the revised Pi delivery",
        expected_revision=1,
    )
    with pytest.raises(RuntimeError, match="stale for goal revision"):
        runtime.submit_final_check(
            record.goal_plus_id,
            check_id=request["check"]["check_id"],
            goal_revision=1,
            verdict="pass",
            summary="old review",
            evidence=[{"kind": "pytest", "result": "passed"}],
        )


def test_goal_like_triage_blocks_stop_until_terminal_status(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    record = runtime.create_goal("Tidy docs wording")

    updated = runtime.record_triage(
        record.goal_plus_id,
        GoalPlusTriage(
            is_optimization=False,
            confidence="high",
            recommended_phase="goal",
            reasons=["qualitative documentation task"],
        ),
    )

    assert updated.phase == "goal"
    assert updated.next_action.kind == "execute_goal"  # type: ignore[union-attr]
    assert updated.next_action.required is True  # type: ignore[union-attr]

    gate = runtime.gate(updated.goal_plus_id, event="stop", context={})
    assert gate.decision == "block"
    assert "仍处于 active" in gate.continuation_prompt
    assert "当前 next action" in gate.continuation_prompt
    assert "goal_plus_status" in gate.continuation_prompt
    assert "时间参照：created_at=" in gate.continuation_prompt
    assert "即使实际收尾晚于预设也使用 complete" in gate.continuation_prompt
    assert "不要仅因超出预设而使用 blocked" in gate.continuation_prompt
    assert "goal_plus_set_status" in gate.continuation_prompt
    # The goal/config data is delivered by each host's per-turn context; the
    # continuation only carries the rules, so it must stay small.
    assert "Tidy docs wording" not in gate.continuation_prompt
    assert len(gate.continuation_prompt) < 800


def test_stop_gate_compact_prompt_keeps_live_state_and_policy_pointer(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    record = runtime.create_goal("Tidy docs wording")

    updated = runtime.record_triage(
        record.goal_plus_id,
        GoalPlusTriage(
            is_optimization=False,
            confidence="high",
            recommended_phase="goal",
            reasons=["qualitative documentation task"],
        ),
    )

    gate = runtime.gate(
        updated.goal_plus_id,
        event="stop",
        context={"stop_prompt_compact": True},
    )
    assert gate.decision == "block"
    prompt = gate.continuation_prompt
    # Live state the model cannot recall stays in every injection.
    assert "仍处于 active" in prompt
    assert "当前 next action" in prompt
    assert "时间参照：created_at=" in prompt
    assert "goal_plus_set_status" in prompt
    assert "完整停止与终态规则" in prompt
    # The static terminal-state policy collapses to its pointer line.
    assert "即使实际收尾晚于预设也使用 complete" not in prompt
    assert "不要仅因超出预设而使用 blocked" not in prompt
    assert "Tidy docs wording" not in prompt
    assert len(prompt) < 500


def test_spec_discovery_stop_gate_blocks_with_next_action(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    record = runtime.create_goal("Optimize model throughput")

    updated = runtime.record_triage(
        record.goal_plus_id,
        {
            "is_optimization": True,
            "confidence": "medium",
            "recommended_phase": "spec_discovery",
            "scenario": "model-infer",
            "reasons": ["throughput is measurable"],
            "missing": ["baseline command", "correctness gate"],
        },
    )

    gate = runtime.gate(updated.goal_plus_id, event="stop", context={})

    assert gate.decision == "block"
    assert gate.phase == "spec_discovery"
    assert "仍处于 active" in gate.continuation_prompt
    assert "baseline command" in gate.continuation_prompt


@pytest.mark.parametrize("tool_name", ["bash", "write", "edit", "apply_patch"])
def test_spec_discovery_allows_host_tools_needed_to_discover_and_materialize_verifier(
    tmp_path, tool_name: str
) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    record = runtime.create_goal("Optimize model throughput")
    runtime.record_triage(
        record.goal_plus_id,
        {
            "is_optimization": True,
            "confidence": "medium",
            "recommended_phase": "spec_discovery",
            "reasons": ["throughput is measurable"],
            "missing": ["public verifier"],
        },
    )

    gate = runtime.gate(
        record.goal_plus_id,
        event="pre_tool_use",
        context={"tool_name": tool_name},
    )

    assert gate.decision == "allow"


def test_ready_spec_draft_requires_complete_search_spec_at_save_boundary(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    record = runtime.create_goal("Optimize model throughput")
    runtime.record_triage(
        record.goal_plus_id,
        {
            "is_optimization": True,
            "confidence": "high",
            "recommended_phase": "search",
            "reasons": ["throughput is measurable"],
        },
    )

    with pytest.raises(ValidationError):
        runtime.save_spec_draft(
            record.goal_plus_id,
            GoalPlusSpecDraft(
                baseline={},
                metric={"name": "throughput"},
                correctness_gate={},
                edit_surface={"allow": ["kernel.py"]},
                search_spec={"metric_name": "throughput"},
                promotion_rule="highest valid throughput",
                confidence="high",
            ),
        )

    assert runtime.status(record.goal_plus_id).spec_draft is None


@pytest.mark.parametrize("record_triage", [False, True])
def test_initial_search_ready_spec_autonomously_allows_freeze(tmp_path, record_triage) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    record = runtime.create_goal("Optimize kernel latency")
    if record_triage:
        triaged = runtime.record_triage(
            record.goal_plus_id,
            GoalPlusTriage(
                is_optimization=True,
                confidence="high",
                recommended_phase="search",
                identified_at="initial",
                scenario="latency-optimization",
                reasons=["latency benchmark exists"],
            ),
        )
        assert triaged.next_action.kind == "draft_initial_search_spec"  # type: ignore[union-attr]

    draft = runtime.save_spec_draft(
        record.goal_plus_id,
        GoalPlusSpecDraft(
            baseline={"command": "python bench.py"},
            metric={"name": "avg_latency_ms", "direction": "minimize"},
            correctness_gate={"command": "python verify.py"},
            edit_surface={"allow": ["kernel.py"], "deny": ["verify.py"]},
            verifier_artifacts=["verify.py", "bench.py"],
            search_spec=_complete_search_spec(),
            promotion_rule="correctness pass and lower latency",
            confidence="high",
            origin="initial",
        ),
    )
    assert draft.next_action.kind == "freeze_search_spec"  # type: ignore[union-attr]
    assert draft.phase == "spec_discovery"
    before_completion = _goal_domain_state(runtime.status(record.goal_plus_id))
    with pytest.raises(RuntimeError, match="Search completion requires"):
        runtime.set_status(record.goal_plus_id, status="complete")
    assert _goal_domain_state(runtime.status(record.goal_plus_id)) == before_completion

    for tool_name in ("bash", "write", "edit"):
        discovery_tool = runtime.gate(
            record.goal_plus_id,
            event="pre_tool_use",
            context={"tool_name": tool_name},
        )
        assert discovery_tool.decision == "allow"

    stop_gate = runtime.gate(record.goal_plus_id, event="stop", context={})
    assert stop_gate.decision == "block"
    assert "自主冻结" in stop_gate.reason

    allowed = runtime.gate(
        record.goal_plus_id,
        event="pre_tool_use",
        context={
            "tool_name": "goal_plus_search_freeze_spec",
            "tool_input": {"goal_plus_id": record.goal_plus_id},
        },
    )
    assert allowed.decision == "allow"

    still_allowed = runtime.gate(
        record.goal_plus_id,
        event="pre_tool_use",
        context={
            "tool_name": "goal_plus_search_freeze_spec",
            "tool_input": {"goal_plus_id": record.goal_plus_id},
        },
    )
    assert still_allowed.decision == "allow"


def test_in_progress_search_discovery_uses_same_autonomous_admission(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    record = runtime.create_goal("Improve docs, then optimize verifier if found")
    runtime.record_triage(
        record.goal_plus_id,
        GoalPlusTriage(
            is_optimization=True,
            confidence="high",
            recommended_phase="search",
            identified_at="in_progress",
            reasons=["constructed verifier during goal execution"],
        ),
    )

    draft = runtime.save_spec_draft(
        record.goal_plus_id,
        GoalPlusSpecDraft(
            baseline={"command": "python bench.py"},
            metric={"name": "avg_latency_ms", "direction": "minimize"},
            correctness_gate={"command": "python verify.py"},
            edit_surface={"allow": ["kernel.py"], "deny": ["verify.py"]},
            verifier_artifacts=["verify.py", "bench.py"],
            search_spec=_complete_search_spec(),
            promotion_rule="correctness pass and lower latency",
            confidence="high",
            origin="in_progress",
        ),
    )
    assert draft.next_action.kind == "freeze_search_spec"  # type: ignore[union-attr]

    gate = runtime.gate(
        record.goal_plus_id,
        event="pre_tool_use",
        context={
            "tool_name": "goal_plus_search_freeze_spec",
            "tool_input": {"goal_plus_id": record.goal_plus_id},
        },
    )
    assert gate.decision == "allow"


def test_high_confidence_spec_draft_links_search_and_final_audit(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    record = runtime.create_goal("Optimize kernel latency")
    runtime.record_triage(
        record.goal_plus_id,
        GoalPlusTriage(
            is_optimization=True,
            confidence="high",
            recommended_phase="search",
            identified_at="in_progress",
            scenario="latency-optimization",
            reasons=["latency benchmark exists"],
        ),
    )

    draft = runtime.save_spec_draft(
        record.goal_plus_id,
        GoalPlusSpecDraft(
            baseline={"command": "python bench.py"},
            metric={"name": "avg_latency_ms", "direction": "minimize"},
            correctness_gate={"command": "python verify.py"},
            edit_surface={"allow": ["kernel.py"], "deny": ["verify.py"]},
            verifier_artifacts=["verify.py", "bench.py"],
            search_spec=_complete_search_spec(),
            promotion_rule="correctness pass and lower latency",
            confidence="high",
            origin="in_progress",
        ),
    )
    assert draft.next_action.kind == "freeze_search_spec"  # type: ignore[union-attr]

    _write_search_run(tmp_path / ".search", "run_001", "spec_abc")
    linked = runtime.link_search_run(record.goal_plus_id, "spec_abc", "run_001")
    assert linked.phase == "search"
    assert linked.linked_search.run_id == "run_001"  # type: ignore[union-attr]
    with pytest.raises(RuntimeError, match="goal_plus_record_search_result"):
        runtime.set_status(
            record.goal_plus_id,
            status="complete",
            reason="no verified candidate was available",
        )

    run_dir = tmp_path / ".search" / "runs" / "run_001"
    report_path = run_dir / "report.md"
    html_report_path = run_dir / "report.html"
    promotion_path = run_dir / "promotion" / "c001.patch"
    promotion_path.parent.mkdir(parents=True)
    report_path.write_text("# report\n", encoding="utf-8")
    html_report_path.write_text("<!doctype html>\n", encoding="utf-8")
    promotion_path.write_text("patch\n", encoding="utf-8")
    (run_dir / "run.json").write_text(
        json.dumps({"state": "promoted", "selected_candidate_id": "c001"}),
        encoding="utf-8",
    )

    final = runtime.record_search_result(
        record.goal_plus_id,
        run_id="run_001",
        selected_candidate_id="c001",
        summary="c001 won",
    )
    assert final.phase == "final_audit"

    gate = runtime.gate(record.goal_plus_id, event="stop", context={})
    assert gate.decision == "block"
    assert "对照持久证据审计完整原始目标中的每项要求" in gate.continuation_prompt
    # A prose time limit needs a judgeable reference, so the continuation
    # carries the goal's creation time and the current time itself.
    assert "时间参照：created_at=" in gate.continuation_prompt
    assert "当前时间=" in gate.continuation_prompt

    completed = runtime.set_status(
        record.goal_plus_id,
        status="complete",
        reason="raw goal audited",
        evidence=[{"kind": "report", "path": "/tmp/report.md"}],
    )
    assert completed.status == "complete"
    assert runtime.gate(record.goal_plus_id, event="stop", context={}).decision == "allow"


def test_pre_tool_use_enforces_goal_and_linked_run_ownership(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    record = runtime.create_goal("Optimize kernel latency")
    runtime.record_triage(
        record.goal_plus_id,
        GoalPlusTriage(
            is_optimization=True,
            confidence="high",
            recommended_phase="search",
            identified_at="in_progress",
            reasons=["latency benchmark exists"],
        ),
    )
    runtime.save_spec_draft(
        record.goal_plus_id,
        GoalPlusSpecDraft(
            baseline={"command": "python bench.py"},
            metric={"name": "avg_latency_ms", "direction": "minimize"},
            correctness_gate={"command": "python verify.py"},
            edit_surface={"allow": ["kernel.py"], "deny": ["verify.py"]},
            verifier_artifacts=["verify.py", "bench.py"],
            search_spec=_complete_search_spec(),
            promotion_rule="correctness pass and lower latency",
            confidence="high",
            origin="in_progress",
        ),
    )
    _write_search_run(tmp_path / ".search", "run_001", "spec_abc")
    runtime.link_search_run(record.goal_plus_id, "spec_abc", "run_001")

    gate = runtime.gate(
        record.goal_plus_id,
        event="pre_tool_use",
        context={"tool_name": "goal_plus_search_promote", "input": {"run_id": "run_001"}},
    )

    assert gate.decision == "allow"

    domain_before = _goal_domain_state(runtime.status(record.goal_plus_id))
    wrong_run = runtime.gate(
        record.goal_plus_id,
        event="pre_tool_use",
        context={"tool_name": "goal_plus_search_promote", "input": {"run_id": "run_other"}},
    )
    wrong_allocations = [
        runtime.gate(
            record.goal_plus_id,
            event="pre_tool_use",
            context={"tool_name": tool_name, "input": {"run_id": "run_other"}},
        )
        for tool_name in (
            "goal_plus_search_list_allocation_decisions",
            "goal_plus_search_apply_allocation_decision",
        )
    ]
    wrong_goal = runtime.gate(
        record.goal_plus_id,
        event="pre_tool_use",
        context={
            "tool_name": "goal_plus_record_triage",
            "input": {"goal_plus_id": "gp_other"},
        },
    )
    assert wrong_run.decision == "block"
    assert all(item.decision == "block" for item in wrong_allocations)
    assert wrong_goal.decision == "block"
    assert "GoalPlusOwnershipMismatch" in (wrong_run.reason or "")
    assert all(
        "GoalPlusOwnershipMismatch" in (item.reason or "")
        for item in wrong_allocations
    )
    assert "GoalPlusOwnershipMismatch" in (wrong_goal.reason or "")
    assert _goal_domain_state(runtime.status(record.goal_plus_id)) == domain_before


def test_link_search_run_is_idempotent_and_appends_distinct_search_tasks(tmp_path) -> None:
    root = tmp_path / ".search"
    runtime = FileGoalPlusRuntime(root)
    record = runtime.create_goal("Optimize model")
    _write_search_run(root, "run_001", "spec_abc")
    _write_search_run(root, "run_002", "spec_def")

    linked = runtime.link_search_run(record.goal_plus_id, "spec_abc", "run_001")
    assert linked.linked_search is not None
    assert linked.linked_search.run_id == "run_001"

    linked_again = runtime.link_search_run(record.goal_plus_id, "spec_abc", "run_001")
    assert linked_again.linked_search is not None
    assert linked_again.linked_search.run_id == "run_001"

    second = runtime.link_search_run(record.goal_plus_id, "spec_def", "run_002")
    assert second.linked_search is not None
    assert second.linked_search.run_id == "run_002"
    assert [task.run_id for task in second.search_tasks] == ["run_001", "run_002"]

    final = runtime.status(record.goal_plus_id)
    assert final.linked_search is not None
    assert final.linked_search.frozen_spec_id == "spec_def"
    assert final.linked_search.run_id == "run_002"
    assert [task.run_id for task in final.search_tasks] == ["run_001", "run_002"]
    assert len(
        [event for event in runtime.list_events(record.goal_plus_id) if event["event_type"] == "search_linked"]
    ) == 2

    other = runtime.create_goal("Unrelated optimization")
    other_before = _goal_domain_state(other)
    with pytest.raises(RuntimeError, match="already linked to Goal Plus"):
        runtime.link_search_run(other.goal_plus_id, "spec_abc", "run_001")
    assert _goal_domain_state(runtime.status(other.goal_plus_id)) == other_before


def test_goal_status_recovers_legacy_search_task_history_from_events(tmp_path) -> None:
    root = tmp_path / ".search"
    runtime = FileGoalPlusRuntime(root)
    record = runtime.create_goal("Optimize model")
    _write_search_run(root, "run_001", "spec_abc")
    _write_search_run(root, "run_002", "spec_def")
    runtime.link_search_run(record.goal_plus_id, "spec_abc", "run_001")
    latest = runtime.link_search_run(record.goal_plus_id, "spec_def", "run_002")

    legacy_payload = latest.model_dump(mode="json")
    legacy_payload.pop("search_tasks")
    runtime.state.goal_path(record.goal_plus_id).write_text(  # noqa: SLF001
        json.dumps(legacy_payload),
        encoding="utf-8",
    )

    recovered = runtime.status(record.goal_plus_id)

    assert [task.run_id for task in recovered.search_tasks] == ["run_001", "run_002"]
    assert recovered.search_tasks[0].linked_at is not None
    assert recovered.linked_search is not None
    assert recovered.linked_search.run_id == "run_002"


def test_recording_superseded_search_result_preserves_current_task(tmp_path) -> None:
    root = tmp_path / ".search"
    runtime = FileGoalPlusRuntime(root)
    record = runtime.create_goal("Optimize model")
    _write_search_run(root, "run_001", "spec_abc")
    _write_search_run(root, "run_002", "spec_def")
    runtime.link_search_run(record.goal_plus_id, "spec_abc", "run_001")
    runtime.link_search_run(record.goal_plus_id, "spec_def", "run_002")

    run_dir = root / "runs" / "run_001"
    report_path = run_dir / "report.md"
    html_report_path = run_dir / "report.html"
    promotion_path = run_dir / "promotion" / "c001.patch"
    promotion_path.parent.mkdir(parents=True)
    report_path.write_text("# report\n", encoding="utf-8")
    html_report_path.write_text("<!doctype html>\n", encoding="utf-8")
    promotion_path.write_text("patch\n", encoding="utf-8")
    (run_dir / "run.json").write_text(
        json.dumps({"state": "promoted", "selected_candidate_id": "c001"}),
        encoding="utf-8",
    )

    updated = runtime.record_search_result(
        record.goal_plus_id,
        run_id="run_001",
        selected_candidate_id="c001",
        summary="older task completed",
    )

    assert updated.phase == "search"
    assert updated.linked_search is not None
    assert updated.linked_search.run_id == "run_002"
    assert updated.search_tasks[0].selected_candidate_id == "c001"
    assert updated.search_tasks[0].html_report_path == str(html_report_path.resolve())
    assert updated.search_tasks[0].result_recorded_at is not None


def test_link_search_run_rejects_missing_or_mismatched_runtime_run(tmp_path) -> None:
    root = tmp_path / ".search"
    runtime = FileGoalPlusRuntime(root)
    record = runtime.create_goal("Optimize model")
    domain_before = _goal_domain_state(record)
    events_before = runtime.list_events(record.goal_plus_id)

    with pytest.raises(FileNotFoundError, match="Call goal_plus_search_create"):
        runtime.link_search_run(record.goal_plus_id, "spec_abc", "run_0001")
    assert _goal_domain_state(runtime.status(record.goal_plus_id)) == domain_before
    assert runtime.list_events(record.goal_plus_id) == events_before

    _write_search_run(root, "run_real", "spec_other")
    with pytest.raises(ValueError, match="belongs to frozen spec spec_other"):
        runtime.link_search_run(record.goal_plus_id, "spec_abc", "run_real")

    final = runtime.status(record.goal_plus_id)
    assert _goal_domain_state(final) == domain_before
    assert runtime.list_events(record.goal_plus_id) == events_before


def test_record_search_result_requires_goal_run_link_without_domain_side_effects(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".search"
    runtime = FileGoalPlusRuntime(root)
    owner = runtime.start_from_host_command(
        "Optimize the linked model",
        invocation=_codex_invocation("codex:test:owner"),
    )
    _write_search_run(root, "run_owned", "spec_owned")
    runtime.link_search_run(owner.goal_plus_id, "spec_owned", "run_owned")
    runtime.set_status(owner.goal_plus_id, status="abandoned", reason="superseded")
    unrelated = runtime.start_from_host_command(
        "Audit an unrelated component",
        invocation=_codex_invocation("codex:test:unrelated"),
    )
    owner_before = _goal_domain_state(runtime.status(owner.goal_plus_id))
    unrelated_before = _goal_domain_state(runtime.status(unrelated.goal_plus_id))
    owner_events_before = runtime.list_events(owner.goal_plus_id)
    unrelated_events_before = runtime.list_events(unrelated.goal_plus_id)

    with pytest.raises(RuntimeError, match="is not linked to search run"):
        runtime.record_search_result(
            unrelated.goal_plus_id,
            run_id="run_owned",
            selected_candidate_id="c001",
        )

    assert _goal_domain_state(runtime.status(owner.goal_plus_id)) == owner_before
    assert _goal_domain_state(runtime.status(unrelated.goal_plus_id)) == unrelated_before
    assert runtime.list_events(owner.goal_plus_id) == owner_events_before
    assert runtime.list_events(unrelated.goal_plus_id) == unrelated_events_before


def test_record_search_result_prefers_existing_runtime_artifact_paths(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    record = runtime.create_goal("Optimize model")
    _write_search_run(tmp_path / ".search", "run_001", "spec_abc")
    runtime.link_search_run(record.goal_plus_id, "spec_abc", "run_001")
    run_dir = tmp_path / ".search" / "runs" / "run_001"
    report_path = run_dir / "report.md"
    promotion_path = run_dir / "promotion" / "c001.patch"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("# report\n", encoding="utf-8")
    promotion_path.parent.mkdir(parents=True)
    promotion_path.write_text("patch\n", encoding="utf-8")
    (run_dir / "run.json").write_text(
        json.dumps({"state": "promoted", "selected_candidate_id": "c001"}),
        encoding="utf-8",
    )

    final = runtime.record_search_result(
        record.goal_plus_id,
        run_id="run_001",
        selected_candidate_id="c001",
        report_path="/tmp/model-filled-report.md",
        promotion_artifact_path="/tmp/model-filled-c001.patch",
        summary="c001 won",
    )

    assert final.linked_search is not None
    assert final.linked_search.report_path == str(report_path.resolve())
    assert final.linked_search.promotion_artifact_path == str(promotion_path.resolve())


@pytest.mark.parametrize("receipt_exists", [False, True])
def test_record_search_result_reserves_final_report_paths_without_generating_files(
    tmp_path, receipt_exists,
) -> None:
    root = tmp_path / ".search"
    runtime = FileGoalPlusRuntime(root)
    record = runtime.create_goal(
        "Optimize model", policy={"final_check": {"mode": "required"}},
    )
    _write_search_run(root, "run_001", "spec_abc")
    runtime.link_search_run(record.goal_plus_id, "spec_abc", "run_001")
    run_dir = root / "runs" / "run_001"
    promotion_path = run_dir / "promotion" / "c001.patch"
    promotion_path.parent.mkdir(parents=True)
    promotion_path.write_text("patch\n", encoding="utf-8")
    (run_dir / "run.json").write_text(
        json.dumps({"state": "promoted", "selected_candidate_id": "c001"}),
        encoding="utf-8",
    )

    receipt = tmp_path / "judge-receipt.json"
    if receipt_exists:
        receipt.write_text('{"valid": true}\n', encoding="utf-8")
    final = runtime.record_search_result(
        record.goal_plus_id,
        run_id="run_001",
        selected_candidate_id="c001",
        report_path=str(receipt),
        summary="c001 won",
    )

    report_path = run_dir / "report.md"
    html_report_path = run_dir / "report.html"
    assert final.linked_search is not None
    assert final.linked_search.report_path == str(report_path.resolve())
    assert final.linked_search.html_report_path == str(html_report_path.resolve())
    assert not report_path.exists()
    assert not html_report_path.exists()

    request = runtime.prepare_final_check(record.goal_plus_id, "codex")
    from goal_plus.application.goal_final_check_launch import final_check_prompt

    current = runtime.state.read_goal(record.goal_plus_id)
    prompt = final_check_prompt(current, current.final_checks[-1], runtime.goal_completion.artifacts.final_check_inputs(current))
    projection = prompt.split(
        "```goal_plus_report_lifecycle\n", 1,
    )[1].split("\n```", 1)[0]
    assert json.loads(projection) == {
        "generation_tool": "goal_plus_search_report",
        "generation_gate": "linked_goals_terminal",
        "reserved_outputs": [{
            "run_id": "run_001",
            "report_path": str(report_path.resolve()),
            "html_report_path": str(html_report_path.resolve()),
        }],
    }
    assert not report_path.exists()
    assert not html_report_path.exists()
    with pytest.raises(RuntimeError, match="passing final check"):
        runtime.set_status(record.goal_plus_id, "complete")


def test_record_search_result_rejects_unpromoted_run(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    record = runtime.create_goal("Optimize model")
    _write_search_run(tmp_path / ".search", "run_001", "spec_abc")
    runtime.link_search_run(record.goal_plus_id, "spec_abc", "run_001")
    run_dir = tmp_path / ".search" / "runs" / "run_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.md").write_text("# report\n", encoding="utf-8")
    (run_dir / "run.json").write_text(
        json.dumps({"state": "ready_to_promote", "selected_candidate_id": "c001"}),
        encoding="utf-8",
    )
    domain_before = _goal_domain_state(runtime.status(record.goal_plus_id))
    events_before = runtime.list_events(record.goal_plus_id)

    with pytest.raises(RuntimeError, match="goal_plus_search_promote"):
        runtime.record_search_result(
            record.goal_plus_id,
            run_id="run_001",
            selected_candidate_id="c001",
        )

    assert _goal_domain_state(runtime.status(record.goal_plus_id)) == domain_before
    assert runtime.list_events(record.goal_plus_id) == events_before


def test_record_search_result_allows_artifact_only_provider_native_promotion(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".search"
    runtime = FileGoalPlusRuntime(root)
    record = runtime.create_goal("Optimize model")
    _write_search_run(root, "run_001", "spec_abc")
    runtime.link_search_run(record.goal_plus_id, "spec_abc", "run_001")
    run_dir = root / "runs" / "run_001"
    promotion_path = run_dir / "promotion" / "c001.patch"
    promotion_path.parent.mkdir(parents=True)
    promotion_path.write_text("patch\n", encoding="utf-8")
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "state": "promoted",
                "selected_candidate_id": "c001",
                "promotion": {
                    "strategy": "squash_commit",
                    "promotion_mode": "artifact_only",
                    "state": "prepared",
                },
            }
        ),
        encoding="utf-8",
    )

    final = runtime.record_search_result(
        record.goal_plus_id,
        run_id="run_001",
        selected_candidate_id="c001",
    )

    assert final.linked_search is not None
    assert final.linked_search.selected_candidate_id == "c001"


def test_record_search_result_requires_default_provider_native_apply(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".search"
    runtime = FileGoalPlusRuntime(root)
    record = runtime.create_goal("Optimize model")
    _write_search_run(root, "run_001", "spec_abc")
    runtime.link_search_run(record.goal_plus_id, "spec_abc", "run_001")
    run_dir = root / "runs" / "run_001"
    promotion_path = run_dir / "promotion" / "c001.patch"
    promotion_path.parent.mkdir(parents=True)
    promotion_path.write_text("patch\n", encoding="utf-8")
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "state": "promoted",
                "selected_candidate_id": "c001",
                "promotion": {
                    "strategy": "squash_commit",
                    "promotion_mode": "apply",
                    "state": "prepared",
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="goal_plus_search_apply_promotion"):
        runtime.record_search_result(
            record.goal_plus_id,
            run_id="run_001",
            selected_candidate_id="c001",
        )


def test_pre_tool_use_rejects_freeze_without_goal_id(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    record = runtime.create_goal("Maybe optimize something")

    gate = runtime.gate(
        record.goal_plus_id,
        event="pre_tool_use",
        context={"tool_name": "goal_plus_search_freeze_spec"},
    )

    assert gate.decision == "block"
    assert "GoalPlusOwnershipMismatch" in gate.reason


@pytest.mark.parametrize("tool_name", ["bash", "shell", "exec_command", "apply_patch", "write", "edit"])
def test_ordinary_goal_allows_tools_without_triage(tmp_path, tool_name) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    record = runtime.create_goal("Tidy docs wording")
    domain_before = _goal_domain_state(record)
    events_before = runtime.list_events(record.goal_plus_id)

    gate = runtime.gate(
        record.goal_plus_id,
        event="pre_tool_use",
        context={"tool_name": tool_name},
    )

    assert gate.decision == "allow"
    assert gate.reason is None
    persisted = runtime.status(record.goal_plus_id)
    events_after = runtime.list_events(record.goal_plus_id)
    assert _goal_domain_state(persisted) == domain_before
    assert persisted.hook_counters == {"pre_tool_use": 1}
    assert events_after[:-1] == events_before
    assert events_after[-1]["event_type"] == "gate_allowed"
    assert events_after[-1]["payload"]["event"] == "pre_tool_use"


def test_ordinary_goal_completes_without_triage_or_search(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".gp")
    record = runtime.start_from_host_command(
        "Fix a bug", invocation=_codex_invocation("ordinary-start"),
    )
    assert record.phase == "goal"
    assert runtime.gate(record.goal_plus_id, event="stop", context={}).decision == "block"

    completed = runtime.set_status(
        record.goal_plus_id, status="complete", evidence=[{"kind": "tests", "passed": True}],
    )
    assert completed.status == "complete"
    assert completed.triage is None
    assert completed.spec_draft is None
    assert completed.search_tasks == []
    assert runtime.gate(record.goal_plus_id, event="stop", context={}).decision == "allow"
    assert not any(event["event_type"] == "triage_recorded" for event in runtime.list_events(record.goal_plus_id))


def test_goal_mode_allows_mutation_after_triage(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    record = runtime.create_goal("Tidy docs wording")
    runtime.record_triage(
        record.goal_plus_id,
        GoalPlusTriage(
            is_optimization=False,
            confidence="high",
            recommended_phase="goal",
            reasons=["qualitative documentation task"],
        ),
    )

    gate = runtime.gate(
        record.goal_plus_id,
        event="pre_tool_use",
        context={"tool_name": "edit"},
    )

    assert gate.decision == "allow"


@pytest.mark.parametrize("tool_name", ["search_start_agent_session", "search_continue_agent_session"])
def test_pre_tool_use_rejects_candidate_call_without_linked_target(tmp_path, tool_name) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    record = runtime.create_goal("Optimize kernel latency")
    runtime.record_triage(
        record.goal_plus_id,
        GoalPlusTriage(
            is_optimization=True,
            confidence="high",
            recommended_phase="search",
            identified_at="initial",
            reasons=["latency benchmark exists"],
        ),
    )

    gate = runtime.gate(
        record.goal_plus_id,
        event="pre_tool_use",
        context={"tool_name": tool_name},
    )

    assert gate.decision == "block"
    assert "GoalPlusOwnershipMismatch" in gate.reason


def test_pre_tool_use_accepts_camel_case_tool_name(tmp_path) -> None:
    runtime = FileGoalPlusRuntime(tmp_path / ".search")
    record = runtime.create_goal("Maybe optimize something")

    gate = runtime.gate(
        record.goal_plus_id,
        event="pre_tool_use",
        context={"toolName": "goal_plus_search_freeze_spec"},
    )

    assert gate.decision == "block"
    assert "GoalPlusOwnershipMismatch" in gate.reason
