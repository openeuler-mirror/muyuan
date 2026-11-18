"""候选校验与真实评测执行（dev/holdout 阶段）。"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from harness_optimizer.adapter import resolve_adapter
from harness_optimizer.evidence import write_evidence, write_trace
from harness_optimizer.runner import parse_metrics, run_command
from harness_optimizer.schema import HarnessSpec
from harness_optimizer.workspace import CandidateWorkspace

from .errors import ComponentModeError
from .metrics import _normalise_metrics
from .runenv import _env_for_run, _render_adapter_command

def _evaluate(
    root: Path,
    command: str | list[str] | tuple[str, ...] | None,
    task: str,
    trace_dir: Path,
    spec: HarnessSpec,
    candidate_id: str,
    stage: str,
    runtime_root: str | None,
    timeout_s: float,
    adapter: Any | None = None,
) -> dict[str, Any]:
    trace_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    selected = adapter or resolve_adapter(spec)
    run_env = _env_for_run(root, task, trace_dir, spec, runtime_root, selected, stage)
    run_command_value: str | list[str] | tuple[str, ...] | None = command

    if run_command_value is None:
        builder = getattr(selected, "build_command", None)
        if callable(builder):
            run_command_value = builder(spec, root)
    else:
        run_command_value = _render_adapter_command(selected, spec, root, run_command_value, runtime_root)

    if run_command_value is None:
        raise ComponentModeError("evaluation command is not configured")
    record = run_command(
        run_command_value,
        root,
        timeout_s=timeout_s,
        env=run_env,
        shell=isinstance(run_command_value, str),
    )
    parser = selected.parse_metrics if hasattr(selected, "parse_metrics") else parse_metrics
    metrics = dict(parser(record.stdout, stderr=record.stderr))
    metrics = _normalise_metrics(metrics, record.duration_s, spec)
    metrics.update({"returncode": record.returncode, "runner_ok": record.ok, "stage": stage})
    run_id = uuid.uuid4().hex
    write_trace(trace_dir / "trace.jsonl", "evaluation", candidate_id=candidate_id,
                run_id=run_id, payload={"stage": stage, "metrics": metrics,
                                        "run": record.to_dict()})
    write_evidence(trace_dir / "evidence.jsonl", "evaluation", candidate_id=candidate_id,
                   run_id=run_id, status="passed" if record.ok else "failed",
                   metrics=metrics,
                   artifacts={"stdout": str(trace_dir / f"{run_id}.stdout.txt"),
                              "stderr": str(trace_dir / f"{run_id}.stderr.txt")},
                   details={"stage": stage})
    (trace_dir / f"{run_id}.stdout.txt").write_text(record.stdout, encoding="utf-8")
    (trace_dir / f"{run_id}.stderr.txt").write_text(record.stderr, encoding="utf-8")
    metrics["run_id"] = run_id
    metrics["elapsed_s"] = round(time.monotonic() - started, 6)
    return metrics


def _validate(candidate: CandidateWorkspace, spec: HarnessSpec,
              trace_dir: Path, task: str, runtime_root: str | None,
              adapter: Any | None = None) -> dict[str, Any]:
    command = spec.validation.command
    if command is None:
        return {"ok": True, "skipped": True}
    record = run_command(
        command,
        candidate.path,
        timeout_s=spec.validation.timeout_s,
        env=_env_for_run(candidate.path, task, trace_dir, spec, runtime_root, adapter, "validation"),
        shell=isinstance(command, str),
    )
    result = {"ok": record.ok, "returncode": record.returncode,
              "duration_s": record.duration_s, "stdout": record.stdout[-2000:],
              "stderr": record.stderr[-2000:]}
    write_trace(trace_dir / "trace.jsonl", "validation", candidate_id=candidate.candidate_id,
                payload=result)
    return result

