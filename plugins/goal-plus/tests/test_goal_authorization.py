"""Goal authorization through typed inputs, without a runtime or model process."""

from dataclasses import dataclass, field

import pytest

from goal_plus.application.goal_authorization import GoalAuthorization
from goal_plus.domain.goal import (
    GoalPlusActiveSession,
    GoalPlusCommandConfig,
    GoalPlusControl,
    GoalPlusHostCommandInvocation,
    GoalPlusLinkedSearch,
    GoalPlusRecord,
)
from goal_plus.domain.goal_authorization import require_control_session
from goal_plus.domain.goal_authorization import tool_matches
from goal_plus.domain.goal_authorization import validate_search_command_config
from goal_plus.domain.search import SearchSpec
from goal_plus.ports.goal_authorization import GoalToolTarget


STAMP = "2026-09-05T00:00:00Z"


@pytest.mark.parametrize("workers,count,accepted", [
    ("model-a", None, True),
    ("model-a", 3, True),
    ("model-a*3", 3, True),
    ("model-a*3", 2, False),
    ("model-a*3", None, False),
    ("model-b", 3, False),
])
def test_worker_command_only_constrains_explicit_counts(workers, count, accepted):
    config = GoalPlusCommandConfig(max_parallel=3, workers=workers, worker_directive="workers")
    spec = SearchSpec(
        objective="maximize score", metric_name="score", metric_direction="maximize",
        source_path=".", edit_surface={"allow": ["candidate.py"]},
        budget={"max_parallel": 3},
        process_verifiers=[{"name": "score", "role": "ranking_signal", "command": ["python", "evaluator.py"]}],
        strategy={"models": [{"model": "model-a", "count": count}]},
    )
    before = spec.model_dump_json()
    if accepted:
        validate_search_command_config(config, spec)
    else:
        with pytest.raises(ValueError, match="workers="):
            validate_search_command_config(config, spec)
    assert spec.model_dump_json() == before


@pytest.mark.parametrize("requested, configured, accepted", [
    ("fork", "fork", True),
    ("fork", "fresh", False),
])
def test_spawn_mode_command_constrains_search_spec(requested, configured, accepted):
    config = GoalPlusCommandConfig(spawn_mode=requested)
    spec = SearchSpec(
        objective="maximize score", metric_name="score", metric_direction="maximize",
        source_path=".", edit_surface={"allow": ["candidate.py"]},
        budget={"max_parallel": 2},
        process_verifiers=[{"name": "score", "role": "ranking_signal", "command": ["python", "evaluator.py"]}],
        strategy={"initial_context": configured},
    )
    if accepted:
        validate_search_command_config(config, spec)
    else:
        with pytest.raises(ValueError, match="spawn_mode=fork"):
            validate_search_command_config(config, spec)


@pytest.mark.parametrize("action", ["goal_plus_search_create", "goal_plus_search_prepare_recovery", "goal_plus_gate", "bash"])
def test_tool_matching_preserves_foreign_registration(action):
    assert tool_matches(action, action)  # Dedicated internal dispatcher.
    assert tool_matches(f"mcp__goal_plus__{action}", action)
    assert tool_matches(f"mcp__goal-plus__{action}", action)
    assert not tool_matches(f"mcp__foreign__{action}", action)
    assert not tool_matches(f"foreign.{action}", action)
    assert not tool_matches(f"mcp__goal_plus_extra__{action}", action)


def invocation(nonce="start-a", *, agent_harness="codex", session="main-a", action="start"):
    return GoalPlusHostCommandInvocation(
        agent_harness=agent_harness,
        transport="pi_extension_command"
        if agent_harness == "pi"
        else "codex_user_prompt_submit",
        command="/goal-plus" if agent_harness == "pi" else "$goal-plus",
        action=action,
        session_id=session,
        invocation_id=nonce,
        raw_input_sha256="a" * 64,
        invoked_at=STAMP,
    )


def goal(goal_id="gp_0001", **updates):
    return GoalPlusRecord(
        goal_plus_id=goal_id,
        raw_goal="Independent authorization fixture",
        created_at=STAMP,
        updated_at=STAMP,
        host_command_invocations=[invocation()],
        search_tasks=[GoalPlusLinkedSearch(run_id="run-a")],
    ).model_copy(update=updates, deep=True)


@dataclass
class MemoryGoals:
    records: dict[str, GoalPlusRecord] = field(default_factory=dict)
    sessions: dict[str, list[str]] = field(default_factory=dict)
    events: dict[str, list[dict[str, object]]] = field(default_factory=dict)

    def goal_ids(self):
        return list(self.records)

    def read_goal(self, goal_plus_id):
        try:
            return self.records[goal_plus_id]
        except KeyError as exc:
            raise FileNotFoundError(goal_plus_id) from exc

    def session_run_ids(self, agent_session_id):
        return self.sessions.get(agent_session_id, [])

    def read_events(self, goal_plus_id):
        return self.events.get(goal_plus_id, [])


