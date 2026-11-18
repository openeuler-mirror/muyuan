"""发现保守、可审阅的 AHE harness 可编辑面。

发现步骤刻意不把 checkout 里的每个文件都推断为 harness 的一部分。
它按 AHE 论文（arXiv:2604.25850，NexAU substrate）的七类可编辑
组件（system prompt、tool description、tool implementation、
middleware、skill、sub-agent 配置、long-term memory）识别面向
agent 的文件，并把任务/evaluator/工作负载路径排除在生成的
manifest 之外。产出是 adapter 合同草稿；调用方应在运行多轮优化前
审阅报告。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


IGNORED_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache",
    "build", "dist", "coverage", ".ahe-candidates", ".ahe-traces",
    "artifacts", ".idea", ".vscode",
})

TASK_DIRS = frozenset({
    "task", "tasks", "workload", "workloads", "kernel", "kernels", "model",
    "models", "benchmark", "benchmarks", "eval", "evaluator", "evaluators",
    "golden", "tests", "test", "fixtures", "fixture", "data", "dataset",
    "datasets",
})

HARNESS_DIRS = frozenset({
    ".pi", ".codex", ".claude", ".agents", ".cursor", "agents", "agent",
    "prompts", "prompt", "instructions", "skills", "skill", "tools", "tool",
    "tool_descriptions", "middleware", "middlewares", "memory", "memories",
    "extensions", "plugins", "hooks", "policies", "policy", "workflows",
    "workflow", "config", "configs", "assets", "src", "lib", "runtime",
})

ROLE_TOKENS = (
    "prompt", "instruction", "instructions", "description", "persona", "tool",
    "skill", "middleware", "memory", "handoff", "ledger", "agent", "policy",
    "workflow", "adapter", "strategy", "config", "extension", "plugin", "hook",
)

# AHE 论文（arXiv:2604.25850 第 3.1 节，NexAU substrate）的
# 七类可编辑组件。发现步骤经过调校，使每类组件都以
# 高置信度可变组件进入生成的 manifest。
AHE_COMPONENT_CLASSES = (
    ("system_prompt", "system prompt text the coding agent runs under"),
    ("tool_description", "declarative tool/API descriptions shown to the model"),
    ("tool_implementation", "executable tool and runtime implementation code"),
    ("middleware", "context, execution, and recovery middleware (e.g. extensions)"),
    ("skill", "skill/playbook files the agent loads on demand"),
    ("sub_agent_config", "sub-agent definitions and per-agent configuration"),
    ("long_term_memory", "persistent memory, ledger, and handoff mechanisms"),
)

SOURCE_SUFFIXES = frozenset({
    ".c", ".cc", ".cpp", ".go", ".java", ".js", ".jsx", ".mjs", ".py",
    ".rs", ".sh", ".ts", ".tsx",
})
TEXT_SUFFIXES = frozenset({
    ".json", ".md", ".rst", ".toml", ".txt", ".yaml", ".yml",
})


@dataclass(frozen=True)
class SurfaceFile:
    path: str
    kind: str
    confidence: str
    reason: str


@dataclass(frozen=True)
class SurfaceDiscovery:
    manifest: dict[str, Any]
    selected: tuple[SurfaceFile, ...]
    review: tuple[SurfaceFile, ...]
    excluded: tuple[dict[str, str], ...]
    warnings: tuple[str, ...]

    def report(self) -> dict[str, Any]:
        return {
            "selected": [item.__dict__ for item in self.selected],
            "review": [item.__dict__ for item in self.review],
            "excluded": list(self.excluded),
            "warnings": list(self.warnings),
        }


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _path_tokens(command: str | Sequence[str] | None) -> Iterable[str]:
    if command is None:
        return ()
    if not isinstance(command, str):
        return (str(item) for item in command)
    try:
        return iter(shlex.split(command, posix=False))
    except ValueError:
        return iter(re.findall(r"[^\s]+", command))


def _command_paths(
    command: str | Sequence[str] | None,
    root: Path,
) -> tuple[Path, ...]:
    paths: list[Path] = []
    for raw in _path_tokens(command):
        token = str(raw).strip("'\";,()[]")
        if not token or "{" in token or "$" in token:
            continue
        candidate = Path(os.path.expanduser(token))
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.exists() and _under(resolved, root):
            paths.append(resolved)
    return tuple(dict.fromkeys(paths))


def _role_for(path: Path, root: Path) -> tuple[str, str, str] | None:
    relative = _relative(path, root)
    parts = tuple(part.lower() for part in Path(relative).parts)
    name = path.name.lower()
    stem = path.stem.lower()
    if name in {"manifest.json", "harness.manifest.json", ".ahe-manifest.json"}:
        return ("metadata", "excluded", "manifest is the adapter contract")
    if any(part in IGNORED_DIRS for part in parts):
        return ("generated", "excluded", "generated or dependency directory")
    if any(part in TASK_DIRS for part in parts):
        return ("task_or_evaluator", "excluded", "task/evaluator/workload path")
    if any(token in stem for token in ("eval", "evaluator", "golden", "benchmark", "fixture")):
        return ("task_or_evaluator", "excluded", "evaluator-like filename")
    if path.suffix.lower() not in SOURCE_SUFFIXES | TEXT_SUFFIXES:
        return ("binary_or_asset", "excluded", "non-text implementation surface")

    score = 0
    reasons: list[str] = []
    if any(part in HARNESS_DIRS for part in parts[:-1]):
        score += 3
        reasons.append("harness-facing directory")
    if any(token in stem for token in ROLE_TOKENS):
        score += 2
        reasons.append("harness-facing filename")
    if path.suffix.lower() in TEXT_SUFFIXES:
        score += 1
        reasons.append("configuration or instruction text")
    if stem in {"main", "entry", "agent", "harness", "plugin"}:
        score += 1
        reasons.append("likely harness entrypoint")
    if score >= 3:
        return ("harness_component", "high", "; ".join(reasons))
    if score >= 2:
        return ("harness_component", "medium", "; ".join(reasons))
    return ("unknown", "review", "not enough evidence to classify as harness code")


def _component_id(path: str, index: int) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", path).strip("-").lower()
    return f"auto-{value or 'component'}-{index}"


def discover_surface(
    harness_root: os.PathLike[str] | str,
    *,
    task: str = "",
    task_root: os.PathLike[str] | str | None = None,
    eval_command: str | Sequence[str] | None = None,
    validation_command: str | Sequence[str] | None = None,
    adapter: str = "command",
    include_medium: bool = False,
    max_files: int = 200,
) -> SurfaceDiscovery:
    """为 harness checkout 返回保守的 manifest 草稿。

    任务根与 evaluator 路径按只读证据处理。中等置信度文件仅报告
    供审阅，除非显式设置 ``include_medium``，否则不可变。
    """
    root = Path(harness_root).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"harness root is not a directory: {root}")
    resolved_task_root = Path(task_root).expanduser().resolve() if task_root else None
    blocked = list(_command_paths(eval_command, root))
    if resolved_task_root and _under(resolved_task_root, root):
        blocked.append(resolved_task_root)

    selected: list[SurfaceFile] = []
    review: list[SurfaceFile] = []
    excluded: list[dict[str, str]] = []
    warnings: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if any(_under(path, item) for item in blocked):
            excluded.append({"path": _relative(path, root), "reason": "task/evaluator path"})
            continue
        classified = _role_for(path, root)
        if classified is None:
            continue
        kind, confidence, reason = classified
        relative = _relative(path, root)
        item = SurfaceFile(relative, kind, confidence, reason)
        if confidence == "excluded":
            excluded.append({"path": relative, "reason": reason})
        elif confidence == "high" or (confidence == "medium" and include_medium):
            selected.append(item)
        else:
            review.append(item)

    if not selected:
        warnings.append("no high-confidence harness components were found; review the candidate list before editing")
    if len(selected) > max_files:
        warnings.append(f"{len(selected)} components exceed the {max_files}-file review budget")
    if not validation_command:
        warnings.append("validation command is missing; add a real syntax/unit validation command before optimization")
    if not eval_command:
        warnings.append("evaluator command is missing; the generated manifest cannot prove task improvement")

    components = [
        {
            "id": _component_id(item.path, index),
            "kind": item.kind,
            "path": item.path,
            "mutable": True,
        }
        for index, item in enumerate(selected, start=1)
    ]
    manifest: dict[str, Any] = {
        "name": f"{root.name}-auto-harness",
        "version": 1,
        "description": (
            "Reviewable harness surface generated from task/evaluator "
            "boundaries, covering the seven editable component types of the "
            "AHE paper (arXiv:2604.25850): system prompt, tool description, "
            "tool implementation, middleware, skill, sub-agent configuration, "
            "and long-term memory."
        ),
        "components": components,
        "adapter": {"name": adapter or "command"},
        "discovery": {
            "mode": "static-safe-draft",
            "task": task,
            "harness_root": str(root),
            "task_root": str(resolved_task_root) if resolved_task_root else None,
            "selected_count": len(selected),
            "review_count": len(review),
            "excluded_count": len(excluded),
            "component_classes": [
                {"class": name, "description": desc}
                for name, desc in AHE_COMPONENT_CLASSES
            ],
        },
    }
    if validation_command:
        manifest["validation"] = {"command": validation_command}
    if eval_command:
        manifest["evaluation"] = {"command": eval_command}
    return SurfaceDiscovery(
        manifest=manifest,
        selected=tuple(selected),
        review=tuple(review),
        excluded=tuple(excluded),
        warnings=tuple(warnings),
    )


def _main() -> int:
    parser = argparse.ArgumentParser(description="Generate a conservative AHE harness surface draft")
    parser.add_argument("harness_root")
    parser.add_argument("--task", default="")
    parser.add_argument("--task-root")
    parser.add_argument("--eval-cmd")
    parser.add_argument("--validation-cmd")
    parser.add_argument("--adapter", default="command")
    parser.add_argument("--include-medium", action="store_true")
    parser.add_argument("--output-manifest")
    args = parser.parse_args()
    result = discover_surface(
        args.harness_root,
        task=args.task,
        task_root=args.task_root,
        eval_command=args.eval_cmd,
        validation_command=args.validation_cmd,
        adapter=args.adapter,
        include_medium=args.include_medium,
    )
    output_path = None
    if args.output_manifest:
        output_path = Path(args.output_manifest).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result.manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "manifest_path": str(output_path) if output_path else None,
        "manifest": result.manifest,
        "report": result.report(),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = ["SurfaceDiscovery", "SurfaceFile", "discover_surface"]
