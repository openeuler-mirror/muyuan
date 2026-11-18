"""单 prompt 文件 harness 的优化循环（模式 A）。

读取 harness（单个文件或文本文件目录），通过 eval 命令或 LLM
测试题评测，用 LLM 进化，仅保留改善（准确率不降、token/时间改善）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .api_config import load_api_config
from .artifacts import _publish_report_artifacts
from .llm_client import call_llm

# ---------------------------------------------------------------------------
# harness 读写
# ---------------------------------------------------------------------------

TEXT_EXTS = {".txt", ".md", ".yaml", ".yml", ".toml", ".json", ".py", ".ts", ".js", ".sh", ".cfg", ".ini"}

def read_harness(path: Path) -> dict[str, str]:
    if path.is_file():
        return {path.name: path.read_text(encoding="utf-8")}
    files = {}
    for f in sorted(path.rglob("*")):
        if f.is_file() and f.suffix.lower() in TEXT_EXTS:
            rel = str(f.relative_to(path))
            try:
                files[rel] = f.read_text(encoding="utf-8")
            except Exception:
                pass
    return files

def write_harness(path: Path, files: dict[str, str]):
    if path.is_file():
        path.write_text(list(files.values())[0], encoding="utf-8")
    else:
        for rel, content in files.items():
            f = path / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content, encoding="utf-8")

def harness_to_context(files: dict[str, str], max_chars=50000) -> str:
    parts = []
    total = 0
    for rel, content in files.items():
        display = content[:max(1000, (max_chars - total) // max(1, len(files)))]
        parts.append(f"--- {rel} ---\n{display}")
        total += len(display)
        if total > max_chars:
            break
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 评测器：在任务上运行 harness
# ---------------------------------------------------------------------------

def evaluate_harness(api, files: dict[str, str], task_prompt: str,
                     eval_command: str | None = None, test_cases=None):
    """评测 harness。两种模式：
    1. 提供 eval_command —— 通过 bash 运行并解析 JSON 输出
    2. 未提供 —— 将 harness 作为 system prompt、任务作为 user message 发送
    """
    if eval_command:
        # 运行自定义评测
        try:
            result = subprocess.run(
                ["bash", "-c", eval_command],
                capture_output=True, text=True, timeout=7200,
                env={**os.environ, "AHE_HARNESS": harness_to_context(files)})
            if result.returncode != 0:
                raise RuntimeError(
                    f"eval command exited with {result.returncode}: "
                    f"{result.stderr[-500:]}")
            data = json.loads(result.stdout.strip().splitlines()[-1])
            return {"accuracy": data.get("accuracy", 0),
                        "tokens": data.get("tokens", 0),
                        "time_s": data.get("time_s", 0),
                        "accuracy_verified": "accuracy" in data,
                        "accuracy_mode": "custom_eval"}
        except Exception as e:
            return {"accuracy": 0, "tokens": 0, "time_s": 0,
                    "accuracy_verified": False, "accuracy_mode": "custom_eval",
                    "error": str(e)}

    # 默认：harness 作 system prompt，任务作 user message
    context = harness_to_context(files)
    if test_cases:
        passed = 0
        total_tokens = 0
        total_time = 0.0
        case_results = []
        errors = []
        for question, expected in test_cases:
            messages = [
                {"role": "system", "content": context[:100000]},
                {"role": "user", "content": question},
            ]
            try:
                text, tokens, elapsed = call_llm(
                    api["url"], api["key"], api["model"], messages)
                matched = expected in text
                passed += int(matched)
                total_tokens += tokens
                total_time += elapsed
                case_results.append({"question": question, "matched": matched,
                                     "response_preview": text[:200]})
            except Exception as e:
                errors.append({"question": question, "error": str(e)})
                case_results.append({"question": question, "matched": False})
        result = {
            "accuracy": passed / len(test_cases),
            "tokens": total_tokens,
            "time_s": round(total_time, 2),
            "accuracy_verified": True,
            "accuracy_mode": "substring",
            "test_cases": case_results,
        }
        if errors:
            result["errors"] = errors
        return result

    # 没有测试题就没有答案键，因此保留历史上的可用性探测，
    # 但将 accuracy 标记为未验证。
    messages = [
        {"role": "system", "content": context[:100000]},
        {"role": "user", "content": task_prompt},
    ]
    try:
        text, tokens, elapsed = call_llm(api["url"], api["key"], api["model"], messages)
        return {"accuracy": 1.0, "tokens": tokens, "time_s": round(elapsed, 2),
                "accuracy_verified": False, "accuracy_mode": "availability",
                "response_preview": text[:200]}
    except Exception as e:
        return {"accuracy": 0, "tokens": 0, "time_s": 0,
                "accuracy_verified": False, "accuracy_mode": "availability",
                "error": str(e)}


# ---------------------------------------------------------------------------
# 进化：LLM 改进 harness
# ---------------------------------------------------------------------------

EVOLVE_SYSTEM = "You are an expert harness optimization agent. Output only the modified files."

def evolve_harness(api, files: dict[str, str], task_prompt: str,
                   baseline: dict, targets: dict, last_delta: str | None):
    harness_str = harness_to_context(files, max_chars=30000)

    target_str = ""
    if targets.get("token"):
        target_str += f"Token: 降低{targets['token']}\n"
    if targets.get("time"):
        target_str += f"时间: 降低{targets['time']}\n"
    if targets.get("accuracy"):
        target_str += f"准确率: {targets['accuracy']}\n"

    user_prompt = f"""优化以下 harness。

