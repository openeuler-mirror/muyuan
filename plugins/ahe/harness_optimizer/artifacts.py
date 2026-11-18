"""AHE 运行的持久实验 artifact。

    组件 optimizer 已产出报告与 JSONL 证据。本模块提供一个无依赖的
    小型 workspace 合同，将这些结果发布为可审阅的 artifact，且不把
    写入方耦合到特定 evaluator 或 harness。

workspace 形如::

    <workspace>/
    manifest.json
    index.json
    runs/<run-id>/run.json
    runs/<run-id>/candidates/<candidate-id>/changes.json
    runs/<run-id>/candidates/<candidate-id>/candidate.diff

可在 adapter 中直接使用 ArtifactWorkspace，或用
ArtifactWorkspace.record_run 一次性导出。
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Union


SCHEMA_VERSION = 1
_METRIC_FIELDS = ("tokens", "time_s", "f1")
_LOWER_IS_BETTER = frozenset(("tokens", "time_s"))
_PROMOTED_STATUSES = frozenset(
    ("keep", "kept", "promote", "promoted", "accept", "accepted", "final", "completed")
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
PathLike = Union[str, os.PathLike]


class ArtifactWorkspaceError(ValueError):
    """artifact workspace 或记录不安全/格式非法时抛出。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    """将常见运行时对象转换为 json.dumps 可接受的值。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {
            "__type__": "bytes",
            "base64": base64.b64encode(value).decode("ascii"),
        }
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "to_dict"):
        return _jsonable(value.to_dict())
    return str(value)


def _safe_id(value: Optional[str], field_name: str, default_prefix: str) -> str:
    if value is None or not str(value).strip():
        value = "{}-{}".format(default_prefix, uuid.uuid4().hex[:12])
    value = str(value).strip()
    if not _SAFE_ID.fullmatch(value) or value in {".", ".."}:
        raise ArtifactWorkspaceError(
            "{} must be a simple workspace-safe id, got {!r}".format(field_name, value)
        )
    return value


def _safe_relative(value: Any, field_name: str = "path") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactWorkspaceError("{} must be a non-empty relative path".format(field_name))
    value = value.replace("\\", "/")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ArtifactWorkspaceError("{} must stay inside the workspace: {!r}".format(field_name, value))
    return "/".join(pure.parts)


def _number(value: Any) -> Optional[Union[int, float]]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = value
    elif isinstance(value, str):
        try:
            numeric = float(value.strip())
        except (TypeError, ValueError):
            return None
    else:
        return None
    if not math.isfinite(float(numeric)):
        return None
    if float(numeric).is_integer():
        return int(numeric)
    return float(numeric)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".{}.tmp-".format(path.name), dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_write_text(path: Path, payload: str) -> None:
    _atomic_write_bytes(path, payload.encode("utf-8"))


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"
    _atomic_write_text(path, payload)


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArtifactWorkspaceError("unable to read JSON artifact {}".format(path)) from exc
    if not isinstance(value, dict):
        raise ArtifactWorkspaceError("JSON artifact must contain an object: {}".format(path))
    return value


def _metric_source(metrics: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if metrics is None:
        return {}
    if not isinstance(metrics, Mapping):
        raise TypeError("metrics must be a mapping")
    nested = metrics.get("metrics")
    source = nested if isinstance(nested, Mapping) else metrics
    return {str(key): value for key, value in source.items()}


def _lookup_metric(source: Mapping[str, Any], aliases: Iterable[str]) -> Optional[Union[int, float]]:
    lower = {str(key).lower(): value for key, value in source.items()}
    for alias in aliases:
        if alias in source:
            number = _number(source[alias])
        else:
            number = _number(lower.get(alias.lower()))
        if number is not None:
            return number
    return None


def normalize_metrics(metrics: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """归一化 evaluator 指标并保留 evaluator 专属字段。

    f1 刻意不从 accuracy 推断。evaluator 可能上报 f1_score、
    macro_f1 或 micro_f1，这些别名会被归一化到规范 f1 字段。
    """
    source = _metric_source(metrics)
    result = {key: _jsonable(value) for key, value in source.items()}
    tokens = _lookup_metric(source, ("tokens", "total_tokens", "token_count"))
    if tokens is None:
        input_tokens = _lookup_metric(source, ("input_tokens", "prompt_tokens"))
        output_tokens = _lookup_metric(source, ("output_tokens", "completion_tokens"))
        if input_tokens is not None and output_tokens is not None:
            tokens = input_tokens + output_tokens
    time_s = _lookup_metric(source, ("time_s", "duration_s", "elapsed_s", "time"))
    f1 = _lookup_metric(source, ("f1", "f1_score", "macro_f1", "micro_f1"))
    if tokens is not None:
        result["tokens"] = tokens
    if time_s is not None:
        result["time_s"] = time_s
    if f1 is not None:
        result["f1"] = f1
    return result


def metric_delta(
    baseline: Optional[Mapping[str, Any]],
    candidate: Optional[Mapping[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """返回 token、wall time 与 F1 的差值。

    delta 恒为候选减基线。越低越好字段额外输出 savings 与
    savings_pct；F1 输出 improvement 与 improvement_pct，
    使期望方向明确。
    """
    before = normalize_metrics(baseline)
    after = normalize_metrics(candidate)
    result: Dict[str, Dict[str, Any]] = {}
    for field in _METRIC_FIELDS:
        left = _number(before.get(field))
        right = _number(after.get(field))
        delta = (right - left) if left is not None and right is not None else None
        relative = None
        if delta is not None and left not in (None, 0):
            relative = round((float(delta) / float(left)) * 100.0, 6)
        item: Dict[str, Any] = {
            "baseline": left,
            "candidate": right,
            "delta": delta,
            "relative_pct": relative,
        }
        if field in _LOWER_IS_BETTER:
            savings = -delta if delta is not None else None
            savings_pct = -relative if relative is not None else None
            item.update({"savings": savings, "savings_pct": savings_pct})
        else:
            item.update({
                "improvement": delta,
                "improvement_pct": relative,
            })
        result[field] = item
    return result


def _normalize_change_rows(changes: Optional[Union[Mapping[str, Any], Sequence[Mapping[str, Any]]]]) -> List[Dict[str, Any]]:
    if changes is None:
        return []
    rows: List[Mapping[str, Any]] = []
    if isinstance(changes, Mapping):
        # 既接受单个显式变更对象，也接受组件 optimizer 产出的
        # 常见 path->content 映射。
        if "path" in changes or "file" in changes:
            rows = [changes]
        else:
            for path, payload in changes.items():
                if isinstance(payload, Mapping):
                    row = dict(payload)
                    row.setdefault("path", str(path))
                else:
                    row = {
                        "path": str(path),
                        "operation": "modify",
                        "content": payload,
                    }
                rows.append(row)
    elif isinstance(changes, Sequence) and not isinstance(changes, (str, bytes, bytearray)):
        rows = list(changes)
    else:
        raise TypeError("changes must be a mapping or a sequence of mappings")

    normalized: List[Dict[str, Any]] = []
    for index, value in enumerate(rows):
        if not isinstance(value, Mapping):
            raise ArtifactWorkspaceError("changes[{}] must be an object".format(index))
        row = {str(key): _jsonable(item) for key, item in value.items()}
        raw_path = row.get("path", row.get("file"))
        row["path"] = _safe_relative(raw_path, "changes[{}].path".format(index))
        row.setdefault("operation", "modify")
        content = value.get("content")
        if isinstance(content, (str, bytes, bytearray)):
            raw_content = bytes(content) if isinstance(content, bytearray) else content
            if isinstance(raw_content, str):
                raw_content = raw_content.encode("utf-8")
            row.setdefault("content_sha256", hashlib.sha256(raw_content).hexdigest())
        normalized.append(row)
    return normalized


@dataclass
class ArtifactRun:
    """ArtifactWorkspace 中单个运行目录的可变句柄。"""

    workspace: "ArtifactWorkspace"
    run_id: str
    path: Path
    data: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return _jsonable(self.data)

    def _metrics(self) -> Dict[str, Any]:
        value = self.data.setdefault("metrics", {})
        if not isinstance(value, dict):
            value = {}
            self.data["metrics"] = value
        return value

    def _persist(self) -> None:
        _atomic_write_json(self.path / "run.json", self.data)
        self.workspace._refresh_index(self.data)

    def record_candidate(
        self,
        candidate_id: Optional[str],
        *,
        changes: Optional[Union[Mapping[str, Any], Sequence[Mapping[str, Any]]]] = None,
        diff: Optional[Union[str, bytes, bytearray]] = None,
        baseline_metrics: Optional[Mapping[str, Any]] = None,
        candidate_metrics: Optional[Mapping[str, Any]] = None,
        baseline_holdout_metrics: Optional[Mapping[str, Any]] = None,
        holdout_metrics: Optional[Mapping[str, Any]] = None,
        status: str = "observed",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """记录一个被提出的候选及其测量指标对比。"""
        safe_candidate = _safe_id(candidate_id, "candidate_id", "candidate")
        normalized_changes = _normalize_change_rows(changes)
        candidate_dir = self.path / "candidates" / safe_candidate
        candidate_dir.mkdir(parents=True, exist_ok=True)
        change_path = candidate_dir / "changes.json"
        _atomic_write_json(change_path, {"changes": normalized_changes})
        artifacts: Dict[str, str] = {
            "changes": str(change_path.relative_to(self.workspace.root).as_posix())
        }
        if diff is not None:
            diff_payload = diff.encode("utf-8") if isinstance(diff, str) else bytes(diff)
            diff_path = candidate_dir / "candidate.diff"
            _atomic_write_bytes(diff_path, diff_payload)
            artifacts["diff"] = str(diff_path.relative_to(self.workspace.root).as_posix())

        baseline = normalize_metrics(baseline_metrics)
        candidate = normalize_metrics(candidate_metrics)
        holdout_before = normalize_metrics(baseline_holdout_metrics)
        holdout_after = normalize_metrics(holdout_metrics)
        entry: Dict[str, Any] = {
            "candidate_id": safe_candidate,
            "status": str(status),
            "recorded_at": _utc_now(),
            "metrics": {
                "baseline": baseline,
                "candidate": candidate,
                "delta": metric_delta(baseline, candidate),
                "holdout_baseline": holdout_before,
                "holdout": holdout_after,
                "holdout_delta": metric_delta(holdout_before, holdout_after),
            },
            "changes": normalized_changes,
            "artifacts": artifacts,
            "metadata": _jsonable(metadata or {}),
        }
        candidates = self.data.setdefault("candidates", [])
        if not isinstance(candidates, list):
            candidates = []
            self.data["candidates"] = candidates
        candidates.append(entry)

        metrics = self._metrics()
        if baseline and not metrics.get("baseline"):
            metrics["baseline"] = baseline
        if baseline_holdout_metrics is not None and not metrics.get("holdout_baseline"):
            metrics["holdout_baseline"] = holdout_before
        if str(status).lower() in _PROMOTED_STATUSES:
            if candidate_metrics is not None:
                metrics["final"] = candidate
            if holdout_metrics is not None:
                metrics["holdout_final"] = holdout_after
        metrics["delta"] = metric_delta(metrics.get("baseline"), metrics.get("final"))
        metrics["holdout_delta"] = metric_delta(
            metrics.get("holdout_baseline"),
            metrics.get("holdout_final"),
        )
        self.data["updated_at"] = _utc_now()
        self._persist()
        return _jsonable(entry)

    def finish(
        self,
        *,
        status: str = "completed",
        final_metrics: Optional[Mapping[str, Any]] = None,
        final_holdout_metrics: Optional[Mapping[str, Any]] = None,
        finished_at: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """结束一个运行，可选地写入晋级指标。"""
        metrics = self._metrics()
        if final_metrics is not None:
            metrics["final"] = normalize_metrics(final_metrics)
        if final_holdout_metrics is not None:
            metrics["holdout_final"] = normalize_metrics(final_holdout_metrics)
        metrics["delta"] = metric_delta(metrics.get("baseline"), metrics.get("final"))
        metrics["holdout_delta"] = metric_delta(
            metrics.get("holdout_baseline"),
            metrics.get("holdout_final"),
        )
        self.data["status"] = str(status)
        self.data["finished_at"] = finished_at or _utc_now()
        if metadata:
            existing = self.data.setdefault("metadata", {})
            if isinstance(existing, dict):
                existing.update(_jsonable(metadata))
        self.data["updated_at"] = _utc_now()
        self._persist()
        return self.to_dict()


class ArtifactWorkspace:
    """创建并更新可审阅的 AHE 实验 artifact workspace。"""

    def __init__(
        self,
        root: PathLike,
        *,
        workspace_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        root_path = Path(root).expanduser()
        if root_path.exists() and root_path.is_symlink():
            raise ArtifactWorkspaceError("workspace root may not be a symlink")
        self.root = root_path.resolve()
        if self.root.exists() and not self.root.is_dir():
            raise ArtifactWorkspaceError("workspace root must be a directory")
        self.root.mkdir(parents=True, exist_ok=True)
        self.runs_root = self.root / "runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "manifest.json"
        self.index_path = self.root / "index.json"
        now = _utc_now()
        if self.manifest_path.exists():
            manifest = _load_json(self.manifest_path)
            if manifest.get("schema_version") != SCHEMA_VERSION:
                raise ArtifactWorkspaceError("unsupported artifact manifest schema")
            self.workspace_id = str(manifest.get("workspace_id") or _safe_id(workspace_id, "workspace_id", "workspace"))
            self.manifest = manifest
        else:
            self.workspace_id = _safe_id(workspace_id, "workspace_id", "workspace")
            self.manifest = {
                "schema_version": SCHEMA_VERSION,
                "type": "autoharness-experiment-workspace",
                "workspace_id": self.workspace_id,
                "created_at": now,
                "updated_at": now,
                "runs_dir": "runs",
                "metric_fields": list(_METRIC_FIELDS),
                "metadata": _jsonable(metadata or {}),
                "run_count": 0,
                "last_run_id": None,
            }
            _atomic_write_json(self.manifest_path, self.manifest)
        if self.index_path.exists():
            self.index = _load_json(self.index_path)
            if self.index.get("schema_version") != SCHEMA_VERSION:
                raise ArtifactWorkspaceError("unsupported artifact index schema")
        else:
            self.index = {
                "schema_version": SCHEMA_VERSION,
                "type": "autoharness-experiment-index",
                "workspace_id": self.workspace_id,
                "updated_at": now,
                "runs": [],
            }
            _atomic_write_json(self.index_path, self.index)

    def _refresh_index(self, run_data: Mapping[str, Any]) -> None:
        runs = self.index.setdefault("runs", [])
        if not isinstance(runs, list):
            runs = []
            self.index["runs"] = runs
        summary = {
            "run_id": run_data.get("run_id"),
            "harness": run_data.get("harness"),
            "task": run_data.get("task"),
            "status": run_data.get("status"),
            "started_at": run_data.get("started_at"),
            "finished_at": run_data.get("finished_at"),
            "updated_at": run_data.get("updated_at"),
            "metrics": _jsonable(run_data.get("metrics", {})),
            "candidate_count": len(run_data.get("candidates", []))
            if isinstance(run_data.get("candidates"), list) else 0,
            "run_path": "runs/{}/run.json".format(run_data.get("run_id")),
        }
        for index, existing in enumerate(runs):
            if isinstance(existing, Mapping) and existing.get("run_id") == summary["run_id"]:
                runs[index] = summary
                break
        else:
            runs.append(summary)
        runs.sort(key=lambda item: str(item.get("started_at") or ""), reverse=True)
        self.index["updated_at"] = _utc_now()
        _atomic_write_json(self.index_path, self.index)
        self.manifest["run_count"] = len(runs)
        self.manifest["last_run_id"] = runs[0].get("run_id") if runs else None
        self.manifest["updated_at"] = _utc_now()
        _atomic_write_json(self.manifest_path, self.manifest)

    def start_run(
        self,
        *,
        run_id: Optional[str] = None,
        harness: Optional[str] = None,
        task: Optional[str] = None,
        started_at: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ArtifactRun:
        safe_run = _safe_id(run_id, "run_id", "run")
        run_path = self.runs_root / safe_run
        if run_path.exists():
            raise ArtifactWorkspaceError("run already exists: {}".format(safe_run))
        run_path.mkdir(parents=True)
        now = started_at or _utc_now()
        data: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "type": "autoharness-run",
            "run_id": safe_run,
            "workspace_id": self.workspace_id,
            "harness": harness,
            "task": task,
            "status": "running",
            "started_at": now,
            "finished_at": None,
            "updated_at": now,
            "metadata": _jsonable(metadata or {}),
            "metrics": {},
            "candidates": [],
        }
        _atomic_write_json(run_path / "run.json", data)
        run = ArtifactRun(self, safe_run, run_path, data)
        self._refresh_index(data)
        return run

    def open_run(self, run_id: str) -> ArtifactRun:
        safe_run = _safe_id(run_id, "run_id", "run")
        run_path = self.runs_root / safe_run
        if not run_path.is_dir():
            raise FileNotFoundError(run_path)
        data = _load_json(run_path / "run.json")
        if data.get("run_id") != safe_run:
            raise ArtifactWorkspaceError("run id does not match its path")
        return ArtifactRun(self, safe_run, run_path, data)

    def record_run(
        self,
        *,
        run_id: Optional[str] = None,
        harness: Optional[str] = None,
        task: Optional[str] = None,
        baseline_metrics: Optional[Mapping[str, Any]] = None,
        final_metrics: Optional[Mapping[str, Any]] = None,
        baseline_holdout_metrics: Optional[Mapping[str, Any]] = None,
        final_holdout_metrics: Optional[Mapping[str, Any]] = None,
        candidate_id: Optional[str] = "final",
        changes: Optional[Union[Mapping[str, Any], Sequence[Mapping[str, Any]]]] = None,
        diff: Optional[Union[str, bytes, bytearray]] = None,
        status: str = "completed",
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """一次调用写完整的运行、候选 diff 与指标差值。"""
        run = self.start_run(
            run_id=run_id,
            harness=harness,
            task=task,
            started_at=started_at,
            metadata=metadata,
        )
        has_candidate = (
            final_metrics is not None
            or final_holdout_metrics is not None
            or changes is not None
            or diff is not None
        )
        if has_candidate:
            run.record_candidate(
                candidate_id,
                changes=changes,
                diff=diff,
                baseline_metrics=baseline_metrics,
                candidate_metrics=final_metrics,
                baseline_holdout_metrics=baseline_holdout_metrics,
                holdout_metrics=final_holdout_metrics,
                status="final" if status == "completed" else status,
            )
        elif baseline_metrics is not None:
            run._metrics()["baseline"] = normalize_metrics(baseline_metrics)
        run.finish(
            status=status,
            final_metrics=final_metrics,
            final_holdout_metrics=final_holdout_metrics,
            finished_at=finished_at,
        )
        return run.to_dict()


__all__ = [
    "ArtifactRun",
    "ArtifactWorkspace",
    "ArtifactWorkspaceError",
    "SCHEMA_VERSION",
    "metric_delta",
    "normalize_metrics",
]
