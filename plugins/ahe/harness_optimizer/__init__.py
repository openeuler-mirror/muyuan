"""第一阶段的通用 harness optimizer 构件。"""

from .adapter import Command, CommandAdapter, HarnessAdapter, get_adapter, register_adapter, resolve_adapter
from .evidence import (
    Evidence,
    EvidenceRecord,
    EvidenceWriter,
    JsonlRecorder,
    Trace,
    TraceEvent,
    TraceRecord,
    TraceWriter,
    append_jsonl,
    iter_jsonl,
    write_evidence,
    write_trace,
)
from .runner import RunRecord, RunResult, parse_metrics, run_command, run_validation
from .schema import AdapterSpec, ComponentSpec, HarnessSpec, ManifestError, ValidationSpec, find_manifest, load_spec
from .schema import RuntimeSpec
from .diagnosis import DIAGNOSIS_SCHEMA_VERSION, DIAGNOSIS_TYPE, normalize_diagnosis
from .workspace import CandidateWorkspace, WorkspaceError, apply_changes, create_candidate, rollback_candidate

__all__ = [
    "AdapterSpec", "CandidateWorkspace", "Command", "CommandAdapter", "ComponentSpec",
    "Evidence", "EvidenceRecord", "EvidenceWriter", "HarnessAdapter", "HarnessSpec",
    "JsonlRecorder", "ManifestError", "RunRecord", "RunResult", "RuntimeSpec", "Trace", "TraceEvent",
    "TraceRecord", "TraceWriter", "ValidationSpec", "WorkspaceError",
    "append_jsonl", "apply_changes", "create_candidate", "find_manifest", "get_adapter",
    "iter_jsonl", "load_spec", "parse_metrics", "register_adapter", "resolve_adapter",
    "rollback_candidate", "run_command", "run_validation", "write_evidence", "write_trace",
    "DIAGNOSIS_SCHEMA_VERSION", "DIAGNOSIS_TYPE", "normalize_diagnosis",
]
