"""基于 LLM 的评估器生成。

根据任务上下文生成评估器脚本及其 AHE-OBJECTIVE，并将 objective
写入 manifest，使 keep 门禁跟随评估器实际测量的指标。
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from .api_config import load_api_config
from .fsutil import _read_head
from .llm_client import call_llm

# ---------------------------------------------------------------------------
# 评估器生成
# ---------------------------------------------------------------------------

_OBJECTIVE_HEADER_RE = re.compile(r"^\s*#\s*AHE-OBJECTIVE:\s*(\{.*\})\s*$", re.MULTILINE)


def _strip_code_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        newline = value.find("\n")
        if newline != -1:
            value = value[newline + 1:]
        stripped = value.rstrip()
        if stripped.endswith("```"):
            value = stripped[:stripped.rfind("```")]
    return value.strip()


def _collect_dir_listing(root: Path, *, max_entries: int = 40, max_depth: int = 2) -> str:
    lines: list[str] = []
    try:
        for base, dirs, files in os.walk(root):
            depth = len(Path(base).relative_to(root).parts)
            if depth >= max_depth:
                dirs[:] = []
            for name in sorted(files):
                lines.append(str((Path(base) / name).relative_to(root)))
                if len(lines) >= max_entries:
                    lines.append("...")
                    return "\n".join(lines)
    except OSError:
        pass
    return "\n".join(lines)



def _generation_context(harness: Path, manifest_payload: dict,
                         eval_task: str | None, eval_project: str | None,
                         task: str) -> list[str]:
    components = manifest_payload.get("components") or []
    component_lines = [
        f"- {c.get('id')}: kind={c.get('kind')} mutable={bool(c.get('mutable'))} path={c.get('path')}"
        for c in components
    ] or ["(no manifest; the harness is evaluated as a whole)"]
    context = [
        f"Task: {task}",
        f"Harness root: {harness}",
        "Harness top-level files:",
        _collect_dir_listing(harness, max_entries=30, max_depth=1),
        "Optimization surface (components the evolve model may modify):",
        *component_lines,
    ]
    if eval_task:
        eval_root = Path(eval_task).expanduser().resolve()
        context.append(f"Eval task root (READ-ONLY): {eval_root}")
        context.append("Eval task files:")
        context.append(_collect_dir_listing(eval_root, max_entries=60, max_depth=2))
        for readme_name in ("README.md", "README_zh.md", "readme.md"):
            readme = eval_root / readme_name
            if readme.is_file():
                context.append(f"Eval task {readme_name} (excerpt):")
                context.append(_read_head(readme))
                break
        if eval_project:
            project = eval_root / "projects" / eval_project
            if project.is_dir():
                context.append(f"Eval project files ({project}):")
                context.append(_collect_dir_listing(project, max_entries=40, max_depth=1))
    return context


def generate_evaluator(*, harness: str, manifest_path: str | None, task: str,
                       eval_task: str | None = None, eval_project: str | None = None,
                       api: dict | None = None, target: str,
                       call_llm_fn=None) -> Path | None:
    """根据任务上下文生成评估器脚本及其 objective。

    评估器是 keep 门禁的唯一裁判，其声明的 objective 头会写入
    manifest，使门禁跟随评估器实际测量的指标。
    """
    api = api or load_api_config()
    if not api.get("url") or not api.get("key"):
        print("ERROR: evaluator generation requires an LLM API configuration")
        return None
    call = call_llm_fn or call_llm

    harness_root = Path(harness).expanduser().resolve()
    manifest_payload: dict = {}
    if manifest_path:
        manifest_file = Path(manifest_path).expanduser().resolve()
        try:
            manifest_payload = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            print(f"ERROR: cannot read manifest {manifest_file}: {exc}")
            return None

    system = (
        "You write AHE evaluators. Output exactly one complete Python 3 file, "
        "no prose before or after, no code fences. The evaluator is the sole "
        "judge behind keep/revert decisions, so it must measure real harness "
        "behavior on the stated task."
    )
    user = "\n".join([
        "Write an AHE evaluator for the optimization task below.",
        "",
        *_generation_context(harness_root, manifest_payload, eval_task, eval_project, task),
        "",
        "Required file contract:",
        "1. First line must be a machine-readable objective header comment:",
        '   # AHE-OBJECTIVE: {"metric": "<primary metric name>", "direction": "maximize|minimize", "description": "<one line>"}',
        "   The metric name is what your evaluator prints and what the keep gate compares.",
        "2. Read the environment: AHE_HARNESS_ROOT (the candidate harness directory to evaluate),",
        "   AHE_TASK, AHE_TRACE_DIR (write artifacts here), AHE_EVAL_STAGE ('dev'/'holdout'),",
        "   AHE_COMPONENT_MANIFEST" + (", AHE_EVAL_TASK_ROOT and AHE_EVAL_TASK_PROJECT" if eval_task else "") + ".",
        "3. Actually run or drive the harness (or its runner) on the task material and measure real",
        "   behavior. Prefer the eval task's own runner/scorer when one exists in the listing above.",
        "4. Print exactly one JSON object on the last stdout line, containing at least the objective",
        "   metric; include tokens and time_s when measurable. Non-zero exit only on evaluator failure.",
        "5. The eval task root, projects, runners, scorers and golden data are READ-ONLY.",
        "6. No stubs: an API availability ping, a sleep, or a constant score is not an evaluator.",
        "7. Standard library only unless the task forces otherwise; keep the file self-contained.",
        "",
        "Output the evaluator file now.",
    ])

    text, _tokens, _elapsed = call(
        api["url"], api["key"], api["evolve_model"],
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=32768,
    )
    source = _strip_code_fence(text)

    problems: list[str] = []
    objective: dict = {}
    header = _OBJECTIVE_HEADER_RE.search(source)
    if not header:
        problems.append("missing '# AHE-OBJECTIVE: {\"metric\": ..., \"direction\": ...}' header comment")
    else:
        try:
            objective = json.loads(header.group(1))
        except ValueError as exc:
            problems.append(f"objective header is not valid JSON: {exc}")
        if not isinstance(objective.get("metric"), str) or not objective["metric"].strip():
            problems.append("objective header must declare a non-empty metric name")
        if str(objective.get("direction", "maximize")).lower() not in ("maximize", "minimize"):
            problems.append("objective direction must be 'maximize' or 'minimize'")
    try:
        compile(source, "ahe_evaluator.py", "exec")
    except SyntaxError as exc:
        problems.append(f"does not compile: {exc}")
    if "AHE_HARNESS_ROOT" not in source:
        problems.append("must read AHE_HARNESS_ROOT from the environment")
    if "json.dumps" not in source:
        problems.append("must print a JSON metrics object (json.dumps)")

    target_path = Path(target).expanduser().resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if problems:
        rejected = target_path.with_suffix(target_path.suffix + ".rejected")
        rejected.write_text(source + "\n", encoding="utf-8")
        print("ERROR: generated evaluator rejected:")
        for problem in problems:
            print(f"  - {problem}")
        print(f"Raw model output kept for review: {rejected}")
        return None

    target_path.write_text(source + "\n", encoding="utf-8")

    manifest_updated = None
    if manifest_path and objective:
        manifest_file = Path(manifest_path).expanduser().resolve()
        backup = manifest_file.with_suffix(manifest_file.suffix + ".ahe-backup")
        shutil.copy2(manifest_file, backup)
        manifest_payload["objective"] = {
            "metric": objective["metric"].strip(),
            "direction": str(objective.get("direction", "maximize")).lower(),
            "description": str(objective.get("description", "")),
        }
        manifest_file.write_text(
            json.dumps(manifest_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        manifest_updated = manifest_file

    print(f"Evaluator written: {target_path}")
    print(f"Objective: {json.dumps(objective, ensure_ascii=False)}")
    if manifest_updated:
        print(f"Manifest objective updated (backup at {backup}): {manifest_updated}")
    print()
    print("Review the evaluator, then run the optimization loop with:")
    parts = [f"python3 ahe.py {harness}"]
    if manifest_path:
        parts.append(f"--manifest {manifest_path}")
    parts.append(f"--task {task!r}")
    if eval_task:
        parts.append(f"--eval-task {eval_task}")
    if eval_project:
        parts.append(f"--eval-project {eval_project}")
    parts.append(f"--eval-cmd 'python3 {target_path}'")
    print("  " + " \\\n    ".join(parts))
    return target_path
