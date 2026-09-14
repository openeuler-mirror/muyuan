"""Host pump policy for annotation and verifier queues."""

from types import SimpleNamespace

import pytest

from goal_plus import evidence_annotator, host_scope, internal_agents
from goal_plus import llm_verifier as verifier
from goal_plus.application.internal_agents import InternalAgentWork
from goal_plus.domain.search import RunState
from goal_plus.infrastructure.state.allocation import FileAllocationState
from goal_plus.infrastructure.state.files import write_json
from goal_plus.ports.state import RunRecord
from tests.test_annotation_admission import frozen_spec
from tests.test_annotation_workflow import Clock


@pytest.fixture
def pump(tmp_path, monkeypatch):
    clock = Clock()
    frozen = frozen_spec(clock, adaptive=True)
    store = FileAllocationState(tmp_path / ".gp")
    write_json(
        store.root_dir / "specs/frozen_test/frozen_spec.json",
        frozen.model_dump(mode="json"),
    )
    run = RunRecord(
        run_id="run_test",
        frozen_spec_id=frozen.frozen_spec_id,
        source_path=str(tmp_path),
        state=RunState.RUNNING,
        created_at=clock.timestamp(),
    )
    write_json(store.run_dir(run.run_id) / "run.json", run.model_dump(mode="json"))
    calls = []
    monkeypatch.setattr(
        host_scope, "run_accepts_internal_agent_launch", lambda **kwargs: True
    )
    monkeypatch.setattr(
        evidence_annotator,
        "kick_evidence_annotator",
        lambda root, run_id, *, agent_harness: calls.append(("annotation", run_id, agent_harness))
        or True,
    )
    monkeypatch.setattr(
        verifier,
        "kick_verifier",
        lambda root, run_id, *, agent_harness: calls.append(("verifier", run_id, agent_harness))
        or True,
    )
    monkeypatch.setattr(
        host_scope,
        "close_run_internal_agents",
        lambda **kwargs: calls.append(("cleanup", kwargs["run_id"])) or [],
    )
    return SimpleNamespace(
        store=store,
        work=InternalAgentWork(store),
        calls=calls,
        run=run,
        frozen=frozen,
    )


def json_state(pump):
    return {
        str(path): path.read_bytes()
        for path in pump.store.root_dir.rglob("*.json")
    }


def kick(pump, agent_harness=None):
    return internal_agents.kick_internal_agents(pump.store.root_dir, "run_test", agent_harness=agent_harness or pump.frozen.agent_harness)


@pytest.mark.parametrize("state", list(RunState))
def test_internal_work_query_is_read_only(pump, state):
    pump.run.state = state
    write_json(
        pump.store.run_dir("run_test") / "run.json",
        pump.run.model_dump(mode="json"),
    )
    before = json_state(pump)
    work = pump.work.inspect("run_test")
    assert work.accepts_work is (
        state
        in {
            RunState.RUNNING,
            RunState.WAITING_FOR_WORKERS,
            RunState.SELECTING,
            RunState.SELECTION_BLOCKED,
        }
    )
    assert work.agent_harness == pump.frozen.agent_harness
    assert work.verifier_enabled is True
    assert json_state(pump) == before


def test_host_callback_scans_annotation_and_verifier_queues(pump):
    assert kick(pump) == {
        "annotation_started": True,
        "verifier_started": True,
    }
    assert [item[0] for item in pump.calls] == ["annotation", "verifier"]


def test_foreign_host_is_rejected_without_mutation(pump):
    before = json_state(pump)
    with pytest.raises(RuntimeError, match="does not match"):
        kick(pump, "thinkthread")
    assert json_state(pump) == before
    assert pump.calls == []


def test_closed_scope_cannot_launch_work(pump, monkeypatch):
    monkeypatch.setattr(
        host_scope, "run_accepts_internal_agent_launch", lambda **kwargs: False
    )
    assert kick(pump) == {
        "annotation_started": False,
        "verifier_started": False,
    }
    assert pump.calls == []
