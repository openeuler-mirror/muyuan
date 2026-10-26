"""Durable fork acceptance shared by the two real ThinkThread Circle hosts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from goal_plus.execution.pi_checkpoint import active_ancestry, completed_checkpoint_prefix


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _native_entries(state_root: Path, root_id: str, child_id: str, agent_harness: str) -> list[dict]:
    session = state_root / "instances" / root_id / "thinkthreads" / child_id / "session"
    if agent_harness == "pi":
        path = session / "active.jsonl"
    else:
        paths = list(session.glob("**/rollout-*.jsonl"))
        if not paths:
            return []
        path = max(paths, key=lambda item: item.stat().st_mtime)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def native_preparation_context(
    state_root: Path, root_id: str, agent_harness: str, *, run_id: str | None = None,
) -> dict | None:
    """Capture the independently observed completed Main preparation turn."""
    entries = _native_entries(state_root, root_id, root_id, agent_harness)
    if agent_harness == "pi" and not entries:
        return None
    assert entries
    if agent_harness == "pi":
        leaf_id = entries[-1].get("id")
        ancestry = active_ancestry(entries[1:], leaf_id)
        marker_index = next(
            (index for index, entry in enumerate(ancestry)
             if entry.get("type") == "custom"
             and entry.get("customType") == "goal-plus-fork-checkpoint"
             and isinstance(entry.get("data"), dict)
             and entry["data"].get("run_id") == run_id),
            None,
        )
        if marker_index is None:
            return None
        # TT readiness can precede the final assistant stop.  Wait for the
        # extension's marker and independently check its preceding message.
        prior_message = next(
            (entry for entry in ancestry[marker_index + 1:] if entry.get("type") == "message"),
            None,
        )
        message = prior_message.get("message") if isinstance(prior_message, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not (
            isinstance(message, dict)
            and message.get("role") == "assistant"
            and message.get("stopReason") == "stop"
            and not (
                isinstance(content, list)
                and any(
                    isinstance(item, dict) and item.get("type") == "toolCall"
                    for item in content
                )
            )
        ):
            return None
        checkpoint_id, prefix = completed_checkpoint_prefix(
            entries, leaf_id, run_id=run_id, require_preparation_marker=True,
        )
    else:
        completed = [index for index, entry in enumerate(entries)
                     if entry.get("type") == "event_msg"
                     and entry.get("payload", {}).get("type") == "task_complete"]
        assert completed
        prefix = entries[:completed[-1] + 1]
        checkpoint_id = prefix[-1]["payload"]["turn_id"]
    return {"checkpoint_id": checkpoint_id, "entries": prefix}


def capture_fork_provider_evidence(
    runtime_root: Path, state_root: Path, root_id: str, agent_harness: str, context: dict,
) -> dict | None:
    """Read real TT Child/Fs state and native history while resources still exist."""
    run_paths = list((runtime_root / "runs").glob("*/run.json"))
    if len(run_paths) != 1:
        return None
    run = _read(run_paths[0])
    group = run.get("dispatch_group")
    if group is None or group["status"] != "started":
        return None
    assert group["checkpoint_id"] == context["checkpoint_id"]
    instance = state_root / "instances" / root_id
    provider = _read(instance / "state.json")
    assert provider["rootId"] == root_id
    main_entries = _native_entries(state_root, root_id, root_id, agent_harness)
    if agent_harness == "pi":
        # Native JSONL headers identify files/sessions; the effective Pi
        # history consists of the active branch's entries, without that header.
        main_entries = list(reversed(active_ancestry(main_entries[1:], main_entries[-1].get("id"))))
        assert main_entries[:len(context["entries"])] == context["entries"], (
            "Main active native prefix changed after preparation"
        )
    main_after_checkpoint = main_entries[len(context["entries"]):]
    if agent_harness == "pi":
        # The run marker records the boundary after the complete assistant
        # entry. It is metadata, and inheriting it does not inherit later Main
        # user/assistant/tool messages. All such later entries remain forbidden.
        main_after_checkpoint = [entry for entry in main_after_checkpoint if not (
            entry.get("type") == "custom"
            and entry.get("customType") == "goal-plus-fork-checkpoint"
            and isinstance(entry.get("data"), dict)
            and entry["data"].get("run_id") == run["run_id"]
        )]
    children = []
    for branch in group["targets"]:
        child_id = branch["provider_session_id"]
        native = _native_entries(state_root, root_id, child_id, agent_harness)
        if not native:
            return None
        prefix = context["entries"]
        active_native = native
        if agent_harness == "pi":
            native = native[1:]
            active_native = list(reversed(active_ancestry(native, native[-1].get("id"))))
        # The provider must have inherited the actual task, tool results and
        # completed boundary, not merely a new prompt naming the old session.
        assert active_native[:len(prefix)] == prefix, "Child active native prefix differs from preparation"
        assert not any(entry in main_after_checkpoint for entry in native), (
            "Child inherited Main entries beyond the selected completed checkpoint"
        )
        child = provider["thinkthreads"][child_id]
        assert child["parentId"] == root_id
        attachment = branch["binding"]["workspace_attachment"]
        assert child["fsBranchId"] == attachment["fs_branch_id"]
        fs_branch = provider["fsBranches"][child["fsBranchId"]]
        assert fs_branch["thinkthreadId"] == child_id
        assert fs_branch["baseSnapshotId"] == attachment["baseline_snapshot_id"]
        assert provider["childSpecs"][child_id]["fsPolicy"] == "private_branch"
        children.append({"child_id": child_id, "fs_branch_id": child["fsBranchId"]})
    assert len(children) == 2
    assert len({item["child_id"] for item in children}) == 2
    assert len({item["fs_branch_id"] for item in children}) == 2
    digest = hashlib.sha256(json.dumps(context["entries"], sort_keys=True).encode()).hexdigest()
    return {
        "checkpoint_id": context["checkpoint_id"],
        "native_prefix_sha256": digest,
        "native_prefix_entries": len(context["entries"]),
        "children": children,
    }


def assert_fork_prepared(runtime_root: Path, goal: dict) -> dict:
    """The real first Main turn prepared a batch and ended before creating branches."""
    assert goal["status"] == "blocked"
    [run_path] = (runtime_root / "runs").glob("*/run.json")
    run = _read(run_path)
    assert run.get("dispatch_group") is None
    candidates = [_read(path) for path in (run_path.parent / "candidates").glob("*/candidate.json")]
    assert {item["candidate_id"] for item in candidates} == {"c001", "c002"}
    assert all(not item["iterations"] for item in candidates)
    assert not list((run_path.parent / "agent_sessions").glob("agent_*.json"))
    frozen = _read(runtime_root / "specs" / run["frozen_spec_id"] / "frozen_spec.json")
    assert frozen["agent_harness"] in {"codex", "pi"}
    assert frozen["runtime_provider"] == "thinkthread"
    strategy = frozen["spec"]["strategy"]
    assert strategy["initial_context"] == "fork"
    assert strategy["inner_agent"] == "common"
    assert strategy["llm_validation"] == "required"
    return {
        "run_id": run["run_id"],
        "goal_revision": goal["goal_revision"],
        "main_session_id": goal["active_session"]["session_id"],
        "frozen_spec_id": run["frozen_spec_id"],
        "baseline_snapshot_id": run["source_artifact_ref"]["id"],
    }


def assert_fork_not_terminally_failed(
    runtime_root: Path, goal: dict, *, resumed: bool,
) -> None:
    """Fail the real-host watchdog as soon as fork infrastructure is terminal.

    A provider rejection is already a durable Search outcome.  Waiting for the
    external completion deadline after the Goal has entered
    ``host_repair_required`` only hides the provider failure behind a long
    timeout and leaves the host process alive.  The preparation checkpoint is
    intentionally excluded: it is a valid blocked state before the resume
    turn starts.
    """
    if not resumed or goal.get("status") != "blocked":
        return
    run_paths = list((runtime_root / "runs").glob("*/run.json"))
    if len(run_paths) != 1:
        return
    run = _read(run_paths[0])
    group = run.get("dispatch_group")
    if not isinstance(group, dict):
        return
    if group.get("status") not in {"partial", "failed"}:
        return
    failure = group.get("failure") or "provider dispatch group failed"
    raise AssertionError(
        "ThinkThread fork reached a terminal provider failure instead of making "
        f"progress: status={group.get('status')!r}, failure={failure!r}; "
        f"inspect {run_paths[0]} and the host resource journals"
    )


def assert_fork_evidence(
    runtime_root: Path,
    run_dir: Path,
    *,
    root_id: str,
    agent_harness: str,
    preparation: dict,
    provider_evidence: dict,
) -> dict:
    """A completed group retains exact native/Fs identity through delivery and audit."""
    run = _read(run_dir / "run.json")
    assert run["run_id"] == preparation["run_id"]
    group = run["dispatch_group"]
    assert group["runtime_provider"] == "thinkthread"
    assert group["main_session_id"] == preparation["main_session_id"]
    assert group["parent_native_session_id"] == root_id
    assert group["frozen_spec_id"] == preparation["frozen_spec_id"]
    assert group["checkpoint_id"] != "latest_completed"
    assert group["checkpoint_id"] == provider_evidence["checkpoint_id"]
    assert group["checkpoint_kind"] in {"completed_turn", "entry"}
    assert group["llm_validation"] == "required"
    assert group["status"] == "closed"
    assert len(group["targets"]) == 2
    branches = sorted(group["targets"], key=lambda item: item["dispatch_target_index"])
    assert [item["dispatch_target_index"] for item in branches] == [1, 2]
    assert {item["candidate_id"] for item in branches} == {"c001", "c002"}
    child_ids, fs_branches, requests, projections = set(), set(), set(), set()
    observations = []
    for branch in branches:
        binding = branch["binding"]
        child_id = branch["provider_session_id"]
        assert child_id and child_id != root_id
        assert branch["launch_state"] == "started"
        assert branch["closed_at"]
        assert branch["audit_state"] == "completed" and branch["audit_record_id"]
        for key in ("runtime_provider", "main_session_id", "parent_native_session_id",
                    "checkpoint_id", "checkpoint_kind", "frozen_spec_id", "dispatch_group_id"):
            assert binding[key] == group[key]
        assert binding["provider_session_id"] == child_id
        attachment = binding["workspace_attachment"]
        assert attachment["provider"] == "thinkthread"
        assert attachment["root_thinkthread_id"] == root_id
        assert attachment["baseline_snapshot_id"] == preparation["baseline_snapshot_id"]
        receipt = binding["launch_receipt"]
        assert receipt["runtime_provider"] == "thinkthread"
        assert receipt["launch_state"] == "started"
        assert receipt["provider_session_id"] == child_id
        assert receipt["parent_native_session_id"] == root_id
        assert receipt["checkpoint_id"] == group["checkpoint_id"]
        assert receipt["checkpoint_kind"] == group["checkpoint_kind"]
        assert receipt["workspace_attachment"] == attachment
        assert receipt["request_id"]
        assert receipt["profile"]
        assert receipt["provider_capabilities"]
        assert receipt["context_projection_digest"] == binding["context_projection_digest"]
        assert binding["context_projection_digest"]
        session = _read(run_dir / "agent_sessions" / f"{branch['agent_session_id']}.json")
        assert session["agent_harness"] == agent_harness
        assert session["runtime_provider"] == "thinkthread"
        assert session["candidate_origin"] == binding
        assert session["session_handle"]["external_id"] == child_id
        assert session["session_handle"]["metadata"]["fs_branch_id"] == attachment["fs_branch_id"]
        [resource_path] = (runtime_root / "host-scopes").glob(
            f"*/thinkthread-sessions/{branch['agent_session_id']}.json"
        )
        resource = _read(resource_path)
        assert resource["thinkthread_id"] == child_id
        assert resource["fs_branch_id"] == attachment["fs_branch_id"]
        assert resource["cleanup_state"] == "closed"
        provenance = resource["dispatch_provenance"]
        assert provenance["session_derivation"] == "fork_current_checkpoint"
        assert provenance["request_id"] == receipt["request_id"]
        assert provenance["checkpoint_id"] == group["checkpoint_id"]
        assert provenance["context_projection_digest"] == binding["context_projection_digest"]
        assert resource["fork_intent"]["state"] == "bound"
        candidate = _read(run_dir / "candidates" / branch["candidate_id"] / "candidate.json")
        assert candidate["task"]["candidate_origin"] == binding
        assert len(candidate["iterations"]) == 2
        assert candidate["iterations"][1]["score"] > candidate["iterations"][0]["score"]
        for iteration in candidate["iterations"]:
            submission = iteration["verifier_submission"]
            audit = submission["input"]["dispatch_audit_context"]
            assert audit["checkpoint_id"] == group["checkpoint_id"]
            assert audit["checkpoint_kind"] == group["checkpoint_kind"]
            assert audit["parent_native_session_id"] == root_id
            assert audit["provider_session_id"] == child_id
            assert audit["settlement_id"] == iteration["settlement_id"]
            assert audit["iteration"] == iteration["iteration"]
            assert audit["context_projection_digest"] == binding["context_projection_digest"]
            records = [
                _read(path) for path in (run_dir / "verifier" / "records").glob("record_*.json")
            ]
            [record] = [item for item in records if item["subject"] == submission["subject"]]
            assert record["outcome"] == "completed"
            assert record["quality_score"] is not None
            if iteration["iteration"] == 2:
                assert record["record_id"] == branch["audit_record_id"]
        child_ids.add(child_id)
        fs_branches.add(attachment["fs_branch_id"])
        requests.add(receipt["request_id"])
        projections.add(binding["context_projection_digest"])
        observations.append({
            "candidate_id": branch["candidate_id"], "child_id": child_id,
            "fs_branch_id": attachment["fs_branch_id"], "request_id": receipt["request_id"],
            "audit_record_id": branch["audit_record_id"],
        })
    assert len(child_ids) == len(fs_branches) == len(requests) == 2
    assert len(projections) == 1
    assert {item["child_id"] for item in provider_evidence["children"]} == child_ids
    assert {item["fs_branch_id"] for item in provider_evidence["children"]} == fs_branches
    return {
        **preparation,
        "root_thinkthread_id": root_id,
        "checkpoint_id": group["checkpoint_id"],
        "checkpoint_kind": group["checkpoint_kind"],
        "context_projection_digest": projections.pop(),
        "provider_evidence": provider_evidence,
        "targets": observations,
    }
