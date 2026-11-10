"""Incremental Codex rollout learning through the shared BATE extractor.

Only explicitly configured files are read. A completed turn seals evidence;
unfinished/failed turns never train. Completion is not a task-quality score.
The journal is reconstructed by stable session/turn IDs, so rereads and restarts
do not multiply support. Publication and its checkpoint share one atomic file.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from ..io import load_specs
from ..models import stable_json
from .traces import import_records

MAX_FILE_BYTES = 16 * 1024 * 1024


def completed_traces(records):
    """Import sealed successful transport turns, preserving real session IDs."""
    header = next((r for r in records if r.get("type") in
                   {"session_meta", "thread.started"}), None)
    if header is None:
        return []
    session = (header.get("payload", {}).get("id") if header["type"] == "session_meta"
               else header.get("thread_id"))
    if not isinstance(session, str) or not session:
        return []
    traces, pending, ordinal = [], [], 0
    for row in records:
        kind = row.get("type")
        payload = row.get("payload", {})
        event = payload.get("type") if kind == "event_msg" else kind
        if event in {"task_started", "turn.started"}:
            pending = []
        if kind == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
            pending = []
        pending.append(row)
        if event not in {"task_complete", "turn.completed", "task_failed", "turn.failed", "turn_aborted"}:
            continue
        if event in {"task_complete", "turn.completed"}:
            imported = import_records([header, *pending], source_id=session)
            # A turn containing a failed primitive is not successful evidence.
            invalid = ("failed_calls", "missing_results", "malformed_calls",
                       "duplicate_call_ids", "orphan_results")
            if not any(imported.diagnostics[key] for key in invalid):
                for trace in imported.traces:
                    traces.append(replace(trace,
                        trace_id=f"{trace.trace_id}:turn:{ordinal}",
                        metadata={**trace.metadata, "evidence": "completed-turn-primitives",
                                  "task_success_verified": False}))
        ordinal += 1
        pending = []
    return traces


def read_evidence(paths, allowed):
    unique, calls = {}, set()
    for path in paths:
        path = Path(path)
        if not path.exists():
            continue  # A configured rollout may not have been created yet.
        with path.open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("Online input exceeds the per-file size limit")
        # A writer may still be appending the final JSON line.
        lines = data.split(b"\n")[:-1]
        records = [json.loads(line) for line in lines if line.strip()]
        if not all(isinstance(row, dict) for row in records):
            raise ValueError("Online rollout rows must be objects")
        for row in records:
            if row.get("type") in {"session_meta", "response_item", "event_msg"} and not isinstance(row.get("payload"), dict):
                raise ValueError("Online rollout payloads must be objects")
            if row.get("type") in {"item.started", "item.completed"} and not isinstance(row.get("item"), dict):
                raise ValueError("Online exec items must be objects")
        for trace in completed_traces(records):
            segments, segment = [], []
            for call in trace.calls:
                if call.name in allowed:
                    segment.append(call)
                else:
                    segments.append(segment)
                    segment = []
            segments.append(segment)
            for index, part in enumerate(segments):
                if len(part) < 2:
                    continue
                selected = replace(trace, trace_id=f"{trace.trace_id}:scope:{index}", calls=tuple(part))
                old = unique.get(selected.trace_id)
                if old is not None:
                    if old.to_dict() != selected.to_dict():
                        raise ValueError("Conflicting copies of an online session segment")
                    continue
                for call in part:
                    identity = (selected.metadata["session_id"], call.call_id)
                    if identity in calls:
                        raise ValueError("Repeated primitive identity across online segments")
                    calls.add(identity)
                unique[selected.trace_id] = selected
                if len(unique) > 10000:
                    raise ValueError("Online corpus exceeds 10000 segments")
    return sorted(unique.values(), key=lambda t: t.trace_id)


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".publish-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class OnlineLearner:
    def __init__(self, settings):
        self.config = settings["online"]
        self.root = Path(self.config["state_dir"])
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # One server owns a learning pool. A second process fails clearly;
        # kernel locks are released after crashes without stale lease stealing.
        import fcntl
        fd = os.open(self.root / "writer.lock", os.O_CREAT | os.O_RDWR, 0o600)
        self._lease = os.fdopen(fd, "w")
        try:
            fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lease.close()
            raise ValueError("Online state directory already has a writer") from None
        self.policy_id = hashlib.sha256(stable_json({
            "online": self.config, "bindings": settings["bindings"]}).encode()).hexdigest()
        self.digest = None
        self.version = None
        self.specs = []
        self.error = None
        self.segments = 0
        self.sessions = 0
        self.extractions = 0
        self._lock = asyncio.Lock()

    def close(self):
        self._lease.close()

    def restore(self):
        path = self.root / "current.json"
        if path.exists():
            record = json.loads(path.read_text())
            if record.get("format") != "bate-codex-online-v1":
                raise ValueError("Unknown online checkpoint format")
            if record.get("policy_id") == self.policy_id:
                from ..spec import MetaToolSpec
                self.specs = [MetaToolSpec.from_dict(s) for s in record["meta_tools"]]
                self.digest, self.version = record["input_digest"], record["version"]
                self.segments, self.sessions = record["segments"], record["sessions"]

    async def _extract(self, traces, allowed):
        with tempfile.TemporaryDirectory(prefix="extract-", dir=self.root) as directory:
            root = Path(directory)
            _atomic_json(root / "traces.json", {"traces": [t.to_dict() for t in traces]})
            _atomic_json(root / "config.json", {"normalization": {
                "argument_matching": "structural", "default_semantic_type": "mutator",
                "tool_rules": [{"pattern": "^" + re.escape(name) + "$",
                                "semantic_type": "accessor"} for name in sorted(allowed)]}})
            command = [sys.executable, "-m", "bate", "extract",
                       "--traces", str(root / "traces.json"), "--config", str(root / "config.json"),
                       "--out", str(root / "output"), "--also-raw", "--naming", "stable-short",
                       "--min-support", str(self.config["min_support"]),
                       "--max-len", str(self.config["max_len"]),
                       "--max-meta-tools", str(self.config["max_meta_tools"])]
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
            try:
                await asyncio.wait_for(process.communicate(), timeout=self.config["extraction_timeout_seconds"])
            except BaseException:
                if process.returncode is None:
                    process.kill()
                await process.communicate()
                raise
            if process.returncode:
                raise ValueError("BATE extraction failed; previous catalog retained")
            return load_specs(root / "output/active_meta_tools.json")

    async def refresh(self, allowed):
        async with self._lock:
            try:
                traces = read_evidence(self.config["trace_files"], allowed)
                sessions = {t.metadata["session_id"] for t in traces}
                digest = hashlib.sha256(stable_json({
                    "policy": self.policy_id, "allowed": sorted(allowed),
                    "traces": [t.to_dict() for t in traces]}).encode()).hexdigest()
                self.segments, self.sessions = len(traces), len(sessions)
                if digest == self.digest:
                    self.error = None
                    return False
                if len(traces) < self.config["min_support"] or len(sessions) < self.config["min_sessions"]:
                    self.error = None
                    return False
                extracted = await self._extract(traces, allowed)
                self.extractions += 1
                by_id = {t.trace_id: t for t in traces}
                selected = []
                for spec in extracted:
                    evidence = [by_id[i] for i in spec.metadata.get("source_trace_ids", []) if i in by_id]
                    count = len({t.metadata["session_id"] for t in evidence})
                    if (count < self.config["min_sessions"] or spec.metadata.get("compiler_warnings")
                            or spec.metadata.get("side_effect_free") is not True
                            or not {s.tool for s in spec.steps} <= set(allowed)):
                        continue
                    selected.append(replace(spec, metadata={**spec.metadata,
                        "distinct_sessions": count, "execution_guard": "none",
                        "catalog_origin": "codex_online", "task_success_verified": False,
                        "evidence": "completed-turn-primitives", "lifecycle_state": "provisional"}))
                # An empty extraction does not erase a useful previous version.
                specs = selected or self.specs
                version = hashlib.sha256(stable_json({"input_digest": digest,
                    "meta_tools": [s.to_dict() for s in specs]}).encode()).hexdigest()
                record = {"format": "bate-codex-online-v1", "policy_id": self.policy_id,
                          "input_digest": digest, "version": version,
                          "segments": len(traces), "sessions": len(sessions),
                          "meta_tools": [s.to_dict() for s in specs]}
                _atomic_json(self.root / "versions" / (version + ".json"), record)
                _atomic_json(self.root / "current.json", record)
                self.specs, self.digest, self.version = specs, digest, version
                self.error = None
                return True
            except (OSError, ValueError, TypeError, KeyError, TimeoutError) as error:
                # Never echo private trajectories or upstream results in status.
                self.error = type(error).__name__
                return False

    def describe(self):
        return {"version": self.version, "segments": self.segments,
                "sessions": self.sessions, "learned_templates": len(self.specs),
                "extractions_this_process": self.extractions, "last_error": self.error,
                "task_success_verified": False}
