"""Canonical Goal decoding and durable ownership indexes are read-only."""

import json

import pytest

from goal_plus.application.goal_authorization import GoalAuthorization
from goal_plus.domain.goal import GoalPlusLinkedSearch, GoalPlusRecord
from goal_plus.infrastructure.state.files import write_json
from goal_plus.infrastructure.state.goal import FileGoalQueries


STAMP = "2026-09-05T00:00:00Z"


def snapshot(root):
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_absent_query_root_is_not_created(tmp_path):
    root = tmp_path / ".gp"
    source = FileGoalQueries(root)
    assert source.goal_ids() == []
    assert source.session_run_ids("session") == []
    assert source.pool_run_id("pool") is None
    assert source.read_events("gp_0001") == []
    assert not root.exists()


def test_goal_history_preserves_canonical_record_precedence_over_events(tmp_path):
    record = GoalPlusRecord(
        goal_plus_id="gp_0001",
        raw_goal="History fixture",
        created_at=STAMP,
        updated_at=STAMP,
        search_tasks=[GoalPlusLinkedSearch(run_id="run-a", summary="record")],
        linked_search=GoalPlusLinkedSearch(run_id="run-a", summary="current"),
    )
    folder = tmp_path / "goal-plus" / record.goal_plus_id
    write_json(folder / "goal.json", record.model_dump(mode="json"))
    events = [
        {
            "event_type": "search_linked",
            "created_at": STAMP,
            "payload": {
                "run_id": "run-a",
                "frozen_spec_id": "spec-a",
                "summary": "event",
            },
        },
        {
            "event_type": "search_result_recorded",
            "created_at": STAMP,
            "payload": {
                "run_id": "run-b",
                "report_path": "report-b",
                "summary": "other",
            },
        },
        {"event_type": "gate_allowed", "payload": {"run_id": "not-a-link"}},
    ]
    (folder / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n"
    )
    before = snapshot(tmp_path)
    source = FileGoalQueries(tmp_path)
    loaded = source.read_goal(record.goal_plus_id)
    assert [task.run_id for task in loaded.search_tasks] == ["run-b", "run-a"]
    assert record.linked_search.summary == "record"
    assert loaded.linked_search.summary == "record"
    assert loaded.linked_search.frozen_spec_id == "spec-a"
    assert loaded.linked_search.linked_at == STAMP
    assert loaded.search_tasks[0].result_recorded_at == STAMP
    assert source.read_events(record.goal_plus_id) == events
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("status", ["blocked", "complete", "abandoned"])
def test_legacy_terminal_import_keeps_historical_search_authority_without_writes(
    tmp_path, status
):
    record = GoalPlusRecord(
        goal_plus_id="gp_0001",
        raw_goal="Legacy fixture",
        created_at=STAMP,
        updated_at=STAMP,
        status=status,
        search_tasks=[GoalPlusLinkedSearch(run_id="old-run")],
    )
    payload = record.model_dump(mode="json", exclude={"control"})
    payload["work_items"] = [
        {"route": "main", "status": "planned"},
        {
            "title": "old title",
            "route": "subagent",
            "status": "blocked",
            "work_item_id": "dispatch-a",
            "objective": "Legacy work",
            "goal_revision": 1,
            "created_at": STAMP,
            "updated_at": STAMP,
        },
    ]
    write_json(tmp_path / "goal-plus" / record.goal_plus_id / "goal.json", payload)
    before = snapshot(tmp_path)
    source = FileGoalQueries(tmp_path)
    loaded = source.read_goal(record.goal_plus_id)
    assert loaded.control.state == "closed"
    assert [(item.work_item_id, item.status) for item in loaded.work_items] == [
        ("dispatch-a", "failed")
    ]
    authorization = GoalAuthorization(source)
    assert authorization.nonterminal_records() == []
    assert authorization.run_owner("old-run") == record.goal_plus_id
    with pytest.raises(RuntimeError, match="GoalPlusResumeRequired"):
        authorization.require_search_execution("old-run", settlement=True)
    assert snapshot(tmp_path) == before


def test_session_index_uses_exact_durable_filename_and_does_not_guess_on_collision(
    tmp_path,
):
    for run in ("run-a", "run-b"):
        write_json(tmp_path / "runs" / run / "agent_sessions" / "shared.json", {})
    write_json(
        tmp_path / "runs" / "run-a" / "agent_sessions" / "shared-suffix.json", {}
    )
    before = snapshot(tmp_path)
    source = FileGoalQueries(tmp_path)
    assert sorted(source.session_run_ids("shared")) == ["run-a", "run-b"]
    assert source.session_run_ids("missing") == []
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ('{"run_id": "run-a"}', "run-a"),
        ('{"run_id": 5}', None),
        ("{}", None),
        ("{", None),
    ],
)
def test_pool_index_preserves_invalid_or_missing_reference_behavior(
    tmp_path, payload, expected
):
    folder = tmp_path / "host-pools" / "pi" / "pool-a"
    folder.mkdir(parents=True)
    (folder / "pool.json").write_text(payload)
    before = snapshot(tmp_path)
    source = FileGoalQueries(tmp_path)
    assert source.pool_run_id("pool-a") == expected
    assert source.pool_run_id("pool") is None
    assert snapshot(tmp_path) == before
