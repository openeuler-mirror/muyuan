import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("session_audit", Path(__file__).resolve().parents[1] / "scripts/check_session_instructions.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
audit_session = module.audit_session
OVERVIEW = "Goal Plus shared workflow"


def audit(tmp_path, records, agent_harness="codex", **kwargs):
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(item) for item in records) + "\n")
    return audit_session(path, agent_harness=agent_harness, overview=OVERVIEW, **kwargs)


def message(role, text):
    return {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}


def response(role, text):
    return {"type": "response_item", "payload": message(role, text)}


def compact(history):
    return {"type": "compacted", "payload": {"replacement_history": history, "guardian_history": [message("developer", OVERVIEW)]}}


def test_codex_archives_and_echoes_do_not_prove_preservation(tmp_path):
    report = audit(tmp_path, [response("developer", OVERVIEW), response("assistant", "done"),
        compact([message("user", OVERVIEW)]), response("assistant", OVERVIEW)])
    assert report["status"] == "missing"
    assert [item["status"] for item in report["checks"]] == ["present", "missing"]


def test_codex_checks_every_compaction_and_reinjected_instructions(tmp_path):
    report = audit(tmp_path, [compact([message("developer", OVERVIEW)]), response("assistant", "ok"),
        compact([]), response("developer", "prefix " + OVERVIEW), response("assistant", "ok")])
    assert report["status"] == "present"
    assert report["compactions"] == 2
    assert {item["compaction"] for item in report["checks"]} == {1, 2}


@pytest.mark.parametrize("records", [
    [response("assistant", OVERVIEW)],
    [{"type": "compacted", "payload": {}}, response("assistant", OVERVIEW)],
    [compact([message("developer", OVERVIEW)])],
])
def test_codex_incomplete_evidence_is_unavailable(tmp_path, records):
    assert audit(tmp_path, records)["status"] == "unavailable"


def observation(entry_id, parent_id, *, present=True, digest=None, session="pi-test"):
    return {"type": "custom", "customType": module.OBSERVATION_TYPE, "id": entry_id, "parentId": parent_id,
        "data": {"version": 1, "session_id": session, "turn_index": 0,
            "expected_sha256": digest or hashlib.sha256(OVERVIEW.encode()).hexdigest(), "overview_present": present}}


def pi_message(entry_id, parent_id):
    return {"type": "message", "id": entry_id, "parentId": parent_id, "message": {"role": "assistant", "content": OVERVIEW}}


def injection(entry_id, parent_id, data):
    return {"type": "custom", "customType": module.INJECTION_OBSERVATION_TYPE, "id": entry_id,
            "parentId": parent_id, "data": {"version": 1, "session_id": "pi-test", **data}}


def delivered(entry_id, parent_id, custom_type):
    return {"type": "custom_message", "customType": custom_type, "id": entry_id,
            "parentId": parent_id, "content": "delivered", "display": False, "details": {}}


def usage_message(entry_id, parent_id, usage):
    return {"type": "message", "id": entry_id, "parentId": parent_id,
            "message": {"role": "assistant", "content": [], "stopReason": "stop", "usage": usage}}


def test_pi_injection_ledger_rolls_up_tokens_and_downgrades_missing_raw_goal(tmp_path):
    records = [{"type": "session", "id": "pi-test"}, observation("a", None),
        usage_message("b", "a", {"input": 90000, "cacheRead": 10000, "cacheWrite": 1}),
        injection("c", "b", {"kind": "goal_context_full", "role": "main", "channel": "system_message",
                             "est_tokens": 407, "chars": 812, "raw_goal_present": False}),
        delivered("d", "c", "goal-plus-native-context"),
        injection("e", "d", {"kind": "goal_context_brief", "role": "main", "channel": "system_message",
                             "est_tokens": 31, "chars": 62, "raw_goal_present": False}),
        delivered("f", "e", "goal-plus-native-context")]
    report = audit(tmp_path, records, "pi")
    ledger = report["injection_ledger"]
    assert ledger["status"] == "observed"
    assert [entry["context_tokens_at_injection"] for entry in ledger["entries"]] == [100001, 100001]
    assert ledger["summary"] == [
        {"role": "main", "kind": "goal_context_brief", "count": 1, "est_tokens": 31,
         "raw_goal_present": {"true": 0, "false": 1}},
        {"role": "main", "kind": "goal_context_full", "count": 1, "est_tokens": 407,
         "raw_goal_present": {"true": 0, "false": 1}},
    ]
    assert ledger["errors"] == [{"line": 4, "reason": "full_context_without_raw_goal"}]
    # The overview audit itself passed; the injection defect alone downgrades it.
    assert report["status"] == "missing"
    assert report["checks"] == [{"line": 3, "compaction": 0, "status": "present", "reason": "host_system_prompt_match"}]


