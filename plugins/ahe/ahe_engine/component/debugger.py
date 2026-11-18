"""失败诊断：外部命令 debugger 或内置 LLM debugger。"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from harness_optimizer.diagnosis import normalize_diagnosis
from harness_optimizer.evidence import append_jsonl, write_evidence, write_trace
from harness_optimizer.runner import run_command
from harness_optimizer.schema import HarnessSpec

from .commands import _debugger_command
from .errors import LLMCall
from .metrics import _objective, _primary_metric
from .runenv import _env_for_run
from .util import _as_float, _json_value, _last_json_object, _strip_json_fence

def _render_debugger_command(
    command: str | list[str] | tuple[str, ...],
    *,
    root: Path,
    trace_dir: Path,
    task: str,
    stage: str,
    candidate_id: str,
    report_path: Path,
) -> str | list[str] | tuple[str, ...]:
    values = {
        "workspace": str(root),
        "harness": str(root),
        "trace_dir": str(trace_dir),
        "task": task,
        "stage": stage,
        "candidate_id": candidate_id,
        "report_path": str(report_path),
        "metrics_path": str(report_path),
    }
    def render(value: str) -> str:
        for key, replacement in values.items():
            value = value.replace("{" + key + "}", replacement)
        return value
    if isinstance(command, str):
        return render(command)
    return tuple(render(str(item)) for item in command)



def _run_debugger(
    root: Path,
    task: str,
    trace_dir: Path,
    spec: HarnessSpec,
    candidate_id: str,
    stage: str,
    metrics: Mapping[str, Any],
    runtime_root: str | None,
    timeout_s: float,
    adapter: Any | None = None,
    explicit_command: str | list[str] | tuple[str, ...] | None = None,
    api: Mapping[str, str] | None = None,
    call_llm: LLMCall | None = None,
    trigger: str = "evaluation",
) -> dict[str, Any]:
    """针对已记录的 trace/evaluator 输出运行可选 debugger。"""
    command = _debugger_command(spec, explicit_command)
    if command is None and not (api and api.get("url") and api.get("key") and call_llm):
        return {"configured": False, "reason": "no debugger command or debugger API"}
    debug_dir = trace_dir / "debugger"
    debug_dir.mkdir(parents=True, exist_ok=True)
    report_path = debug_dir / f"{candidate_id}-{stage}.metrics.json"
    report_path.write_text(json.dumps(_json_value(metrics), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if command is None:
        # 为未声明独立 debugger 进程的 manifest 保留内置 LLM debugger。
        # 它接收有界的 trace/evidence，只返回诊断；变异仍是下一个独立步骤。
        trace_excerpt = ""
        for artifact_name in ("trace.jsonl", "evidence.jsonl"):
            artifact_path = trace_dir / artifact_name
            if artifact_path.is_file():
                try:
                    trace_excerpt += f"\n--- {artifact_name} ---\n{artifact_path.read_text(encoding='utf-8')[-12000:]}"
                except (OSError, UnicodeDecodeError):
                    pass
        prompt = f"""Analyze this failed or regressed harness evaluation.
Do not propose code and do not edit files. Return JSON only with these fields:
root_cause (string), failure_modes (array of strings), summary (string),
general_mechanism (string), recommended_focus (array of strings),
evidence_ids (array of strings), evidence (array of objects with id/kind/detail),
impact (object with performance/cost/latency strings), risks (array of strings),
and confidence (number from 0 to 1). Keep the diagnosis generalizable to the
harness and cite only evidence present in the trace or metrics.