@pytest.fixture
def authorization():
    source = MemoryGoals(
        records={
            "gp_0002": goal(
                "gp_0002", search_tasks=[GoalPlusLinkedSearch(run_id="run-b")]
            ),
            "gp_0001": goal(),
        },
        sessions={"session-a": ["run-a"], "ambiguous": ["run-a", "run-b"]},
    )
    before = {key: record.model_dump_json() for key, record in source.records.items()}
    yield GoalAuthorization(source)
    assert {
        key: record.model_dump_json() for key, record in source.records.items()
    } == before


@pytest.mark.parametrize(
    ("inputs", "expected"),
    [
        (
            {"run_id": "run-a", "agent_session_id": "ambiguous"},
            "gp_0001",
        ),
        ({"agent_session_id": "session-a"}, "gp_0001"),
    ],
)
def test_durable_targets_resolve_without_a_unique_active_goal(
    authorization, inputs, expected
):
    record = authorization.require_active_target(
        GoalToolTarget.from_input("goal_plus_search_select", inputs)
    )
    assert record.goal_plus_id == expected


@pytest.mark.parametrize(
    "inputs",
    [
        {},
        {"agent_session_id": "ambiguous"},
        {"agent_session_id": "ambiguous", "pool_id": "pool-b"},
        {"pool_id": "pool-b"},
        {"run_id": "standalone"},
        {"pool_id": "missing"},
        {"run_id": 1},
        {"goal_plus_id": "gp_0001", "run_id": "run-b"},
        {"goal_plus_id": "missing", "run_id": "run-a"},
    ],
)
def test_unknown_ambiguous_and_foreign_targets_are_rejected(authorization, inputs):
    with pytest.raises(RuntimeError, match="GoalPlusOwnershipMismatch"):
        authorization.require_active_target(
            GoalToolTarget.from_input("goal_plus_search_select", inputs)
        )


@pytest.mark.parametrize("tool", ["goal_plus_search_freeze_spec", "goal_plus_search_create"])
@pytest.mark.parametrize("prefix", ["", "mcp__goal_plus__", "mcp__goal-plus__"])
def test_bootstrap_requires_explicit_goal_even_with_a_unique_owner(tool, prefix):
    service = GoalAuthorization(MemoryGoals(records={"gp_0001": goal()}))
    assert service.require_unique_active().goal_plus_id == "gp_0001"
    for inputs in ({}, {"run_id": "run-a"}):
        with pytest.raises(RuntimeError, match="GoalPlusOwnershipMismatch"):
            service.require_active_target(
                GoalToolTarget.from_input(prefix + tool, inputs)
            )
    assert (
        service.require_active_target(
            GoalToolTarget.from_input(prefix + tool, {"goal_plus_id": "gp_0001"})
        ).goal_plus_id
        == "gp_0001"
    )


@pytest.mark.parametrize(
    ("source", "allowed"),
    [
        (None, True),
        ("run-a", True),
        ("run-b", False),
        ("", False),
        (42, False),
    ],
)
def test_successor_create_validates_non_none_source(authorization, source, allowed):
    target = GoalToolTarget.from_input(
        "goal_plus_search_create",
        {
            "goal_plus_id": "gp_0001",
            "source_run_id": source,
        },
    )
    if allowed:
        assert authorization.require_active_target(target).goal_plus_id == "gp_0001"
    else:
        with pytest.raises(RuntimeError, match="GoalPlusOwnershipMismatch"):
            authorization.require_active_target(target)


def test_target_normalization_does_not_silently_strip_identity():
    target = GoalToolTarget.from_input(
        "goal_plus_search_create",
        {
            "goal_plus_id": " gp_0001 ",
            "run_id": "",
            "agent_session_id": False,
            "source_run_id": 42,
        },
    )
    assert target.goal_plus_id == " gp_0001 "
    assert target.run_id is None and target.agent_session_id is None
    assert target.source_run_id is None and target.source_run_requested


def test_direct_typed_successor_reference_requires_ownership(authorization):
    target = GoalToolTarget("goal_plus_search_create", goal_plus_id="gp_0001", source_run_id="run-b")
    with pytest.raises(RuntimeError, match="GoalPlusOwnershipMismatch"):
        authorization.require_active_target(target)