def test_pi_injection_ledger_healthy_cadence_stays_present(tmp_path):
    records = [{"type": "session", "id": "pi-test"}, observation("a", None),
        usage_message("b", "a", {"input": 100, "cacheRead": 0, "cacheWrite": 0}),
        injection("c", "b", {"kind": "goal_context_full", "role": "main", "est_tokens": 900,
                             "raw_goal_present": True}),
        delivered("d", "c", "goal-plus-native-context"),
        injection("e", "d", {"kind": "goal_context_brief", "role": "main", "est_tokens": 31,
                             "raw_goal_present": False}),
        delivered("f", "e", "goal-plus-native-context")]
    report = audit(tmp_path, records, "pi")
    assert report["status"] == "present"
    assert report["injection_ledger"]["errors"] == []
    assert report["injection_ledger"]["summary"][1]["raw_goal_present"] == {"true": 1, "false": 0}


def test_pi_injection_ledger_flags_stale_continuation_and_absent_delivery(tmp_path):
    records = [{"type": "session", "id": "pi-test"},
        injection("a", None, {"kind": "goal_context_skipped", "role": "main", "reason": "continuation_turn"}),
        injection("b", "a", {"kind": "must_continue", "role": "main", "est_tokens": 200}),
        delivered("c", "b", "goal-plus-must-continue"),
        # An input-transform injection becomes the user prompt itself: no
        # custom message exists to find, so it must not be presence-checked.
        injection("d", "c", {"kind": "goal_start", "role": "main", "channel": "input_transform", "est_tokens": 700}),
        injection("e", "d", {"kind": "worker_continuation", "role": "worker", "est_tokens": 90}),
        delivered("f", "e", "goal-plus-worker-continuation"),
        injection("g", "f", {"kind": "tt_missing_attempt", "role": "worker", "est_tokens": 80})]
    ledger = audit(tmp_path, records, "pi")["injection_ledger"]
    assert {item["reason"] for item in ledger["errors"]} == {
        "goal_context_skipped_on_prompt_turn", "registered_injection_absent"}
    absent = next(item for item in ledger["errors"] if item["reason"] == "registered_injection_absent")
    assert absent == {"line": 8, "reason": "registered_injection_absent"}


def test_pi_injection_ledger_rejects_malformed_entries(tmp_path):
    records = [{"type": "session", "id": "pi-test"},
        {"type": "custom", "customType": module.INJECTION_OBSERVATION_TYPE, "id": "a", "parentId": None, "data": {"version": 1}}]
    ledger = audit(tmp_path, records, "pi")["injection_ledger"]
    assert ledger["errors"] == [{"line": 2, "reason": "invalid_injection_observation"}]
    assert ledger["entries"] == []


def test_codex_injection_ledger_is_unavailable(tmp_path):
    report = audit(tmp_path, [response("developer", OVERVIEW), response("assistant", "done")])
    assert report["injection_ledger"] == {"status": "unavailable", "reason": "no_injection_observations"}