Task: {task}
Stage: {stage}
Metrics: {json.dumps(_json_value(metrics), ensure_ascii=False)}
Trace/evidence (bounded):
{trace_excerpt[-22000:]}
"""
        started = time.monotonic()
        try:
            model = api.get("debug_model") or api.get("evolve_model") or api.get("model") or ""
            text, usage_tokens, llm_time = call_llm(  # type: ignore[misc]
                api["url"], api["key"], model,
                [{"role": "system", "content": "Return a concise JSON diagnosis only."},
                 {"role": "user", "content": prompt}],
                max_tokens=4096,
            )
            analysis = _last_json_object(_strip_json_fence(text)) or {"summary": text[-6000:]}
            stdout = text
            stderr = ""
            diagnosis = normalize_diagnosis(
                analysis,
                task=task,
                stage=stage,
                candidate_id=candidate_id,
                metrics=metrics,
                source="builtin_llm",
                trigger=trigger,
            )
            result: dict[str, Any] = {
                "configured": True,
                "kind": "llm",
                "ok": True,
                "returncode": 0,
                "timed_out": False,
                "duration_s": round(llm_time or (time.monotonic() - started), 6),
                "task": task,
                "candidate_id": candidate_id,
                "stage": stage,
                "trigger": trigger,
                "run_id": uuid.uuid4().hex,
                "analysis": dict(analysis),
                "diagnosis": diagnosis,
                "debugger_tokens": usage_tokens,
                "metrics_path": str(report_path),
                "trace_dir": str(trace_dir),
            }
        except Exception as exc:
            stdout = ""
            stderr = str(exc)
            result = {
                "configured": True, "kind": "llm", "ok": False,
                "returncode": None, "timed_out": False, "duration_s": round(time.monotonic() - started, 6),
                "task": task, "candidate_id": candidate_id, "stage": stage,
                "trigger": trigger, "run_id": uuid.uuid4().hex,
                "analysis": None, "diagnosis": normalize_diagnosis(
                    {"summary": f"Debugger failed: {exc}"},
                    task=task,
                    stage=stage,
                    candidate_id=candidate_id,
                    metrics=metrics,
                    source="builtin_llm",
                    trigger=trigger,
                ), "error": str(exc),
                "metrics_path": str(report_path), "trace_dir": str(trace_dir),
            }
        diagnosis_path = debug_dir / f"{result['run_id']}.diagnosis.json"
        diagnosis_path.write_text(
            json.dumps(result["diagnosis"], ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        result["diagnosis_path"] = str(diagnosis_path)
        append_jsonl(trace_dir / "diagnosis.jsonl", result["diagnosis"])
        stdout_path = debug_dir / f"{result['run_id']}.stdout.txt"
        stderr_path = debug_dir / f"{result['run_id']}.stderr.txt"
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        result["artifacts"] = {"stdout": str(stdout_path), "stderr": str(stderr_path)}
        (debug_dir / f"{result['run_id']}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
        )
        write_trace(trace_dir / "trace.jsonl", "debugger", candidate_id=candidate_id,
                    run_id=str(result["run_id"]), payload=result)
        write_evidence(trace_dir / "evidence.jsonl", "debugger", candidate_id=candidate_id,
                       run_id=str(result["run_id"]), status="passed" if result["ok"] else "failed",
                       metrics=metrics, artifacts={"stdout": str(stdout_path), "stderr": str(stderr_path),
                                                    "metrics": str(report_path), "diagnosis": str(diagnosis_path)},
                       details={"stage": stage, "analysis": result.get("analysis") or {},
                                "diagnosis": result.get("diagnosis") or {}})
        return result
    rendered = _render_debugger_command(command, root=root, trace_dir=trace_dir, task=task,
                                        stage=stage, candidate_id=candidate_id,
                                        report_path=report_path)
    env = _env_for_run(root, task, trace_dir, spec, runtime_root, adapter, f"debug:{stage}")
    env.update({
        "AHE_DEBUGGER": "1",
        "AHE_DEBUGGER_REPORT": str(report_path),
        "AHE_DEBUGGER_TRACE": str(trace_dir),
        "AHE_FAILURE_METRICS": str(report_path),
    })
    record = run_command(rendered, root, timeout_s=timeout_s, env=env,
                         shell=isinstance(rendered, str))
    run_id = uuid.uuid4().hex
    stdout_path = debug_dir / f"{run_id}.stdout.txt"
    stderr_path = debug_dir / f"{run_id}.stderr.txt"
    stdout_path.write_text(record.stdout, encoding="utf-8")
    stderr_path.write_text(record.stderr, encoding="utf-8")
    analysis = _last_json_object(record.stdout)
    diagnosis_input = analysis or {
        "summary": "Debugger did not emit a JSON object; inspect stdout/stderr artifacts.",
    }
    diagnosis = normalize_diagnosis(
        diagnosis_input,
        task=task,
        stage=stage,
        candidate_id=candidate_id,
        metrics=metrics,
        source="external_command",
        trigger=trigger,
    )
    result: dict[str, Any] = {
        "configured": True,
        "ok": record.ok,
        "returncode": record.returncode,
        "timed_out": record.timed_out,
        "duration_s": record.duration_s,
        "task": task,
        "candidate_id": candidate_id,
        "stage": stage,
        "trigger": trigger,
        "run_id": run_id,
        "analysis": dict(analysis) if analysis else None,
        "diagnosis": diagnosis,
        "stdout_tail": record.stdout[-6000:],
        "stderr_tail": record.stderr[-4000:],
        "metrics_path": str(report_path),
        "trace_dir": str(trace_dir),
        "artifacts": {"stdout": str(stdout_path), "stderr": str(stderr_path)},
    }
    diagnosis_path = debug_dir / f"{run_id}.diagnosis.json"
    diagnosis_path.write_text(
        json.dumps(diagnosis, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    result["diagnosis_path"] = str(diagnosis_path)
    append_jsonl(trace_dir / "diagnosis.jsonl", diagnosis)
    (debug_dir / f"{run_id}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    write_trace(trace_dir / "trace.jsonl", "debugger", candidate_id=candidate_id,
                run_id=run_id, payload=result)
    write_evidence(trace_dir / "evidence.jsonl", "debugger", candidate_id=candidate_id,
                   run_id=run_id, status="passed" if record.ok else "failed",
                   metrics=metrics, artifacts={"stdout": str(stdout_path), "stderr": str(stderr_path),
                                                "metrics": str(report_path), "diagnosis": str(diagnosis_path)},
                   details={"stage": stage, "analysis": analysis or {}, "diagnosis": diagnosis})
    return result


def _needs_debug(metrics: Mapping[str, Any], reference: Mapping[str, Any] | None,
                 spec: HarnessSpec) -> bool:
    if not metrics.get("runner_ok"):
        return True
    if metrics.get("failed") is True or str(metrics.get("status", "")).lower() in {
        "failed", "error", "timeout", "timed_out",
    }:
        return True
    # 聚合分数类 evaluator 汇报总成绩，但通过的 runner 仍可能包含
    # 失败任务。将零分任务条目交给 debugger，使下一轮提案拿到真实失败轨迹。
    task_scores = metrics.get("task_scores")
    if isinstance(task_scores, (list, tuple)):
        for item in task_scores:
            if not isinstance(item, Mapping):
                continue
            score = _as_float(item.get("score"))
            if score is not None and score <= 0:
                return True
    _, current = _primary_metric(metrics, spec)
    if current is None:
        return True
    if current == 0:
        return True
    if reference is None:
        return False
    _, previous = _primary_metric(reference, spec)
    if previous is None:
        return False
    _, direction = _objective(spec)
    return current < previous if direction == "maximize" else current > previous


