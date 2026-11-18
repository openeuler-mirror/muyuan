"""``python3 ahe.py`` 的 argparse 入口。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from . import PLUGIN_ROOT
from .api_config import load_api_config
from .artifacts import _publish_report_artifacts
from .evaluator_gen import generate_evaluator
from .export import export_best
from .llm_client import call_llm
from .preflight import _looks_like_missing_dependency, install_harness, preflight


def main():
    ap = argparse.ArgumentParser(description="AHE: optimize any harness")
    ap.add_argument("harness", nargs="?", help="harness path (file or directory)")
    ap.add_argument("--task", "-t", help="task description")
    ap.add_argument("--task-root",
                    help="read-only workload/task checkout used during adapter discovery")
    ap.add_argument("--eval-task",
                    help="read-only evaluation task root (benchmark checkout); exposed to "
                         "evaluators via AHE_EVAL_TASK_ROOT and defaults --task-root")
    ap.add_argument("--eval-project",
                    help="target project inside the eval task; exposed to "
                         "evaluators via AHE_EVAL_TASK_PROJECT")
    ap.add_argument("--test", nargs="*", help="test cases: 'input|expected'")
    ap.add_argument("--eval-cmd", help="custom evaluation command (optional)")
    ap.add_argument("--holdout-eval-cmd",
                    help="independent holdout evaluator for component mode")
    ap.add_argument("--manifest",
                    help="component harness manifest (enables executable component mode)")
    ap.add_argument("--adapter", default="auto",
                    help="component adapter name (default: manifest adapter)")
    ap.add_argument("--discover-adapter", action="store_true",
                    help="generate a conservative harness-surface manifest and exit")
    ap.add_argument("--output-manifest",
                    help="where to write the generated adapter manifest")
    ap.add_argument("--include-medium", action="store_true",
                    help="include medium-confidence harness files in discovery output")
    ap.add_argument("--generate-evaluator", nargs="?", const="", default=None, metavar="PATH",
                    help="generate an evaluator script from the task + manifest (+ --eval-task) "
                         "using the evolve model, write the objective into the manifest, and "
                         "print the recommended --eval-cmd; defaults to ahe_evaluator.py next "
                         "to the manifest")
    ap.add_argument("--export-best", metavar="REPORT",
                    help="export the final accepted candidate from a finished run's "
                         "ahe-component-report.json as a ready-to-use harness and exit")
    ap.add_argument("--export-patch-report", metavar="REPORT",
                    help="build a harness/patches-style review folder (README.md + "
                         "per-round cumulative .patch/.md) from a finished run's "
                         "ahe-component-report.json and exit")
    ap.add_argument("--target",
                    help="export destination for --export-best / --export-patch-report "
                         "(defaults: <harness>.ahe-best / <trace>/patch-report)")
    ap.add_argument("--register", action="store_true",
                    help="(default behavior) with --export-best: register the exported "
                         "harness as a Pi package so its commands load in future Pi sessions")
    ap.add_argument("--no-register", action="store_true",
                    help="with --export-best: skip the automatic Pi package registration")
    ap.add_argument("--as-suffix", metavar="SUFFIX",
                    help="with --export-best: command suffix for the exported harness; "
                         "defaults to the eval task name (--eval-project), following the "
                         "<harness>-<task> naming rule, e.g. --as-suffix pypto turns "
                         "/my-tool into /my-tool-pypto")
    ap.add_argument("--no-rename", action="store_true",
                    help="with --export-best: keep the original command names "
                         "(risks colliding with the source harness)")
    ap.add_argument("--preflight", action="store_true",
                    help="read-only pre-run check from the source directory: is the harness "
                         "installed, and how does it run; then exit")
    ap.add_argument("--install-harness", nargs="?", const="", default=None, metavar="CMD",
                    help="install the harness from its source directory before evaluation; "
                         "defaults to the manifest installation.command or a detected "
                         "install.sh/install.py/package.json/pyproject.toml entry")
    ap.add_argument("--validation-cmd",
                    help="real candidate validation command used during adapter discovery")
    ap.add_argument("--workspace-dir",
                    help="root directory for isolated component candidates")
    ap.add_argument("--trace-dir",
                    help="directory for JSONL traces and evidence")
    ap.add_argument("--runtime-root",
                    help="external runtime/harness checkout exposed to the adapter")
    ap.add_argument("--debug-cmd",
                    help="failure debugger command; placeholders: {workspace}, {trace_dir}, {task}, {stage}, {metrics_path}")
    ap.add_argument("--no-write-back", action="store_true",
                    help="keep accepted component candidates staged without modifying the source")
    ap.add_argument("--token-target", help="e.g. '30%%'")
    ap.add_argument("--time-target", help="e.g. '20%%'")
    ap.add_argument("--accuracy-constraint", default="不变")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--baseline-only", action="store_true",
                    help="evaluate the current harness once without editing it")
    ap.add_argument("--artifact-workspace",
                    help="Git workspace artifact target (default: <AHE repo>/artifacts/autoharness)")
    ap.add_argument("--artifact-include-candidates", action="store_true",
                    help="copy candidate workspace files into the artifact bundle")
    ap.add_argument("--artifact-commit", action="store_true",
                    help="commit generated artifact files (does not push)")
    ap.add_argument("--artifact-push", action="store_true",
                    help="push the generated artifact commit; requires --artifact-commit")
    ap.add_argument("--artifact-remote", default="origin")
    ap.add_argument("--artifact-branch")
    ap.add_argument("--artifact-message")
    ap.add_argument("--api-url", default=os.environ.get("LLM_BASE_URL", ""))
    ap.add_argument("--api-key", default=os.environ.get("LLM_API_KEY", ""))
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL", ""))
    ap.add_argument("--evolve-model", default=os.environ.get("EVOLVE_LLM_MODEL", "GLM-5.3"))
    args = ap.parse_args()

    if args.export_best:
        export_best(Path(args.export_best).expanduser().resolve(), args.target,
                    register=not args.no_register, suffix=args.as_suffix,
                    no_rename=args.no_rename)
        return

    if args.export_patch_report:
        from patch_report import export_patch_report
        export_patch_report(Path(args.export_patch_report).expanduser().resolve(),
                            args.target)
        return

    if args.preflight:
        preflight(args.harness, args.manifest)
        return

    if args.install_harness is not None:
        install_harness(args.harness, args.install_harness or None, args.manifest)
        return

    if not args.harness:
        ap.error("harness path is required unless --export-best/--export-patch-report is used")

    runtime_root = args.runtime_root

    if args.eval_task:
        # evaluator/debugger 子进程继承 os.environ（run_command 会在应用
        # AHE_* 覆盖前先复制它），因此在这里导出即完成全部接线。
        # eval 任务 checkout 对所有运行模式保持只读。
        os.environ["AHE_EVAL_TASK_ROOT"] = str(Path(args.eval_task).expanduser().resolve())
        if args.eval_project:
            os.environ["AHE_EVAL_TASK_PROJECT"] = args.eval_project
        if not args.task_root:
            args.task_root = args.eval_task
        if not args.task:
            args.task = (f"优化 harness 完成 {args.eval_project} 评测任务"
                         if args.eval_project else
                         f"优化 harness 完成 {args.eval_task} 评测任务")

    if args.discover_adapter:
        if not args.task:
            ap.error("--task is required with --discover-adapter")
        from harness_optimizer.surface import discover_surface

        discovery = discover_surface(
            args.harness,
            task=args.task,
            task_root=args.task_root,
            eval_command=args.eval_cmd,
            validation_command=args.validation_cmd,
            adapter="command" if args.adapter == "auto" else args.adapter,
            include_medium=args.include_medium,
        )
        output_path = None
        if args.output_manifest:
            output_path = Path(args.output_manifest).expanduser().resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(discovery.manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        print(json.dumps({
            "manifest_path": str(output_path) if output_path else None,
            "manifest": discovery.manifest,
            "report": discovery.report(),
        }, ensure_ascii=False, indent=2))
        return

    if args.generate_evaluator is not None:
        if not args.task:
            ap.error("--task is required with --generate-evaluator "
                     "(or derive one via --eval-task)")
        if args.generate_evaluator:
            target = args.generate_evaluator
        elif args.manifest:
            target = str(Path(args.manifest).expanduser().resolve().parent / "ahe_evaluator.py")
        else:
            target = "ahe_evaluator.py"
        api = load_api_config(args.api_url, args.api_key, args.model, args.evolve_model)
        generate_evaluator(
            harness=args.harness,
            manifest_path=args.manifest,
            task=args.task,
            eval_task=args.eval_task,
            eval_project=args.eval_project,
            api=api,
            target=target,
        )
        return

    if not args.task:
        ap.error("--task is required unless --discover-adapter or --generate-evaluator is used")

    test_cases = []
    for raw_case in args.test or []:
        question, separator, expected = raw_case.partition("|")
        if not separator or not question.strip() or not expected.strip():
            ap.error("--test entries must use the form 'input|expected'")
        test_cases.append((question.strip(), expected.strip()))

    targets = {}
    if args.token_target: targets["token"] = args.token_target
    if args.time_target: targets["time"] = args.time_target
    if args.accuracy_constraint: targets["accuracy"] = args.accuracy_constraint
    if args.artifact_push and not args.artifact_commit:
        ap.error("--artifact-push requires --artifact-commit")
    artifact_workspace = args.artifact_workspace or str(
        PLUGIN_ROOT.parents[1] / "artifacts" / "autoharness"
    )

    if args.manifest:
        from ahe_engine.component import ComponentModeError, run_component_mode

        api = load_api_config(args.api_url, args.api_key, args.model, args.evolve_model)
        try:
            report = run_component_mode(
                harness_path_str=args.harness,
                manifest_path=args.manifest,
                task_prompt=args.task,
                targets=targets,
                rounds=args.rounds,
                eval_command=args.eval_cmd,
                holdout_eval_command=args.holdout_eval_cmd,
                baseline_only=args.baseline_only,
                api=api,
                call_llm=call_llm,
                adapter_name=None if args.adapter == "auto" else args.adapter,
                workspace_dir=args.workspace_dir,
                trace_dir=args.trace_dir,
                runtime_root=runtime_root,
                write_back=not args.no_write_back,
                debug_command=args.debug_cmd,
            )
        except (ComponentModeError, FileNotFoundError, ValueError) as exc:
            print(f"ERROR: {exc}")
            return
        if report and report.get("trace_dir"):
            # report 是本次运行刚生成的可信产物；显式放行其 trace 与
            # candidates 根，bridge 不再依赖 report 字段隐式信任。
            trusted_roots = [
                value for value in (report.get("trace_dir"), report.get("workspace_dir")) if value
            ] or None
            artifact_result = _publish_report_artifacts(
                Path(report["trace_dir"]) / "ahe-component-report.json",
                artifact_workspace,
                include_candidates=args.artifact_include_candidates,
                commit=args.artifact_commit,
                push=args.artifact_push,
                remote=args.artifact_remote,
                branch=args.artifact_branch,
                message=args.artifact_message,
                allowed_source_roots=trusted_roots,
            )
            if artifact_result:
                report["artifact_bundle"] = artifact_result
        baseline = report.get("baseline") if isinstance(report, dict) else None
        if (isinstance(baseline, dict) and not baseline.get("runner_ok", True)
                and _looks_like_missing_dependency(json.dumps(baseline, default=str))):
            print()
            print("提示: baseline 失败疑似缺少依赖。可先做只读预检并从源目录安装:")
            base_cmd = f"python3 ahe.py {args.harness}"
            if args.manifest:
                base_cmd += f" --manifest {args.manifest}"
            print(f"  {base_cmd} --preflight")
            print(f"  {base_cmd} --install-harness")
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return

    run(args.harness, args.task, targets, args.rounds, args.eval_cmd,
        baseline_only=args.baseline_only, test_cases=test_cases or None,
        api_url=args.api_url, api_key=args.api_key, model=args.model,
        evolve_model=args.evolve_model, artifact_workspace=artifact_workspace,
        artifact_include_candidates=args.artifact_include_candidates,
        artifact_commit=args.artifact_commit, artifact_push=args.artifact_push,
        artifact_remote=args.artifact_remote, artifact_branch=args.artifact_branch,
        artifact_message=args.artifact_message)


if __name__ == "__main__":
    main()