def test_pi_archive_audit_keeps_overview_digest_across_batches(tmp_path):
    """The batch loop must not shadow the overview digest it later compares against."""
    digest = hashlib.sha256(OVERVIEW.encode()).hexdigest()
    entries = [
        {"id": "a", "parentId": None, "type": "custom", "customType": module.OBSERVATION_TYPE,
         "data": {"version": 1, "session_id": "pi-test", "turn_index": 0,
                  "expected_sha256": digest, "overview_present": True}},
        {"id": "b", "parentId": "a", "type": "message", "message": {"role": "assistant", "content": "ok"}},
    ]
    batch = {"version": 1, "session_id": "pi-test", "stream_id": "s1", "sequence": 0,
             "captured_at": "2026-01-01T00:00:00Z", "leaf_id": "b", "entries": entries}
    content = hashlib.sha256(json.dumps(batch, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    directory = tmp_path / "host-logs/session-diagnostics/tt-child"
    directory.mkdir(parents=True)
    (directory / f"{content}.json").write_text(json.dumps(batch))
    (directory / "index.json").write_text(json.dumps({
        "version": 1, "source": "thinkthread_messages", "agent_harness": "pi",
        "session_id": "pi-test", "identity": {}, "status": "recorded", "pending_batches": {},
        "batches": {content: {"stream_id": "s1", "sequence": 0, "entries": len(entries),
                              "compressed_bytes": 1, "decoded_bytes": 1,
                              "captured_at": "2026-01-01T00:00:00Z", "content_sha256": content}},
    }))
    report = audit_session(directory, agent_harness="pi", overview=OVERVIEW)
    assert report["status"] == "present"
    assert report["checks"] == [{"line": 3, "compaction": 0, "status": "present", "reason": "host_system_prompt_match"}]


def test_pi_branch_and_each_inference_observation(tmp_path):
    records = [{"type": "session", "id": "pi-test"}, observation("a", None), pi_message("b", "a"),
        {"type": "compaction", "id": "c", "parentId": "b"}, observation("d", "c"), pi_message("e", "d"),
        observation("f", "c", present=False), pi_message("g", "f")]
    assert audit(tmp_path, records, "pi")["status"] == "missing"
    report = audit(tmp_path, records, "pi", leaf_id="e")
    assert report["status"] == "present"
    assert report["compactions"] == 1
    assert len(report["checks"]) == 2


@pytest.mark.parametrize("data,reason", [
    ({"present": False}, "host_system_prompt_mismatch"),
    ({"digest": "different-build"}, "overview_version_mismatch"),
    ({"session": "peer"}, "invalid_observation_identity"),
])
def test_pi_observation_failures(tmp_path, data, reason):
    report = audit(tmp_path, [{"type": "session", "id": "pi-test"}, observation("a", None, **data), pi_message("b", "a")], "pi")
    assert report["status"] != "present"
    assert report["checks"][0]["reason"] == reason


def test_pi_old_or_missing_turn_observation_is_unavailable(tmp_path):
    records = [{"type": "session", "id": "pi-test"}, observation("a", None), pi_message("b", "a"), pi_message("c", "b")]
    assert audit(tmp_path, records, "pi")["status"] == "unavailable"
    assert audit(tmp_path, [{"type": "session", "id": "pi-test"}, pi_message("b", None)], "pi")["status"] == "unavailable"


def test_corrupted_history_never_passes(tmp_path):
    path = tmp_path / "broken.jsonl"
    path.write_text(json.dumps(response("developer", OVERVIEW)) + '\n{"type":')
    report = audit_session(path, agent_harness="codex", overview=OVERVIEW)
    assert report["status"] == "unavailable"
    assert report["errors"] == [{"line": 2, "reason": "invalid_json_record"}]
    malformed = [response([], OVERVIEW), {"type": "response_item", "payload": {"type": []}}]
    assert audit(tmp_path, malformed)["status"] == "unavailable"


def test_pi_broken_branch_never_passes(tmp_path):
    report = audit(tmp_path, [observation("a", "missing"), pi_message("b", "a")], "pi")
    assert report["status"] == "unavailable"
    assert report["errors"] == [{"reason": "invalid_branch"}]


def test_recent_activity_bounds_content_and_keeps_failure_facts(tmp_path):
    records = tool_history()
    result = next(item["message"] for item in reversed(records) if item.get("message", {}).get("role") == "toolResult")
    result["isError"] = False
    result["details"] = {"ok": False}
    result["content"] = [{"type": "text", "text": "x" * 1000}]
    report = audit(tmp_path, records, "pi", tools=True, include_content=True)
    view = module.recent_activity(report, 1)
    assert len(view["recent_calls"]) == 1
    call = view["recent_calls"][0]
    assert call["outcome"] == "returned" and call["reported_ok"] is False
    assert call["result_truncated"] and len(call["result_excerpt"]) == 800
    assert "arguments" not in call and "contracts" not in view
    hidden = module.recent_activity(audit(tmp_path, records, "pi", tools=True), 1)
    assert "result_excerpt" not in hidden["recent_calls"][0]


@pytest.mark.parametrize("agent_harness,record", [
    ("codex", {"type": "compacted", "payload": []}),
    ("codex", {"type": "response_item", "payload": None}),
    ("pi", {"type": "message", "id": "a", "parentId": []}),
])
def test_malformed_structured_fields_are_reported(tmp_path, agent_harness, record):
    report = audit(tmp_path, [record], agent_harness)
    assert report["status"] == "unavailable"
    assert report["errors"][0]["reason"] == "invalid_json_record"


def tool_history():
    schema = {"type": "object", "properties": {"search_spec": {
        "type": "object", "properties": {"strategy": {"type": "object"}},
        "required": ["strategy"], "additionalProperties": False}},
        "required": ["search_spec"], "additionalProperties": False}
    contract = {"name": "goal_plus_save_spec_draft", "description": "Put strategy inside search_spec", "parameters": schema}
    serialized = json.dumps({"registered": [contract], "request": [{**contract, "description": ""}]})
    digest = hashlib.sha256(serialized.encode()).hexdigest()
    entries = [{"type": "session", "id": "pi-test"}]

    def append(entry):
        entry.update(id=str(len(entries)), parentId=entries[-1]["id"] if len(entries) > 1 else None)
        entries.append(entry)

    append({"type": "custom", "customType": "goal-plus-tool-contracts", "data": {
        "version": 1, "serialized": serialized, "sha256": digest}})
    for index, arguments in enumerate([{"search_spec": {}, "strategy": {}}, {"search_spec": {"strategy": {}}}]):
        append({"type": "custom", "customType": "goal-plus-provider-request", "data": {
            "version": 1, "session_id": "pi-test", "request_id": str(index),
            "contracts_sha256": digest, "status": "observed"}})
        append({"type": "message", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "PRIVATE THOUGHT"},
            {"type": "toolCall", "id": f"call-{index}", "name": contract["name"], "arguments": arguments}]}})
        append({"type": "message", "message": {"role": "toolResult", "toolCallId": f"call-{index}",
            "toolName": contract["name"], "isError": index == 0,
            "content": [{"type": "text", "text": "Private error context" if index == 0 else "ok"}]}})
    return entries