@pytest.mark.parametrize("blocked_at,accepted", [
    ("2026-09-05T00:00:02Z", True),
    ("2026-09-05T00:00:01Z", False),
    ("2026-09-05T00:00:00Z", False),
    (None, False),
])
def test_fork_implicit_checkpoint_requires_post_batch_blocked_turn(blocked_at, accepted):
    source = MemoryGoals(
        records={"gp_0001": goal()},
        events={"gp_0001": [
            {"event_type": "search_linked", "payload": {"run_id": "run-a"}},
            {"event_type": "status_changed", "event_id": "event-prepared",
             "created_at": blocked_at, "payload": {"status": "blocked"}},
        ]},
    )
    authorization = GoalAuthorization(source)
    if accepted:
        preparation = authorization.require_fork_checkpoint_preparation(
            "run-a", batch_started_at="2026-09-05T00:00:01Z",
        )
        assert (preparation.goal_plus_id, preparation.run_id, preparation.event_id, preparation.created_at) == (
            "gp_0001", "run-a", "event-prepared", blocked_at,
        )
    else:
        with pytest.raises(RuntimeError, match="ForkPreparationRequired"):
            authorization.require_fork_checkpoint_preparation(
                "run-a", batch_started_at="2026-09-05T00:00:01Z",
            )

    source.events["gp_0001"] = [source.events["gp_0001"][0]]
    with pytest.raises(RuntimeError, match="ForkPreparationRequired"):
        authorization.require_fork_checkpoint_preparation(
            "run-a", batch_started_at="2026-09-05T00:00:01Z",
        )


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"status": "blocked"}, "GoalPlusResumeRequired.*status=blocked"),
        ({"status": "complete"}, "GoalPlusTerminal"),
        (
            {"control": GoalPlusControl(state="paused")},
            "GoalPlusResumeRequired.*reason=explicit_pause",
        ),
        ({"entrypoint_status": "legacy_unverified"}, "GoalPlusEntrypointRequired"),
        ({"host_command_invocations": []}, "GoalPlusEntrypointRequired"),
    ],
)
def test_target_requires_active_verified_goal(updates, reason):
    record = goal(**updates)
    before = record.model_dump_json()
    service = GoalAuthorization(MemoryGoals(records={record.goal_plus_id: record}))
    with pytest.raises(RuntimeError, match=reason):
        service.require_active_target(
            GoalToolTarget("goal_plus_search_freeze_spec", goal_plus_id="gp_0001")
        )
    assert record.model_dump_json() == before


def test_missing_explicit_goal_preserves_entrypoint_error_when_no_nonterminal_records():
    service = GoalAuthorization(
        MemoryGoals(records={"gp_0001": goal(status="complete")})
    )
    with pytest.raises(RuntimeError, match="GoalPlusEntrypointRequired.*none"):
        service.require_active_target(
            GoalToolTarget("goal_plus_search_create", goal_plus_id="missing")
        )


@pytest.mark.parametrize(
    ("tool", "allowed"),
    [
        ("goal_plus_search_recovery_status", False),
        ("goal_plus_search_prepare_recovery", False),
        ("goal_plus_search_recover_candidate", False),
        ("goal_plus_search_get_global_evidence", True),
        ("goal_plus_search_create", True),
    ],
)
def test_historical_ownership_does_not_authorize_current_revision_recovery(
    tool, allowed
):
    record = goal(goal_revision=2)
    before = record.model_dump_json()
    service = GoalAuthorization(MemoryGoals(records={record.goal_plus_id: record}))
    target = GoalToolTarget.from_input(
        tool,
        {
            "goal_plus_id": record.goal_plus_id,
            "run_id": "run-a",
            "source_run_id": "run-a",
        },
    )
    error = service.ownership_error(record, target)
    assert (error is None) == allowed
    assert service.run_owner("run-a") == record.goal_plus_id
    assert record.model_dump_json() == before


@pytest.mark.parametrize(
    ("status", "control", "revision", "work", "settlement"),
    [
        ("active", "active", 1, True, True),
        ("active", "paused", 1, False, True),
        ("blocked", "active", 1, False, True),
        ("needs_user", "paused", 1, False, True),
        ("active", "closed", 1, False, False),
        ("complete", "active", 1, False, False),
        ("abandoned", "paused", 1, False, False),
        ("active", "active", 2, False, False),
        ("active", "paused", 2, False, False),
    ],
)
def test_execution_gate_preserves_settlement_and_historical_link_rules(
    status,
    control,
    revision,
    work,
    settlement,
):
    record = goal(
        status=status, control=GoalPlusControl(state=control), goal_revision=revision
    )
    before = record.model_dump_json()
    service = GoalAuthorization(MemoryGoals(records={record.goal_plus_id: record}))
    for finishing, allowed in ((False, work), (True, settlement)):
        if allowed:
            service.require_search_execution("run-a", settlement=finishing)
        else:
            with pytest.raises(
                RuntimeError, match="GoalPlus(?:ResumeRequired|Terminal)"
            ):
                service.require_search_execution("run-a", settlement=finishing)
        service.require_search_execution("standalone", settlement=finishing)
    assert record.model_dump_json() == before


