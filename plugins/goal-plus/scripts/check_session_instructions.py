"""Read-only audit of Goal Plus overview visibility in native session history.

Codex reports recorded working-context evidence, not the full provider request.
Pi reports agent_harness observations of the system prompt at each inference turn.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


OBSERVATION_TYPE = "goal-plus-instructions-observation"
INJECTION_OBSERVATION_TYPE = "goal-plus-injection-observation"
RECENT_CONTENT_CHARS = 800
# Kinds delivered as persistent custom messages can be presence-checked on the
# branch; other channels (system prompt, user transform, steering) have no
# stable history marker of their own. A kind maps to a custom type only for its
# custom-message channels - the same kind delivered via input_transform
# becomes the user prompt and must not be presence-checked.
INJECTION_CUSTOM_TYPES = {
    "goal_context_full": "goal-plus-native-context",
    "goal_context_brief": "goal-plus-native-context",
    "goal_start": "goal-plus-command-context",
    "goal_edit": "goal-plus-command-context",
    "native_continuation": "goal-plus-command-context",
    "must_continue": "goal-plus-must-continue",
    "publication_notification": "goal-plus-publication-notification-delivery",
    "missing_attempt_nudge": "goal-plus-missing-attempt",
    "worker_continuation": "goal-plus-worker-continuation",
}


def checkout_tool_manifest(role: str) -> dict:
    """List checkout schemas in a disposable root without invoking any tool or host."""
    import asyncio
    from tempfile import TemporaryDirectory
    from goal_plus.domain.goal import GoalPlusFinalCheckerIdentity
    from goal_plus.server import create_mcp

    async def collect(root):
        mcp = create_mcp(
            root, caller_role=role,
            caller_agent_session_id="audit-session" if role == "candidate" else None,
            caller_final_check=GoalPlusFinalCheckerIdentity(goal_plus_id="audit", check_id="audit", goal_revision=1)
            if role == "final_checker" else None,
            session_controls=(lambda *_: None) if role == "main" else None,
            session_launcher=object() if role == "main" else None,
        )
        if callable(getattr(mcp, "list_tools", None)):
            tools = await mcp.list_tools()
        else:
            tools = (await mcp.get_tools()).values()
        return {"tools": [{"name": tool.name, "description": tool.description,
                           "inputSchema": tool.parameters} for tool in tools]}

    with TemporaryDirectory(prefix="goal-plus-schema-audit-") as root:
        return asyncio.run(collect(Path(root)))


def analyze_tools(records, *, session_id, include_content=False, reference_manifest=None):
    """Correlate Pi branch calls with observed contracts; never infer model intent."""
    from jsonschema import exceptions, validators
    from referencing import Registry
    from referencing.exceptions import NoSuchResource, Unresolvable

    def refuse_remote_reference(uri):
        raise NoSuchResource(ref=uri)

    def leaf_errors(errors):
        for error in errors:
            if error.context:
                yield from leaf_errors(error.context)
            else:
                yield error

    report = {"evidence_scope": "provider_request_hook", "status": "unavailable",
              "contracts": {}, "requests": [], "turns": [], "calls": [], "errors": []}
    snapshots = {}
    reference = {tool["name"]: {"name": tool["name"], "description": tool.get("description", ""),
                                "parameters": tool.get("inputSchema", {})}
                 for tool in (reference_manifest or {}).get("tools", [])}
    if reference:
        report["reference_contracts"] = reference
    pending_request = None
    calls_by_id = {}
    previous = {}
    for line, entry in records:
        if entry.get("type") == "compaction":
            pending_request = None
        if entry.get("type") == "custom":
            data = entry.get("data")
            if not isinstance(data, dict):
                continue
            if entry.get("customType") == "goal-plus-tool-contracts":
                try:
                    raw = data["serialized"]
                    if data.get("version") != 1 or hashlib.sha256(raw.encode()).hexdigest() != data["sha256"]:
                        raise ValueError("invalid digest")
                    snapshot = json.loads(raw)
                    for key in ("registered", "request"):
                        if not isinstance(snapshot[key], list) or any(
                            not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                            or not isinstance(tool.get("description"), str)
                            or not isinstance(tool.get("parameters"), dict) for tool in snapshot[key]
                        ):
                            raise ValueError("invalid contracts")
                        if len({tool["name"] for tool in snapshot[key]}) != len(snapshot[key]):
                            raise ValueError("duplicate tool")
                    snapshots[data["sha256"]] = snapshot
                    report["contracts"][data["sha256"]] = {"line": line, **snapshot}
                except (KeyError, TypeError, ValueError, AttributeError):
                    report["errors"].append({"line": line, "reason": "invalid_contract_snapshot"})
            elif entry.get("customType") == "goal-plus-provider-request":
                valid = (data.get("version") == 1 and session_id
                         and data.get("session_id") == session_id
                         and isinstance(data.get("request_id"), str)
                         and isinstance(data.get("contracts_sha256"), str)
                         and data["contracts_sha256"] in snapshots
                         and data.get("status") == "observed")
                pending_request = {"line": line, "request_id": data.get("request_id"),
                                   "model": data.get("model"), "turn_index": data.get("turn_index"),
                                   "timestamp": entry.get("timestamp"), "request_bytes": data.get("request_bytes"),
                                   "contracts_sha256": data.get("contracts_sha256"),
                                   "status": "observed" if valid else "unavailable"}
                report["requests"].append(pending_request)
        message = entry.get("message", {})
        if entry.get("type") != "message" or not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            turn = {"line": line, "timestamp": entry.get("timestamp"),
                    "request_id": (pending_request or {}).get("request_id"),
                    "stop_reason": message.get("stopReason"),
                    "provider": message.get("provider"), "model": message.get("model")}
            if include_content and isinstance(message.get("errorMessage"), str):
                turn["error_text"] = message["errorMessage"]
            report["turns"].append(turn)
            content = message.get("content", [])
            for part in content if isinstance(content, list) else []:
                if not isinstance(part, dict) or part.get("type") != "toolCall":
                    continue
                name, call_id = part.get("name"), part.get("id")
                if not isinstance(name, str) or not name.startswith(("goal_plus_", "search_", "thinkthread_")):
                    continue
                request = pending_request or {}
                snapshot = snapshots.get(request.get("contracts_sha256"), {}) if request.get("status") == "observed" else {}
                requested = {tool["name"]: tool for tool in snapshot.get("request", [])}
                registered = {tool["name"]: tool for tool in snapshot.get("registered", [])}
                contract = requested.get(name) if snapshot else reference.get(name)
                args = part.get("arguments")
                digest = hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                call = {"line": line, "tool_call_id": call_id, "name": name,
                        "request_id": request.get("request_id"),
                        "contract_evidence": "observed" if snapshot else "reference_only" if contract else "unavailable",
                        "arguments_sha256": digest, "schema_issues": [], "outcome": "unavailable"}
                if name in previous:
                    prior = previous[name]
                    call["previous_call_id"] = prior["tool_call_id"]
                    call["same_arguments"] = prior["arguments_sha256"] == digest
                if snapshot:
                    call["tool_visible"] = name in requested
                    if name in requested and name in registered:
                        call["description_matches_registration"] = requested[name]["description"] == registered[name]["description"]
                        call["schema_matches_registration"] = requested[name]["parameters"] == registered[name]["parameters"]
                if contract:
                    try:
                        schema = contract["parameters"]
                        validator_type = validators.validator_for(schema)
                        validator_type.check_schema(schema)
                        errors = validator_type(schema, registry=Registry(retrieve=refuse_remote_reference)).iter_errors(args)
                        for error in leaf_errors(errors):
                            issue = {"path": list(error.absolute_path), "rule": error.validator,
                                     "schema_path": list(error.absolute_schema_path)}
                            if error.validator == "required" and isinstance(error.instance, dict):
                                issue["missing_fields"] = [key for key in error.validator_value if key not in error.instance]
                            if error.validator == "additionalProperties" and isinstance(error.instance, dict):
                                issue["extra_fields"] = sorted(
                                    key for key in error.instance if key not in error.schema.get("properties", {})
                                    and not any(re.search(pattern, key) for pattern in error.schema.get("patternProperties", {}))
                                )
                            call["schema_issues"].append(issue)
                        call["schema_status"] = "invalid" if call["schema_issues"] else "valid"
                    except (exceptions.SchemaError, Unresolvable, TypeError, ValueError):
                        call["schema_status"] = "unavailable"
                else:
                    call["schema_status"] = "unavailable"
                if include_content:
                    call["arguments"] = args
                if not isinstance(call_id, str) or call_id in calls_by_id:
                    report["errors"].append({"line": line, "reason": "invalid_tool_call_identity"})
                else:
                    calls_by_id[call_id] = call
                previous[name] = call
                report["calls"].append(call)
            pending_request = None
        elif message.get("role") == "toolResult":
            call_id = message.get("toolCallId")
            call = calls_by_id.get(call_id) if isinstance(call_id, str) else None
            if call is not None:
                if call["outcome"] != "unavailable" or message.get("toolName") != call["name"]:
                    report["errors"].append({"line": line, "reason": "invalid_tool_result_identity"})
                    continue
                call["result_line"] = line
                call["result_sha256"] = hashlib.sha256(json.dumps(
                    message.get("content"), sort_keys=True, ensure_ascii=False,
                ).encode()).hexdigest()
                call["outcome"] = "error" if message.get("isError") is True else "returned" if message.get("isError") is False else "unavailable"
                details = message.get("details")
                if isinstance(details, dict) and isinstance(details.get("ok"), bool):
                    call["reported_ok"] = details["ok"]
                if include_content:
                    call["result_text"] = _message_text(message)
                if include_content and message.get("isError"):
                    call["error_text"] = _message_text(message)
    if report["requests"] and all(r["status"] == "observed" for r in report["requests"]) and all(
        call["contract_evidence"] == "observed" for call in report["calls"]
    ) and not report["errors"]:
        report["status"] = "observed"
    report["summary"] = {"requests": len(report["requests"]), "calls": len(report["calls"]),
                         "provider_error_turns": sum(turn["stop_reason"] == "error" for turn in report["turns"]),
                         "tool_errors": sum(call["outcome"] == "error" for call in report["calls"]),
                         "schema_invalid_calls": sum(call["schema_status"] == "invalid" for call in report["calls"])}
    return report


def _message_text(item: dict[str, Any]) -> str:
    content = item.get("content", [])
    if not isinstance(content, list):
        return ""
    return "\n".join(
        part["text"] for part in content
        if isinstance(part, dict) and part.get("type") in ("input_text", "text")
        and isinstance(part.get("text"), str)
    )


def analyze_context(records, tool_analysis, *, overview: str, token_encoding: str | None = None):
    """Account for observed GP definitions and text, not reconstructed provider context."""
    encoder = None
    token_status = "not_requested"
    if token_encoding:
        try:
            import tiktoken
            encoder = tiktoken.get_encoding(token_encoding)
            token_status = "estimated"
        except (ImportError, ValueError, OSError):
            token_status = "unavailable"

    def serialized(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def size(text):
        return {"bytes": len(text.encode("utf-8")), "characters": len(text),
                "estimated_tokens": len(encoder.encode(text, disallowed_special=())) if encoder else None}

    def contracts_size(contracts):
        rows = [{"name": item["name"], **size(serialized(item)),
                 "description_bytes": len(item.get("description", "").encode()),
                 "schema_bytes": len(serialized(item.get("parameters", {})).encode())}
                for item in contracts]
        return {"tool_count": len(rows), **size(serialized(contracts)),
                "tools": sorted(rows, key=lambda item: (-item["bytes"], item["name"]))}

    by_line = dict(records)
    snapshots = {digest: contracts_size(snapshot["request"])
                 for digest, snapshot in tool_analysis.get("contracts", {}).items()}
    calls = []
    for call in tool_analysis.get("calls", []):
        message = by_line[call["line"]].get("message", {})
        part = next((item for item in message.get("content", [])
                     if isinstance(item, dict) and item.get("type") == "toolCall"
                     and item.get("id") == call["tool_call_id"]), None)
        result_line = call.get("result_line")
        result = by_line.get(result_line, {}).get("message", {})
        text = _message_text(result) if result_line else None
        row = {"name": call["name"], "line": call["line"], "result_line": result_line,
               "arguments": size(serialized(part.get("arguments"))) if part else None,
               "result": size(text) if text is not None else None}
        if text is not None:
            try:
                value = json.loads(text)
                if isinstance(value, dict):
                    row["result_fields"] = sorted(
                        ({"field": key, **size(serialized(item))} for key, item in value.items()),
                        key=lambda item: (-item["bytes"], item["field"]),
                    )
            except ValueError:
                pass
        calls.append(row)
    totals = {}
    for call in calls:
        total = totals.setdefault(call["name"], {"name": call["name"], "calls": 0,
            "arguments_bytes": 0, "result_bytes": 0, "largest_result_bytes": 0})
        total["calls"] += 1
        total["arguments_bytes"] += (call["arguments"] or {}).get("bytes", 0)
        total["result_bytes"] += (call["result"] or {}).get("bytes", 0)
        total["largest_result_bytes"] = max(total["largest_result_bytes"], (call["result"] or {}).get("bytes", 0))
    for snapshot in snapshots.values():
        unused = [tool for tool in snapshot["tools"] if tool["name"] not in totals]
        snapshot["uncalled_in_selected_branch"] = {
            "tool_count": len(unused), "definition_bytes": sum(tool["bytes"] for tool in unused),
        }

    turns = []
    for turn in tool_analysis.get("turns", []):
        usage = by_line[turn["line"]].get("message", {}).get("usage")
        usage = usage if isinstance(usage, dict) else {}
        values = [usage.get(key) for key in ("input", "cacheRead", "cacheWrite")]
        input_tokens = sum(values) if all(type(value) is int and value >= 0 for value in values) else None
        turns.append({"line": turn["line"], "request_id": turn["request_id"],
                      "input_tokens_including_cache": input_tokens})
    requests = [{"line": request["line"], "request_id": request["request_id"],
                 "contracts_sha256": request["contracts_sha256"], "status": request["status"],
                 "request_bytes": request.get("request_bytes")}
                for request in tool_analysis.get("requests", [])]
    report = {
        "status": tool_analysis.get("status", "unavailable"),
        "scope": "selected_branch_gp_tool_definitions_and_text",
        "tokenization": {"status": token_status, "encoding": token_encoding},
        "limitations": [
            "Token estimates encode recorded text; provider tool framing and actual tokenizer may differ.",
            "Recorded result totals are accumulated history, not current context occupancy or billed input.",
            "Provider input usage includes all context and cached input, not only Goal Plus.",
            "Full system prompt, native tools, images and compaction retention are not reconstructed.",
        ],
        "overview_reference": size(overview),
        "contract_snapshots": snapshots, "requests": requests, "provider_turns": turns,
        "tools": sorted(totals.values(), key=lambda item: (-item["result_bytes"], item["name"])),
        "calls": calls,
        "totals": {"calls": len(calls),
                   "unavailable_results": sum(item["result"] is None for item in calls),
                   "arguments_bytes": sum(item["arguments_bytes"] for item in totals.values()),
                   "result_bytes": sum(item["result_bytes"] for item in totals.values()),
                   "arguments_estimated_tokens": sum((item["arguments"] or {}).get("estimated_tokens") or 0 for item in calls) if encoder else None,
                   "result_estimated_tokens": sum((item["result"] or {}).get("estimated_tokens") or 0 for item in calls) if encoder else None},
    }
    if tool_analysis.get("reference_contracts"):
        reference = tool_analysis["reference_contracts"]
        report["reference_tools"] = {"evidence": "reference_only",
            **contracts_size(list(reference.values()))}
        report["reference_for_observed_sets"] = {
            digest: {"missing_tools": [item["name"] for item in snapshot["request"] if item["name"] not in reference],
                     **contracts_size([reference[item["name"]] for item in snapshot["request"] if item["name"] in reference])}
            for digest, snapshot in tool_analysis.get("contracts", {}).items()
        }
    return report


def analyze_injections(records: list[tuple[int, dict[str, Any]]], *, agent_harness: str) -> dict[str, Any]:
    """Summarize injection-ledger observations and enforce the injection contract.

    Every Goal Plus injection site registers a ``goal-plus-injection-observation``
    entry; this analyzer rolls them up per role and kind, correlates each with
    the most recent provider input usage (context size at injection time), and
    reports the defects that motivated the ledger: a full goal context whose
    payload lacks ``raw_goal``, a registered injection whose custom message is
    absent from the branch, and a prompt-path turn that skipped the goal
    context because of a stale must-continue flag.
    """
    if agent_harness != "pi":
        return {"status": "unavailable", "reason": "no_injection_observations"}
    errors: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    context_tokens: int | None = None
    pending_delivery: list[tuple[int, str]] = []
    for line, record in records:
        message = record.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            usage = message.get("usage")
            if isinstance(usage, dict):
                values = [usage.get(key) for key in ("input", "cacheRead", "cacheWrite")]
                if all(type(value) is int and value >= 0 for value in values):
                    context_tokens = sum(values)
        if record.get("type") == "custom_message" and record.get("customType") in set(INJECTION_CUSTOM_TYPES.values()):
            custom_type = record["customType"]
            still_pending = []
            for at_line, kind in pending_delivery:
                if INJECTION_CUSTOM_TYPES[kind] == custom_type and at_line <= line:
                    continue
                still_pending.append((at_line, kind))
            pending_delivery = still_pending
        if record.get("type") != "custom" or record.get("customType") != INJECTION_OBSERVATION_TYPE:
            continue
        data = record.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("kind"), str):
            errors.append({"line": line, "reason": "invalid_injection_observation"})
            continue
        entry = {"line": line, "kind": data["kind"]}
        for key in ("role", "session_id", "channel", "target_id", "est_tokens", "chars",
                    "raw_goal_present", "reason", "goal_revision", "control_version"):
            if key in data:
                entry[key] = data[key]
        entry["context_tokens_at_injection"] = context_tokens
        entries.append(entry)
        kind = data["kind"]
        if kind == "goal_context_full" and data.get("raw_goal_present") is False:
            errors.append({"line": line, "reason": "full_context_without_raw_goal"})
        if kind == "goal_context_skipped" and data.get("reason") == "continuation_turn":
            # These entries only originate from before_agent_start, which fires
            # solely on prompt-path turns; a real continuation turn bypasses it.
            errors.append({"line": line, "reason": "goal_context_skipped_on_prompt_turn"})
        if kind in INJECTION_CUSTOM_TYPES and entry.get("channel") != "input_transform":
            pending_delivery.append((line, kind))
    errors.extend({"line": at_line, "reason": "registered_injection_absent"}
                  for at_line, kind in pending_delivery)
    summary: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        row = summary.setdefault((entry.get("role", "unknown"), entry["kind"]),
                                 {"count": 0, "est_tokens": 0,
                                  "raw_goal_present": {"true": 0, "false": 0}})
        row["count"] += 1
        row["est_tokens"] += entry.get("est_tokens") or 0
        if isinstance(entry.get("raw_goal_present"), bool):
            row["raw_goal_present"]["true" if entry["raw_goal_present"] else "false"] += 1
    return {
        "status": "observed" if entries else "unavailable",
        "scope": "selected_branch_injection_observations",
        "limitations": [
            "Token estimates use the extension heuristic (CJK about one token per character, ASCII about four characters per token), not a tokenizer.",
            "Context size at injection is the most recent provider input usage before the ledger entry; it covers the whole context, not only Goal Plus.",
        ],
        "entries": entries,
        "summary": [{"role": role, "kind": kind, **row} for (role, kind), row in sorted(summary.items())],
        "errors": errors,
    }


def audit_session(path: Path, *, agent_harness: str, overview: str, leaf_id: str | None = None,
                  tools: bool = False, include_content: bool = False,
                  reference_manifest: dict | None = None, context: bool = False,
                  token_encoding: str | None = None) -> dict[str, Any]:
    overview = overview.strip()
    if not overview:
        raise ValueError("expected overview must not be empty")
    digest = hashlib.sha256(overview.encode()).hexdigest()
    report: dict[str, Any] = {
        "agent_harness": agent_harness, "history": str(path), "expected_sha256": digest,
        "evidence_scope": "recorded_working_context" if agent_harness == "codex" else "host_system_prompt_observation",
        "status": "unavailable", "compactions": 0, "checks": [], "errors": [],
    }
    records = []
    try:
        archive_errors = []
        if path.is_dir():
            index = json.loads((path / "index.json").read_text())
            if index.get("source") == "native":
                return audit_session(Path(index["history"]), agent_harness=agent_harness, overview=overview,
                                     leaf_id=leaf_id, tools=tools, include_content=include_content,
                                     reference_manifest=reference_manifest, context=context,
                                     token_encoding=token_encoding)
            if index.get("source") != "thinkthread_messages" or agent_harness != "pi":
                raise ValueError("unsupported diagnostic archive")
            entries = {}
            sequences = {}
            archive_leaf = None
            for batch_digest, metadata in sorted(index["batches"].items(), key=lambda item: (
                item[1]["captured_at"], item[1]["stream_id"], item[1]["sequence"],
            )):
                if not re.fullmatch(r"[a-f0-9]{64}", batch_digest):
                    raise ValueError("invalid archive filename")
                batch = json.loads((path / f"{batch_digest}.json").read_text())
                if hashlib.sha256(json.dumps(batch, sort_keys=True, separators=(",", ":")).encode()).hexdigest() != metadata["content_sha256"]:
                    raise ValueError("archive content hash mismatch")
                if batch["session_id"] != index["session_id"]:
                    raise ValueError("archive session mismatch")
                sequence = batch["sequence"]
                seen_sequences = sequences.setdefault(batch["stream_id"], set())
                if sequence in seen_sequences:
                    archive_errors.append({"reason": "duplicate_archive_sequence"})
                seen_sequences.add(sequence)
                archive_leaf = batch["leaf_id"]
                for entry in batch["entries"]:
                    prior = entries.get(entry["id"])
                    if prior is not None and prior != entry:
                        archive_errors.append({"reason": "changed_archive_entry"})
                    entries[entry["id"]] = entry
            for seen_sequences in sequences.values():
                if sorted(seen_sequences) != list(range(len(seen_sequences))):
                    archive_errors.append({"reason": "missing_archive_batch"})
            if not sequences or index.get("status") != "recorded":
                archive_errors.append({"reason": "incomplete_archive"})
            stream = [json.dumps({"type": "session", "id": index.get("session_id")})]
            stream.extend(json.dumps(entry) for entry in entries.values())
            leaf_id = leaf_id or archive_leaf
            report["archive"] = {"identity": index.get("identity"), "batches": index.get("batches"),
                                 "collection_complete": index.get("collection_complete", False)}
        else:
            stream = path.read_text(encoding="utf-8").splitlines()
        report["errors"].extend(archive_errors)
        if stream:
            for line_number, line in enumerate(stream, 1):
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError("expected object")
                    if not isinstance(record.get("type"), str):
                        raise ValueError("missing record type")
                    if agent_harness == "codex" and record["type"] in {"session_meta", "compacted", "response_item"} and not isinstance(record.get("payload"), dict):
                        raise ValueError("invalid payload")
                    if agent_harness == "pi" and (
                        not isinstance(record.get("id"), str)
                        or (record.get("parentId") is not None and not isinstance(record["parentId"], str))
                    ):
                        raise ValueError("invalid entry identity")
                    records.append((line_number, record))
                except ValueError:
                    report["errors"].append({"line": line_number, "reason": "invalid_json_record"})
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        report["errors"].append({"reason": "unreadable_history"})
    if agent_harness == "pi":
        # Pi history is a tree. Audit only the selected branch, including its ancestors.
        entries = {r["id"]: (n, r) for n, r in records if isinstance(r.get("id"), str)}
        if len(entries) != sum(isinstance(r.get("id"), str) for _, r in records):
            report["errors"].append({"reason": "duplicate_entry_id"})
        leaf = leaf_id or next((r["id"] for _, r in reversed(records) if r.get("type") != "session" and isinstance(r.get("id"), str)), None)
        branch = []
        seen = set()
        while leaf is not None:
            if leaf in seen or leaf not in entries:
                report["errors"].append({"reason": "invalid_branch"})
                break
            seen.add(leaf)
            n, entry = entries[leaf]
            branch.append((n, entry))
            leaf = entry.get("parentId")
        session_id = next((r.get("id") for _, r in records if r.get("type") == "session"), None)
        report["session_id"] = session_id
        records = list(reversed(branch))
    elif agent_harness != "codex":
        raise ValueError("unsupported agent_harness")

    report["injection_ledger"] = analyze_injections(records, agent_harness=agent_harness)

    if tools or context:
        tool_analysis = analyze_tools(
            records, session_id=report.get("session_id"), include_content=include_content,
            reference_manifest=reference_manifest,
        ) if agent_harness == "pi" else {"status": "unavailable", "reason": "no_provider_tool_observations"}
        if report["errors"]:
            tool_analysis["status"] = "unavailable"
        if tools:
            report["tool_analysis"] = tool_analysis
        if context:
            report["context_usage"] = analyze_context(records, tool_analysis, overview=overview,
                                                       token_encoding=token_encoding)

    present = False
    context_known = False
    observation = None
    generation = 0
    used_observation = False

    def check(line: int, status: str, reason: str) -> None:
        report["checks"].append({"line": line, "compaction": generation, "status": status, "reason": reason})

    for line, record in records:
        kind = record.get("type")
        if agent_harness == "codex":
            if kind == "session_meta":
                report["session_id"] = record.get("payload", {}).get("id")
            if kind == "compacted":
                generation += 1
                replacement = record.get("payload", {}).get("replacement_history")
                context_known = isinstance(replacement, list)
                present = context_known and any(
                    isinstance(item, dict) and item.get("type") == "message"
                    and item.get("role") in ("system", "developer")
                    and overview in _message_text(item) for item in replacement
                )
                # guardian_history and summaries are archives, not instruction evidence.
            if kind != "response_item":
                continue
            item = record.get("payload", {})
            if not isinstance(item, dict):
                report["errors"].append({"line": line, "reason": "invalid_response_item"})
                continue
            if item.get("type") == "message" and item.get("role") in ("system", "developer"):
                present = present or overview in _message_text(item)
            if (item.get("type") == "message" and item.get("role") == "assistant") or item.get("type") in ("function_call", "custom_tool_call"):
                status = "present" if present else "missing" if context_known else "unavailable"
                check(line, status, "overview_in_instructions" if present else "overview_absent_from_recorded_context" if context_known else "incomplete_context_evidence")
        else:
            if kind == "compaction":
                generation += 1
                observation = None
            if kind == "custom" and record.get("customType") == OBSERVATION_TYPE:
                observation = record.get("data")
                used_observation = False
            message = record.get("message", {})
            if kind == "message" and isinstance(message, dict) and message.get("role") == "assistant":
                data = observation if isinstance(observation, dict) else {}
                if used_observation or not data:
                    check(line, "unavailable", "missing_turn_observation")
                elif data.get("version") != 1 or data.get("session_id") != report.get("session_id") or not report.get("session_id"):
                    check(line, "unavailable", "invalid_observation_identity")
                elif data.get("expected_sha256") != digest:
                    check(line, "unavailable", "overview_version_mismatch")
                elif type(data.get("overview_present")) is not bool:
                    check(line, "unavailable", "invalid_observation")
                else:
                    found = data["overview_present"]
                    check(line, "present" if found else "missing", "host_system_prompt_match" if found else "host_system_prompt_mismatch")
                used_observation = True
    report["compactions"] = generation
    statuses = {item["status"] for item in report["checks"]}
    if report["errors"]:
        report["status"] = "unavailable"
    elif "missing" in statuses:
        report["status"] = "missing"
    elif statuses == {"present"}:
        report["status"] = "present"
    # Injection-ledger defects mean an expected payload was absent from model
    # context; they downgrade an otherwise clean overview audit to "missing".
    if report["injection_ledger"].get("errors") and report["status"] == "present":
        report["status"] = "missing"
    # A compaction at EOF has no subsequent model turn to inspect.
    if generation and not any(item["compaction"] == generation for item in report["checks"]):
        report["errors"].append({"reason": "no_turn_after_last_compaction"})
        report["status"] = "unavailable"
    return report


def recent_activity(report: dict[str, Any], limit: int) -> dict[str, Any]:
    """Bounded view of the existing branch-aware audit, without contract dumps."""
    analysis = report.get("tool_analysis", {})
    recent = []
    for call in analysis.get("calls", [])[-limit:]:
        row = {key: call[key] for key in (
            "line", "tool_call_id", "name", "outcome", "reported_ok", "schema_status", "result_line",
        ) if key in call}
        if "result_text" in call:
            text = call["result_text"]
            row["result_excerpt"] = text[:RECENT_CONTENT_CHARS]
            row["result_truncated"] = len(text) > RECENT_CONTENT_CHARS
        recent.append(row)
    models = sorted({(turn.get("provider"), turn.get("model")) for turn in analysis.get("turns", [])
                     if isinstance(turn.get("provider"), str) and isinstance(turn.get("model"), str)})
    provider_errors = []
    for turn in [item for item in analysis.get("turns", []) if item.get("stop_reason") == "error"][-limit:]:
        row = {key: turn.get(key) for key in ("line", "timestamp", "provider", "model", "stop_reason")}
        if "error_text" in turn:
            row["error_excerpt"] = turn["error_text"][:RECENT_CONTENT_CHARS]
            row["error_truncated"] = len(turn["error_text"]) > RECENT_CONTENT_CHARS
        provider_errors.append(row)
    return {
        "history": report["history"], "status": report["status"],
        "compactions": report["compactions"], "errors": report["errors"],
        "tool_analysis_status": analysis.get("status", "unavailable"),
        "tool_analysis_errors": analysis.get("errors", []),
        "injection_ledger_status": report.get("injection_ledger", {}).get("status", "unavailable"),
        "injection_ledger_errors": report.get("injection_ledger", {}).get("errors", []),
        "models": [{"provider": provider, "model": model} for provider, model in models],
        "summary": analysis.get("summary", {}), "recent_calls": recent,
        "recent_provider_errors": provider_errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("history", type=Path, help="Native JSONL or a host-logs/session-diagnostics session directory")
    parser.add_argument("--agent-harness", choices=("codex", "pi"), required=True)
    parser.add_argument("--overview", type=Path, required=True, help="Exact assets/goal-plus-instructions.txt from the session's build")
    parser.add_argument("--leaf-id", help="Pi branch leaf; defaults to last persisted entry")
    parser.add_argument("--tools", action="store_true", help="Analyze Pi provider tool contracts and tool-call results (requires jsonschema)")
    parser.add_argument("--recent", type=int, metavar="N", help="Show the last N GP calls and provider errors (1-100); implies --tools")
    parser.add_argument("--include-content", action="store_true", help="Include Goal Plus call arguments, results and provider errors from local history")
    manifest = parser.add_mutually_exclusive_group()
    manifest.add_argument("--tool-manifest", type=Path, help="Optional tools/list JSON for historical comparison; never counts as request evidence")
    manifest.add_argument("--checkout-tools", choices=("main", "candidate", "final_checker"),
                          help="Compare against this checkout's role schemas in a disposable root; no tools or hosts are executed")
    parser.add_argument("--context", action="store_true", help="Measure observed GP schemas, arguments, results and provider input usage without printing content")
    parser.add_argument("--token-encoding", help="Optional tiktoken encoding, e.g. o200k_base; estimates text tokens, not provider billing")
    parser.add_argument("--output", type=Path, help="Write the local JSON report to this path")
    args = parser.parse_args()
    if args.recent is not None and not 1 <= args.recent <= 100:
        parser.error("--recent must be between 1 and 100")
    args.tools = args.tools or args.recent is not None
    try:
        report = audit_session(args.history, agent_harness=args.agent_harness, overview=args.overview.read_text(encoding="utf-8"), leaf_id=args.leaf_id,
                               tools=args.tools, include_content=args.include_content,
                               reference_manifest=(json.loads(args.tool_manifest.read_text()) if args.tool_manifest
                                                   else checkout_tool_manifest(args.checkout_tools) if args.checkout_tools else None),
                               context=args.context, token_encoding=args.token_encoding)
    except (OSError, UnicodeError, ValueError) as exc:
        parser.error(str(exc))
    encoded = json.dumps(recent_activity(report, args.recent) if args.recent is not None else report,
                         ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
        print(str(args.output))
    else:
        print(encoded)
    if args.tools and report["tool_analysis"]["status"] == "unavailable":
        return 2
    if args.context and report["context_usage"]["status"] == "unavailable":
        return 2
    return {"present": 0, "missing": 1, "unavailable": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
