"""组件模式 campaign 主循环。

baseline -> propose -> apply -> validate -> evaluate -> gate ->
keep（累积进 best）/ revert（进失败台账）-> promote/export。
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from harness_optimizer.adapter import resolve_adapter
from harness_optimizer.evidence import write_trace
from harness_optimizer.schema import HarnessSpec
from harness_optimizer.workspace import apply_changes, apply_patches, create_candidate

from .commands import (
    _evaluation_command,
    _expand_command_env,
    _manifest_for_root,
    _runtime_root,
)
from .debugger import _needs_debug, _run_debugger
from .errors import ComponentModeError, LLMCall
from .evaluate import _evaluate, _validate
from .gates import _acceptable
from .metrics import _metrics_summary, _objective
from .patches import _record_persistent_failure
from .propose import _propose
from .util import _as_float, _milestone

def _promote(best_root: Path, source_root: Path, spec: HarnessSpec) -> None:
    for component in spec.all_components:
        if not component.mutable:
            continue
        source = best_root / Path(component.path)
        target = source_root / Path(component.path)
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            source_files = {item.relative_to(source) for item in source.rglob("*") if item.is_file()}
            for existing in sorted(target.rglob("*"), reverse=True):
                relative = existing.relative_to(target)
                if existing.is_file() and relative not in source_files:
                    existing.unlink()
            for item in source.rglob("*"):
                relative = item.relative_to(source)
                destination = target / relative
                if item.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                elif item.is_file():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, destination)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        elif target.exists():
            target.unlink() if target.is_file() else shutil.rmtree(target)


def run_component_mode(
    *,
    harness_path_str: str,
    manifest_path: str | None,
    task_prompt: str,
    targets: Mapping[str, Any],
    rounds: int,
    eval_command: str | None,
    holdout_eval_command: str | None,
    baseline_only: bool,
    api: Mapping[str, str],
    call_llm: LLMCall,
    adapter_name: str | None = None,
    workspace_dir: str | None = None,
    trace_dir: str | None = None,
    runtime_root: str | None = None,
    write_back: bool = True,
    debug_command: str | list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any] | None:
    requested_root = Path(harness_path_str).expanduser().resolve()
    if not requested_root.is_dir():
        raise ComponentModeError("component mode requires a harness directory")
    spec = _manifest_for_root(manifest_path, requested_root)
    if adapter_name:
        spec = replace(spec, adapter=replace(spec.adapter, name=adapter_name))
    try:
        adapter = resolve_adapter(spec)
    except KeyError as exc:
        raise ComponentModeError(str(exc)) from exc

    # manifest 可以放在 AHE 里，而可变 runtime 在另一个 checkout。候选
    # workspace 必须复制 runtime root，否则针对运行时资产的 patch 不会
    # 被真正评测和晋级。
    requested_write_back = bool(write_back)
    frozen_setting = spec.metadata.get("baseline_frozen", spec.metadata.get("freeze_baseline"))
    if isinstance(frozen_setting, Mapping):
        frozen_setting = frozen_setting.get("frozen")
    baseline_frozen = frozen_setting is True
    # 声明冻结基线的运行（benchmark 类目标）始终通过隔离候选进行优化，
    # 绝不会被意外晋级。
    if baseline_frozen:
        write_back = False
    external_root = _runtime_root(spec, requested_root, runtime_root)
    if external_root is not None:
        source_root = external_root
        spec = replace(spec, root=source_root)
    else:
        source_root = requested_root
    command = _expand_command_env(_evaluation_command(spec, eval_command))
    holdout_command = _expand_command_env(holdout_eval_command or spec.adapter.config.get("holdout_command"))
    if command is None and not callable(getattr(adapter, "build_command", None)):
        raise ComponentModeError("component mode requires an evaluation command or adapter command")
    if rounds > 0 and not baseline_only and (not api.get("url") or not api.get("key")):
        raise ComponentModeError("component optimization requires an LLM API configuration")

    trace_root = (Path(trace_dir).expanduser().resolve()
                  if trace_dir else source_root.parent / f".{source_root.name}.ahe-traces")
    trace_root.mkdir(parents=True, exist_ok=True)
    workspace_root = Path(workspace_dir).expanduser().resolve() if workspace_dir else source_root.parent / ".ahe-candidates"
    workspace_root.mkdir(parents=True, exist_ok=True)
    # 连 baseline 评测也在一次性快照中运行。evaluator 通常会在
    # harness 旁边创建日志/状态；直接在 ``source_root`` 上运行会在
    # 首个变异提出之前就破坏冻结基线约定。
    baseline_candidate = create_candidate(
        source_root, workspace_root=workspace_root,
        candidate_id=f"baseline-{uuid.uuid4().hex[:8]}", spec=spec,
        allow_symlinks=baseline_frozen,
    )
    baseline_root = baseline_candidate.path
    # AHE_REUSE_BASELINE：从上一次运行的报告加载 baseline，跳过重新测量。
    # 复用只跳过指标测量；上面创建的 baseline candidate 仍是 holdout
    # 评测与后续提案所需的当前源快照，直接复用，绝不重复创建第二份
    # 完整 checkout（否则每次复用都会额外滞留一份不被引用的目录）。
    baseline_reuse_path = os.environ.get("AHE_REUSE_BASELINE", "")
    if baseline_reuse_path and Path(baseline_reuse_path).is_file():
        print(f"[baseline] reusing metrics from {baseline_reuse_path}", flush=True)
        _reused = json.loads(Path(baseline_reuse_path).read_text(encoding="utf-8"))
        baseline = _reused.get("metrics") or _reused
        baseline_holdout = None
        _milestone(f"baseline reused from previous run: {_metrics_summary(baseline, spec)}")
    else:
        _milestone("baseline eval start")
        baseline = _evaluate(baseline_root, command, task_prompt, trace_root, spec,
                         baseline_candidate.candidate_id, "dev", runtime_root,
                         spec.validation.timeout_s, adapter)
        _milestone(f"baseline eval done: {_metrics_summary(baseline, spec)}")
    baseline_holdout = (_evaluate(baseline_root, holdout_command, task_prompt, trace_root,
                                  spec, baseline_candidate.candidate_id, "holdout",
                                  runtime_root, spec.validation.timeout_s, adapter)
                        if holdout_command else None)
    baseline_debug: list[dict[str, Any]] = []
    if _needs_debug(baseline, None, spec):
        baseline_debug.append(_run_debugger(
            baseline_root, task_prompt, trace_root, spec,
            baseline_candidate.candidate_id, "dev", baseline, runtime_root,
            spec.validation.timeout_s, adapter, debug_command, api, call_llm,
            trigger="baseline",
        ))
    if baseline_holdout is not None and _needs_debug(baseline_holdout, None, spec):
        baseline_debug.append(_run_debugger(
            baseline_root, task_prompt, trace_root, spec,
            baseline_candidate.candidate_id, "holdout", baseline_holdout, runtime_root,
            spec.validation.timeout_s, adapter, debug_command, api, call_llm,
            trigger="baseline_holdout",
        ))
    history: list[dict[str, Any]] = [{"round": 0, "action": "baseline", "metrics": baseline,
                                      "holdout": baseline_holdout,
                                      "candidate": str(baseline_root),
                                      "debugger": baseline_debug or None}]
    report_path = trace_root / "ahe-component-report.json"
    eval_task_project = os.environ.get("AHE_EVAL_TASK_PROJECT", "")
    if baseline_only:
        report = {"mode": "component", "manifest": str(spec.manifest_path or ""),
                  "adapter": spec.adapter.name,
                  "objective": dict(zip(("metric", "direction"), _objective(spec))),
                  "harness": str(source_root), "task": task_prompt, "targets": dict(targets),
                  "eval_task_project": eval_task_project,
                  "rounds": 0, "baseline": baseline, "baseline_holdout": baseline_holdout,
                  "final": baseline, "history": history, "write_back": False,
                  "requested_write_back": requested_write_back,
                  "baseline_frozen": baseline_frozen,
                  "baseline_candidate": str(baseline_root),
                  "trace_dir": str(trace_root), "workspace_dir": str(workspace_root)}
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        return report

    best_root = baseline_root
    best_metrics = baseline
    best_holdout = baseline_holdout
    best_changes: dict[str, str | None] = {}
    best_patches: list[dict[str, Any]] = []
    changes_history: list[dict[str, Any]] = []
    keep_target = int(os.environ.get("AHE_KEEP_TARGET", "0") or 0)
    keep_count = 0
    round_number = 0
    noop_streak = 0
    max_noop_streak = int(os.environ.get("AHE_MAX_NOOP_STREAK", "5") or 5)
    while round_number < rounds:
        if keep_target > 0 and keep_count >= keep_target:
            break
        round_number += 1
        candidate = create_candidate(source_root,
                                     workspace_root=workspace_root,
                                     candidate_id=f"round-{round_number}-{uuid.uuid4().hex[:8]}",
                                     spec=spec, allow_symlinks=baseline_frozen)
        try:
            if best_changes:
                apply_changes(candidate, best_changes, spec=spec)
            if best_patches:
                apply_patches(candidate, best_patches, spec=spec)
            proposal = _propose(api, call_llm, best_root, spec, task_prompt, best_metrics, history)
            _pc = proposal.get("changes", {})
            _pp = proposal.get("patches", [])
            _targets = [str(_p.get("component_id") or _p.get("path") or _p.get("file") or "?")
                        for _p in _pp]
            _milestone(f"round-{round_number}: evolve proposed {len(_pc)} changes, {len(_pp)} patches"
                       + (f" -> {', '.join(_targets[:5])}{' ...' if len(_targets) > 5 else ''}"
                          if _targets else ""))
            if not proposal.get("changes") and not proposal.get("patches"):
                # 空提案候选绝不能靠运行时/token 的噪声测量获胜。
                # 旧循环会评测空提案，可能意外保留未变化的候选。
                _milestone(f"round-{round_number}: empty proposal, reverting (noop_streak={noop_streak + 1})")
                history.append({"round": round_number, "action": "revert",
                                "reason": "empty_proposal", "proposal": proposal})
                changes_history.append({"round": round_number, **proposal,
                                        "action": "revert", "reason": "empty_proposal"})
                candidate.cleanup()
                # 空提案不消耗尝试预算（只有真实提案及其评测才消耗）。
                # evolve 持续拒绝提案时中止运行。
                round_number -= 1
                noop_streak += 1
                if noop_streak >= max_noop_streak:
                    _milestone(f"aborting: {noop_streak} consecutive empty proposals")
                    break
                continue
            noop_streak = 0
            apply_changes(candidate, proposal["changes"], spec=spec)
            apply_patches(candidate, proposal.get("patches", []), spec=spec)
            _milestone(f"round-{round_number}: patch applied to {candidate.candidate_id}, starting validation")
            validation = _validate(candidate, spec, trace_root, task_prompt, runtime_root, adapter)
            if not validation["ok"]:
                _milestone(f"round-{round_number}: validation FAILED, reverting")
                history.append({"round": round_number, "action": "revert", "reason": "validation_failed",
                                "validation": validation, "proposal": proposal})
                changes_history.append({"round": round_number, **proposal, "action": "revert",
                                        "validation": validation})
                candidate.cleanup()
                # 只有候选被保留后才计入编号轮次。
                round_number -= 1
                noop_streak += 1
                if noop_streak >= max_noop_streak:
                    _milestone(f"aborting: {noop_streak} consecutive validation failures")
                    break
                continue
            _milestone(f"round-{round_number}: validation ok, starting dev eval")
            candidate_metrics = _evaluate(candidate.path, command, task_prompt, trace_root,
                                          spec, candidate.candidate_id, "dev", runtime_root, spec.validation.timeout_s, adapter)
            _milestone(f"round-{round_number}: dev eval done: {_metrics_summary(candidate_metrics, spec)}")
            candidate_holdout = (_evaluate(candidate.path, holdout_command, task_prompt, trace_root,
                                            spec, candidate.candidate_id, "holdout", runtime_root, spec.validation.timeout_s, adapter)
                                 if holdout_command else None)
            if candidate_holdout is not None:
                _milestone(f"round-{round_number}: holdout eval done: {_metrics_summary(candidate_holdout, spec)}")
            debugger_results: list[dict[str, Any]] = []
            if _needs_debug(candidate_metrics, best_metrics, spec):
                debugger_results.append(_run_debugger(
                    candidate.path, task_prompt, trace_root, spec,
                    candidate.candidate_id, "dev", candidate_metrics, runtime_root,
                    spec.validation.timeout_s, adapter, debug_command, api, call_llm,
                    trigger="candidate",
                ))
            if candidate_holdout is not None and _needs_debug(candidate_holdout, best_holdout, spec):
                debugger_results.append(_run_debugger(
                    candidate.path, task_prompt, trace_root, spec,
                    candidate.candidate_id, "holdout", candidate_holdout, runtime_root,
                    spec.validation.timeout_s, adapter, debug_command, api, call_llm,
                    trigger="candidate_holdout",
                ))
            for _dbg in debugger_results:
                _rc = _dbg.get("diagnosis") if isinstance(_dbg, dict) else None
                _rc = _rc.get("root_cause", "") if isinstance(_rc, dict) else ""
                _milestone(f"round-{round_number}: debugger: root_cause={str(_rc)[:140]}")
            keep = _acceptable(candidate_metrics, best_metrics, candidate_holdout, best_holdout, spec)
            action = "keep" if keep else "revert"
            _milestone(f"round-{round_number}: {action.upper()}"
                       f" 鈥?candidate[{_metrics_summary(candidate_metrics, spec)}]"
                       f" vs best[{_metrics_summary(best_metrics, spec)}]")
            row = {"round": round_number, "action": action, "metrics": candidate_metrics,
                   "holdout": candidate_holdout, "proposal": proposal, "validation": validation,
                   "candidate": str(candidate.path), "debugger": debugger_results or None}
            if keep:
                keep_count += 1
            else:
                for p in (proposal.get("patches") or []):
                    _record_persistent_failure({
                        "run_id": str(trace_root.name),
                        "round": round_number,
                        "summary": str(p.get("reason", p.get("operation", "edit")))[:120],
                        "operation": p.get("operation"),
                        "old": str(p.get("old", ""))[:400],
                        "new": str(p.get("new", ""))[:400],
                        "outcome": f"reverted: {_objective(spec)[0]}="
                                    f"{_as_float(candidate_metrics.get(_objective(spec)[0]))} "
                                    f"keeps={_as_float(candidate_metrics.get('kept_iterations'))}",
                    })
            history.append(row)
            changes_history.append({"round": round_number, **proposal, "action": action,
                                    "metrics": candidate_metrics, "holdout": candidate_holdout,
                                    "debugger": debugger_results or None})
            if keep:
                noop_streak = 0
                best_root = candidate.path
                best_metrics = candidate_metrics
                best_holdout = candidate_holdout
                best_changes.update(proposal["changes"])
                best_patches.extend(proposal.get("patches", []))
            else:
                candidate.cleanup()
                # 本轮产出被接受的候选之前，
                # 不推进到下一个编号轮次。
                round_number -= 1
                noop_streak += 1
                if noop_streak >= max_noop_streak:
                    _milestone(f"aborting: {noop_streak} consecutive reverts")
                    break
                continue
        except Exception as exc:
            _milestone(f"round-{round_number}: error, reverting: {str(exc)[:180]}")
            failed_proposal = locals().get("proposal")
            history.append({"round": round_number, "action": "revert", "error": str(exc),
                            "proposal": failed_proposal if isinstance(failed_proposal, Mapping) else None})
            changes_history.append({"round": round_number, "action": "revert", "error": str(exc),
                                    "proposal": failed_proposal if isinstance(failed_proposal, Mapping) else None})
            if isinstance(failed_proposal, Mapping):
                for failed_patch in (failed_proposal.get("patches") or []):
                    _record_persistent_failure({
                        "run_id": str(trace_root.name),
                        "round": round_number,
                        "summary": str(failed_patch.get("reason", failed_patch.get("operation", "edit")))[:120],
                        "operation": failed_patch.get("operation"),
                        "old": str(failed_patch.get("old", failed_patch.get("anchor", "")))[:400],
                        "new": str(failed_patch.get("new", failed_patch.get("replacement", "")))[:400],
                        "outcome": f"failed: {str(exc)[:240]}",
                    })
            # apply/propose 失败（超时、锚点错误）同样不消耗尝试预算；
            # 记录日志并防止无限自旋。
            try:
                write_trace(trace_root / "trace.jsonl", "round_error",
                            candidate_id=f"round-{round_number}-error",
                            payload={"round": round_number, "error": str(exc)[:500]})
            except Exception:
                pass
            round_number -= 1
            noop_streak += 1
            if noop_streak >= max_noop_streak:
                _milestone(f"aborting: {noop_streak} consecutive failed/no-op rounds")
                break
            candidate.cleanup()

    backup = None
    if write_back and (best_changes or best_patches):
        backup_path = source_root.parent / f"{source_root.name}.ahe-backup"
        if backup_path.exists():
            shutil.rmtree(backup_path) if backup_path.is_dir() else backup_path.unlink()
        shutil.copytree(source_root, backup_path, symlinks=True)
        _promote(best_root, source_root, spec)
        backup = str(backup_path)
    report = {"mode": "component", "manifest": str(spec.manifest_path or ""),
              "adapter": spec.adapter.name, "objective": dict(zip(("metric", "direction"), _objective(spec))),
              "harness": str(source_root), "task": task_prompt,
              "eval_task_project": eval_task_project,
              "targets": dict(targets), "rounds": rounds, "baseline": baseline,
              "baseline_holdout": baseline_holdout, "final": best_metrics,
              "final_holdout": best_holdout, "history": history,
              "change_manifest": changes_history,
              "write_back": bool(write_back and (best_changes or best_patches)),
              "requested_write_back": requested_write_back,
              "baseline_frozen": baseline_frozen,
              "accepted_candidate": str(best_root) if (best_changes or best_patches) else None,
              "baseline_candidate": str(baseline_root),
              "backup": backup, "trace_dir": str(trace_root), "workspace_dir": str(workspace_root)}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    _maybe_export_patch_report(report_path)
    return report


def _maybe_export_patch_report(report_path: Path) -> None:
    """campaign 结束后写入 harness/patches 风格的报告目录。

    默认启用；AHE_PATCH_REPORT=0 关闭，AHE_PATCH_REPORT_DIR 重定向
    输出（例如写入仓库的 harness/patches/<...>）。报告失败绝不能
    掩盖一次已完成的 campaign。
    """
    if os.environ.get("AHE_PATCH_REPORT", "1") == "0":
        return
    try:
        from patch_report import export_patch_report
        target = os.environ.get("AHE_PATCH_REPORT_DIR") or None
        export_patch_report(report_path, target)
    except Exception as exc:
        print(f"[patch-report] export failed (campaign result unaffected): {exc}", flush=True)
