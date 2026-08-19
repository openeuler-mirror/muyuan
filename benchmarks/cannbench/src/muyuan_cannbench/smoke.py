from __future__ import annotations

import argparse
import json
import py_compile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from auto_pipeline.converter.registry import create_converter
from auto_pipeline.core import BenchmarkPipeline, CannBenchClient, CannBenchEvalResult
from auto_pipeline.generator.registry import create_generator

from .paths import CANN_BENCH_ROOT, MUYUAN_ROOT
from .registration import register_integrations


class NonNpuCannBenchClient(CannBenchClient):
    """Use real case loading and pipeline flow, replacing only kernel_eval."""

    def eval_submission(
        self,
        *,
        bench_name: str,
        source_dir: Path,
        task_selector: str,
        reports_dir: Path,
        device_id: int | None = None,
        extra_args: Iterable[str] | None = None,
    ) -> CannBenchEvalResult:
        del device_id, extra_args
        reports_dir = Path(reports_dir).resolve()
        reports_dir.mkdir(parents=True, exist_ok=True)
        errors: list[str] = []
        for path in sorted((Path(source_dir) / "cann_bench").glob("*.py")):
            try:
                py_compile.compile(str(path), doraise=True)
            except py_compile.PyCompileError as exc:
                errors.append(str(exc))
        payload = {
            "mode": "non_npu_control_plane_smoke",
            "benchmark": bench_name,
            "task_selector": task_selector,
            "source_dir": str(Path(source_dir).resolve()),
            "npu_executed": False,
            "kernel_eval_executed": False,
            "python_sources_compile": not errors,
            "errors": errors,
        }
        report_path = reports_dir / "non_npu_smoke.json"
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return CannBenchEvalResult(
            returncode=0 if not errors else 1,
            command=["non-npu-static-eval"],
            reports_dir=reports_dir,
            stdout=json.dumps(payload, sort_keys=True),
            stderr="\n".join(errors),
            report_files=[report_path],
        )


def install_non_npu_eval_patch() -> None:
    """Patch only the final eval boundary for an explicitly opted-in CLI smoke."""

    CannBenchClient.eval_submission = NonNpuCannBenchClient.eval_submission


def run_non_npu_smoke(*, output_root: Path, selector: str = "tasks/level1/mish") -> dict:
    register_integrations()
    output_root = Path(output_root).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=False)
    config_path = MUYUAN_ROOT / "benchmarks" / "cannbench" / "configs" / "goal-plus-triton-ascend.smoke.yaml"
    generator = create_generator(
        "goal-plus",
        {
            "config_path": str(config_path),
            "codegen_target": "triton_ascend",
            "device_id": 0,
            "verify_timeout": 120,
        },
    )
    converter = create_converter("goal-plus", "cann", {"timeout_sec": 120})
    pipeline = BenchmarkPipeline(
        bench_name="cann",
        client=NonNpuCannBenchClient(CANN_BENCH_ROOT),
    )
    stages: list[dict[str, str]] = []

    def status_callback(**payload: str) -> None:
        stages.append(dict(payload))

    result = pipeline.run_case(
        selector=selector,
        generator=generator,
        converter=converter,
        workdir=output_root / "generate",
        submission_dir=output_root / "submission",
        reports_dir=output_root / "reports",
        timeout_sec=120,
        status_callback=status_callback,
    )
    summary = {
        "status": result.status,
        "operator": result.case.operator,
        "selector": selector,
        "pipeline_stages": stages,
        "submission_dir": str(result.submission.source_dir) if result.submission else None,
        "eval_ok": bool(result.eval_result and result.eval_result.ok),
        "eval_report_files": [str(path) for path in result.eval_result.report_files] if result.eval_result else [],
        "goal_plus": dict(result.generated_artifact.metadata) if result.generated_artifact else {},
        "npu_executed": False,
    }
    summary_path = output_root / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if result.status != "success" or not summary["eval_ok"]:
        raise RuntimeError(f"non-NPU pipeline smoke failed; see {summary_path}")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output")
    parser.add_argument("--selector", default="tasks/level1/mish")
    args = parser.parse_args(argv)
    if args.output:
        output = Path(args.output)
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = MUYUAN_ROOT / "benchmarks" / "cannbench" / "runs" / f"non-npu-{stamp}"
    summary = run_non_npu_smoke(output_root=output, selector=args.selector)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