def test_context_usage_counts_request_schema_once_and_never_prints_content(tmp_path):
    entries = tool_history()
    for entry in entries:
        if entry.get("message", {}).get("role") == "assistant":
            entry["message"]["usage"] = {"input": 12, "cacheRead": 100, "cacheWrite": 5, "output": 999}
        elif entry.get("message", {}).get("role") == "toolResult":
            entry["message"]["details"] = {"duplicate_ui_payload": "not model text" * 100}
    report = audit(tmp_path, entries, "pi", context=True)
    usage = report["context_usage"]
    assert "tool_analysis" not in report
    assert usage["status"] == "observed"
    assert len(usage["contract_snapshots"]) == 1
    snapshot = next(iter(usage["contract_snapshots"].values()))
    assert snapshot["tool_count"] == 1
    assert snapshot["tools"][0]["description_bytes"] == 0
    assert usage["totals"]["calls"] == 2
    assert usage["totals"]["result_bytes"] == len("Private error contextok".encode())
    assert usage["provider_turns"][0]["input_tokens_including_cache"] == 117
    assert usage["requests"][0]["request_bytes"] is None
    assert snapshot["estimated_tokens"] is None
    encoded = json.dumps(report)
    assert "PRIVATE THOUGHT" not in encoded
    assert "Private error context" not in encoded
    assert "Put strategy inside" not in encoded