任务: {task_prompt}

当前 harness 文件:
{harness_str}

基线指标: accuracy={baseline.get('accuracy', 'N/A')}, tokens={baseline.get('tokens', 'N/A')}, time={baseline.get('time_s', 'N/A')}s

优化目标:
{target_str or 'token降低、时间降低、准确率不降'}

上一轮效果: {last_delta or '首轮'}

请修改 harness 文件以达成目标。输出格式:
=== FILE: 文件名 ===
修改后的完整文件内容
=== END ===

如果只改一个文件且原始只有一个文件，直接输出内容即可。"""

    try:
        text, _, _ = call_llm(api["url"], api["key"], api["evolve_model"],
                               [{"role": "system", "content": EVOLVE_SYSTEM},
                                {"role": "user", "content": user_prompt}],
                               max_tokens=16384)
        return parse_files(text, files)
    except Exception:
        return files


def parse_files(text: str, original: dict[str, str]) -> dict[str, str]:
    pattern = r"=== FILE:\s*(.+?)\s*===\s*\n(.*?)(?:\n=== END ===|\Z)"
    matches = re.findall(pattern, text, re.DOTALL)
    if matches:
        new_files = {}
        for fname, content in matches:
            fname = fname.strip()
            if fname in original:
                new_files[fname] = content.strip()
        if new_files:
            return new_files
    # 单文件回退
    clean = text.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```\w*\n?", "", clean)
        clean = re.sub(r"\n?```$", "", clean)
    if len(original) == 1:
        return {list(original.keys())[0]: clean}
    return original


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

def run(harness_path_str, task_prompt, targets, rounds, eval_command=None,
        baseline_only=False, test_cases=None, api_url=None, api_key=None,
        model=None, evolve_model=None, artifact_workspace=None,
        artifact_include_candidates=False, artifact_commit=False,
        artifact_push=False, artifact_remote="origin", artifact_branch=None,
        artifact_message=None):
    harness_path = Path(harness_path_str)
    if not harness_path.exists():
        print(f"ERROR: {harness_path} not found")
        return None

    api = load_api_config(api_url, api_key, model, evolve_model)
    if (not api["url"] or not api["key"]) and not (baseline_only and eval_command):
        print("ERROR: no API config found. Options: set LLM_BASE_URL/LLM_API_KEY/"
              "LLM_MODEL env vars; or point AHE_ENV_FILE at an env file with "
              "LLM_*/CSI_* keys; or launch from a Pi session whose provider "
              "config (PI_PROVIDER + models.json) ahe.py can inherit.")
        return None

    original_files = read_harness(harness_path)
    is_dir = harness_path.is_dir()

    print(f"\n{'='*60}")
    print(f"AHE: {rounds} rounds")
    print(f"Harness: {harness_path} ({len(original_files)} files)")
    print(f"Task: {task_prompt[:100]}...")
    if targets:
        print(f"Targets: {targets}")
    print(f"{'='*60}\n")

    # 仅优化运行才备份。baseline 探测必须保持 harness 不被改动，
    # 以便在首次编辑前安全使用。
    backup = harness_path.parent / f"{harness_path.name}.ahe-backup"
    if not baseline_only:
        if backup.exists():
            shutil.rmtree(backup) if backup.is_dir() else backup.unlink()
        if is_dir:
            shutil.copytree(harness_path, backup)
        else:
            shutil.copy2(harness_path, backup)

    # 基线
    print("[baseline] ", end="", flush=True)
    base = evaluate_harness(api, original_files, task_prompt, eval_command,
                            test_cases)
    print(f"acc={base['accuracy']}  tokens={base['tokens']}  time={base['time_s']}s")

    history = [{"round": 0, "metrics": base, "action": "baseline"}]

    if baseline_only:
        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "harness": str(harness_path),
            "task": task_prompt,
            "targets": targets,
            "rounds": 0,
            "history": history,
            "baseline": base,
            "final": base,
            "improvement": {
                "accuracy_delta": 0,
                "tokens_delta": 0,
                "time_delta_s": 0,
            },
        }
        report_path = harness_path.parent / "ahe-report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
        artifact_result = _publish_report_artifacts(
            report_path,
            artifact_workspace,
            include_candidates=artifact_include_candidates,
            commit=artifact_commit,
            push=artifact_push,
            remote=artifact_remote,
            branch=artifact_branch,
            message=artifact_message,
        )
        if artifact_result:
            report["artifact_bundle"] = artifact_result
        print(f"Baseline report: {report_path}")
        return report

    best = {"files": original_files, "metrics": base}
    last_delta = None

    for rd in range(1, rounds + 1):
        print(f"\n[round {rd}] evolving... ", end="", flush=True)
        new_files = evolve_harness(api, best["files"], task_prompt,
                                   best["metrics"], targets, last_delta)
        print(f"evaluating... ", end="", flush=True)
        new_metrics = evaluate_harness(api, new_files, task_prompt, eval_command,
                                       test_cases)

        acc_ok = new_metrics["accuracy"] >= best["metrics"]["accuracy"]
        token_ok = new_metrics["tokens"] <= best["metrics"]["tokens"]
        time_ok = new_metrics["time_s"] <= best["metrics"]["time_s"]
        improved = acc_ok and (token_ok or time_ok) and (
            new_metrics["tokens"] < best["metrics"]["tokens"] or
            new_metrics["time_s"] < best["metrics"]["time_s"])

        if improved:
            delta = (f"acc {best['metrics']['accuracy']}→{new_metrics['accuracy']}, "
                     f"tokens {best['metrics']['tokens']}→{new_metrics['tokens']}, "
                     f"time {best['metrics']['time_s']}→{new_metrics['time_s']}s")
            print(f"KEEP  {delta}")
            best = {"files": new_files, "metrics": new_metrics}
            last_delta = f"improved: {delta}"
            history.append({"round": rd, "metrics": new_metrics, "action": "keep"})
        else:
            delta = (f"acc {best['metrics']['accuracy']}→{new_metrics['accuracy']}, "
                     f"tokens {best['metrics']['tokens']}→{new_metrics['tokens']}")
            print(f"REVERT  {delta}")
            last_delta = f"reverted: {delta}"
            history.append({"round": rd, "metrics": new_metrics, "action": "revert"})

    # 写回
    write_harness(harness_path, best["files"])

    # 报告
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "harness": str(harness_path),
        "task": task_prompt,
        "targets": targets,
        "rounds": rounds,
        "history": history,
        "baseline": base,
        "final": best["metrics"],
        "improvement": {
            "accuracy_delta": best["metrics"]["accuracy"] - base["accuracy"],
            "tokens_delta": base["tokens"] - best["metrics"]["tokens"],
            "time_delta_s": base["time_s"] - best["metrics"]["time_s"],
        },
    }
    report_path = harness_path.parent / "ahe-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    artifact_result = _publish_report_artifacts(
        report_path,
        artifact_workspace,
        include_candidates=artifact_include_candidates,
        commit=artifact_commit,
        push=artifact_push,
        remote=artifact_remote,
        branch=artifact_branch,
        message=artifact_message,
    )
    if artifact_result:
        report["artifact_bundle"] = artifact_result

    print(f"\n{'='*60}")
    print(f"Final: acc={best['metrics']['accuracy']}, "
          f"tokens={best['metrics']['tokens']}, time={best['metrics']['time_s']}s")
    print(f"Delta: tokens {-report['improvement']['tokens_delta']:+d}, "
          f"time {-report['improvement']['time_delta_s']:+.1f}s")
    print(f"Written back: {harness_path}")
    print(f"Backup: {backup}")
    print(f"Report: {report_path}")
    print(f"{'='*60}\n")

    return report