@pytest.mark.parametrize("status", ["active", "blocked", "needs_user"])
def test_start_conflicts_are_scoped_to_host_and_session(status):
    record = goal(status=status, control=GoalPlusControl(state="paused"))
    before = record.model_dump_json()
    service = GoalAuthorization(MemoryGoals(records={record.goal_plus_id: record}))
    assert service.require_start(invocation()).goal_plus_id == record.goal_plus_id
    with pytest.raises(RuntimeError, match="native host session already owns"):
        service.require_start(invocation("new-request"))
    assert service.require_start(invocation("pi-request", agent_harness="pi")) is None
    assert service.require_start(invocation("other-session", session="main-b")) is None
    assert record.model_dump_json() == before


def test_invocation_replay_prefers_nonterminal_then_latest_historical_record():
    records = {"gp_0002": goal("gp_0002", status="complete"), "gp_0001": goal()}
    service = GoalAuthorization(MemoryGoals(records=records))
    assert service.record_for_invocation("start-a").goal_plus_id == "gp_0001"
    records["gp_0001"] = goal(status="abandoned")
    assert service.record_for_invocation("start-a").goal_plus_id == "gp_0002"
    assert service.require_start(invocation()) is None
    assert service.record_for_invocation("missing") is None


@pytest.mark.parametrize(
    ("session_state", "status", "action", "allowed"),
    [
        ("attached", "active", "pause", True),
        ("stale", "active", "summary", False),
        ("detached", "active", "resume", False),
        ("detached", "active", "summary", True),
        ("detached", "complete", "edit", True),
        ("detached", "active", "edit", False),
    ],
)
@pytest.mark.parametrize("agent_harness", ["codex", "pi"])
def test_control_requires_original_session_with_narrow_historical_access(
    session_state,
    status,
    action,
    allowed,
    agent_harness,
):
    record = goal(
        status=status,
        active_session=GoalPlusActiveSession(
            agent_harness="pi" if agent_harness == "pi" else agent_harness,
            session_id="main-a",
            state=session_state,
            attached_at=STAMP,
            last_seen_at=STAMP,
        ),
    )
    before = record.model_dump_json()
    command = invocation(agent_harness=agent_harness, action=action)
    if allowed:
        require_control_session(record, command)
    else:
        with pytest.raises(RuntimeError, match="attached original native session"):
            require_control_session(record, command)
    with pytest.raises(RuntimeError, match="attached original native session"):
        require_control_session(
            record, invocation(agent_harness=agent_harness, action=action, session="foreign")
        )
    assert record.model_dump_json() == before


@pytest.mark.parametrize("agent_harness", ["codex", "pi"])
def test_main_search_session_control_rechecks_owner_and_execution(agent_harness):
    record = goal(
        host_command_invocations=[invocation(agent_harness=agent_harness)],
        active_session=GoalPlusActiveSession(
            agent_harness="pi" if agent_harness == "pi" else "codex", session_id="main-a",
            state="attached", attached_at=STAMP, last_seen_at=STAMP,
        ),
    )
    source = MemoryGoals(records={record.goal_plus_id: record})
    authorization = GoalAuthorization(source)

    def check(**kwargs):
        return authorization.require_main_search_session(
            kwargs.pop("run_id", "run-a"),
            **{"agent_harness": agent_harness, "session_id": "main-a", "execution": True, **kwargs},
        )

    before = record.model_dump_json()
    assert check() == record
    for changes in (
        {"session_id": "peer"}, {"agent_harness": "pi" if agent_harness == "codex" else "codex"},
        {"run_id": "unlinked"},
    ):
        for execution in (False, True):
            with pytest.raises(PermissionError):
                check(**changes, execution=execution)
    assert record.model_dump_json() == before

    for changes in (
        {"control": GoalPlusControl(state="paused")},
        {"goal_revision": record.goal_revision + 1},
        {"active_session": record.active_session.model_copy(update={"state": "detached"})},
    ):
        stopped = record.model_copy(update=changes, deep=True)
        source.records[record.goal_plus_id] = stopped
        before = stopped.model_dump_json()
        assert check(execution=False) == stopped
        with pytest.raises((RuntimeError, PermissionError)):
            check()
        assert stopped.model_dump_json() == before