def test_context_usage_missing_tokenizer_does_not_fabricate_zero_tokens(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    def unavailable(_):
        raise ValueError("unknown encoding")

    monkeypatch.setitem(sys.modules, "tiktoken", SimpleNamespace(get_encoding=unavailable))
    entries = tool_history()[:-1]
    usage = audit(tmp_path, entries, "pi", context=True, token_encoding="unknown")["context_usage"]
    assert usage["tokenization"]["status"] == "unavailable"
    assert usage["totals"]["result_estimated_tokens"] is None
    assert usage["totals"]["unavailable_results"] == 1
    assert usage["calls"][-1]["result"] is None


def test_context_usage_branch_missing_evidence_and_unknown_tokens(tmp_path):
    entries = tool_history()
    usage = audit(tmp_path, entries, "pi", context=True, leaf_id="4")["context_usage"]
    assert usage["totals"]["calls"] == 1
    assert usage["provider_turns"][0]["input_tokens_including_cache"] is None
    assert usage["calls"][0]["result"]["bytes"] == len("Private error context")
    missing = audit(tmp_path, entries[0:1], "pi", context=True)["context_usage"]
    assert missing["status"] == "unavailable"
    assert missing["contract_snapshots"] == {}
    codex = audit(tmp_path, [response("assistant", "done")], context=True)["context_usage"]
    assert codex["status"] == "unavailable"


def test_tools_expose_description_loss_nested_errors_and_correction(tmp_path):
    report = audit(tmp_path, tool_history(), "pi", tools=True)["tool_analysis"]
    assert report["status"] == "observed"
    first, second = report["calls"]
    assert first["description_matches_registration"] is False
    assert first["schema_matches_registration"] is True
    assert first["schema_status"] == "invalid"
    assert {tuple(issue["path"]) for issue in first["schema_issues"]} == {(), ("search_spec",)}
    assert any(issue.get("extra_fields") == ["strategy"] for issue in first["schema_issues"])
    assert first["outcome"] == "error"
    assert second["schema_status"] == "valid"
    assert second["previous_call_id"] == first["tool_call_id"]
    assert second["same_arguments"] is False
    assert "Private error context" not in json.dumps(report)
    assert "PRIVATE THOUGHT" not in json.dumps(report)
    assert "arguments" not in first
    detailed = audit(tmp_path, tool_history(), "pi", tools=True, include_content=True)["tool_analysis"]
    assert detailed["calls"][0]["error_text"] == "Private error context"
    assert detailed["calls"][1]["result_text"] == "ok"
    assert len(detailed["calls"][1]["result_sha256"]) == 64
    assert "PRIVATE THOUGHT" not in json.dumps(detailed)


def test_tool_snapshot_cannot_cross_branch_or_be_reused_as_request(tmp_path):
    entries = tool_history()
    entries.append({"type": "message", "id": "sibling", "parentId": None, "message": entries[3]["message"]})
    report = audit(tmp_path, entries, "pi", tools=True)["tool_analysis"]
    assert report["status"] == "unavailable"
    assert report["contracts"] == {}
    assert report["calls"][0]["contract_evidence"] == "unavailable"
    assert audit(tmp_path, entries, "pi", tools=True, leaf_id="7")["tool_analysis"]["status"] == "observed"
    entries[-1]["parentId"] = "7"
    report = audit(tmp_path, entries, "pi", tools=True)["tool_analysis"]
    assert report["status"] == "unavailable"
    assert report["calls"][-1]["contract_evidence"] == "unavailable"


def test_corrupt_contract_and_reference_manifest_never_prove_visibility(tmp_path):
    entries = tool_history()
    snapshot = json.loads(entries[1]["data"]["serialized"])
    entries[1]["data"]["sha256"] = "wrong"
    manifest = {"tools": [{"name": tool["name"], "inputSchema": tool["parameters"]}
                           for tool in snapshot["registered"]]}
    report = audit(tmp_path, entries, "pi", tools=True, reference_manifest=manifest)["tool_analysis"]
    assert report["status"] == "unavailable"
    assert report["errors"][0]["reason"] == "invalid_contract_snapshot"
    assert report["calls"][0]["contract_evidence"] == "reference_only"
    assert report["calls"][0]["schema_status"] == "invalid"


def test_tool_analysis_does_not_fetch_remote_schema(tmp_path):
    entries = tool_history()
    entries[1]["data"]["sha256"] = "missing"
    manifest = {"tools": [{"name": "goal_plus_save_spec_draft", "inputSchema": {
        "$ref": "https://invalid.example/never-fetch-this-schema"}}]}
    report = audit(tmp_path, entries, "pi", tools=True, reference_manifest=manifest)["tool_analysis"]
    assert report["calls"][0]["schema_status"] == "unavailable"


def test_provider_errors_without_tool_calls_remain_visible(tmp_path):
    entries = tool_history()
    entries.append({"type": "custom", "id": "retry-request", "parentId": "7",
                    "customType": "goal-plus-provider-request", "data": {
                        **entries[2]["data"], "request_id": "retry"}})
    entries.append({"type": "message", "id": "failed-turn", "parentId": "retry-request",
                    "message": {"role": "assistant", "stopReason": "error", "content": [],
                                "errorMessage": "429: provider-private-detail", "model": "fixture-model"}})
    report = audit(tmp_path, entries, "pi", tools=True)["tool_analysis"]
    assert report["summary"]["provider_error_turns"] == 1
    assert report["turns"][-1]["request_id"] == "retry"
    assert report["turns"][-1]["stop_reason"] == "error"
    assert "provider-private-detail" not in json.dumps(report)
    detailed = audit(tmp_path, entries, "pi", tools=True, include_content=True)["tool_analysis"]
    assert detailed["turns"][-1]["error_text"] == "429: provider-private-detail"


def test_tool_analysis_exposes_nested_union_errors(tmp_path):
    entries = tool_history()
    entries[1]["data"]["sha256"] = "missing"
    schema = {"type": "object", "properties": {"search_spec": {"anyOf": [
        {"type": "object", "properties": {}, "additionalProperties": False}, {"type": "null"}]}}}
    manifest = {"tools": [{"name": "goal_plus_save_spec_draft", "inputSchema": schema}]}
    report = audit(tmp_path, entries, "pi", tools=True, reference_manifest=manifest)["tool_analysis"]
    issues = report["calls"][1]["schema_issues"]
    assert any(issue["path"] == ["search_spec"] and issue.get("extra_fields") == ["strategy"]
               and "anyOf" in issue["schema_path"] for issue in issues)
