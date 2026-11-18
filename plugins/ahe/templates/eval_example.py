#!/usr/bin/env python3
"""AHE 评估器最小模板。

AHE 对每个候选运行一次本命令，并从 stdout 最后一行读取单个
JSON 对象。输入通过环境变量传入；命令行不传任何参数，因此
同一条命令适用于每个候选。

必需输出字段：
  accuracy  —— 主指标（generic adapter），越高越好
建议输出：
  tokens    —— harness 消耗 API 时的 LLM token 用量
  time_s    —— wall-clock 秒数
"""
import json
import os
import subprocess
import sys
import time


def parse_accuracy(stdout: str) -> float:
    """替换为你的真实评分逻辑（正则、pytest 退出码等）。"""
    return 1.0 if "ALL PASS" in stdout else 0.0


def main() -> int:
    candidate = os.environ["AHE_HARNESS_ROOT"]
    task = os.environ.get("AHE_TASK", "")
    stage = os.environ.get("AHE_EVAL_STAGE", "dev")
    trace_dir = os.environ.get("AHE_TRACE_DIR", "")
    if trace_dir:
        os.makedirs(trace_dir, exist_ok=True)
        with open(os.path.join(trace_dir, f"{stage}-evaluator.log"), "a", encoding="utf-8") as fh:
            fh.write(f"task={task} candidate={candidate}\n")

    t0 = time.time()
    result = subprocess.run(
        [sys.executable, "run_benchmark.py"],
        cwd=candidate,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    metrics = {
        "accuracy": parse_accuracy(result.stdout),
        "tokens": 0,
        "time_s": round(time.time() - t0, 3),
        "runner_ok": result.returncode == 0,
    }
    print(json.dumps(metrics))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
