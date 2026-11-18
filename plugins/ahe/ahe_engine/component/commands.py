"""基于 manifest 的命令解析（runtime root、评测器、debugger）。"""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Mapping

from harness_optimizer.schema import HarnessSpec, load_spec

def _runtime_root(spec: HarnessSpec, fallback: Path, explicit: str | None = None) -> Path | None:
    """解析 manifest 声明的可选外部 runtime checkout。"""
    raw = explicit or spec.runtime.root
    if not raw:
        raw = spec.adapter.config.get(
            "runtime_root",
            spec.adapter.config.get("root"),
        )
    if not isinstance(raw, str) or not raw.strip():
        return None
    expanded = os.path.expandvars(os.path.expanduser(raw.strip()))
    if "$" in expanded:
        return None
    path = Path(expanded)
    if not path.is_absolute():
        path = fallback / path
    path = path.resolve()
    return path if path.is_dir() else None


def _manifest_for_root(manifest_path: str | None, root: Path) -> HarnessSpec:
    spec = load_spec(manifest_path or root)
    # 组件路径始终相对于用户提供的 harness 根目录，
    # 即使 manifest 存放在该根目录之外。
    return replace(spec, root=root.resolve())


def _evaluation_command(spec: HarnessSpec, explicit: str | None) -> str | list[str] | None:
    if explicit:
        return explicit
    for key in ("command", "eval_command", "evaluator_command"):
        command = spec.adapter.config.get(key)
        if isinstance(command, str) and command.strip():
            return command
        if isinstance(command, (list, tuple)) and command:
            return [str(item) for item in command]
    command = spec.adapter.config.get("command")
    if isinstance(command, str) and command.strip():
        return command
    if isinstance(command, (list, tuple)) and command:
        return [str(item) for item in command]
    evaluation = spec.metadata.get("evaluation")
    if isinstance(evaluation, Mapping):
        command = evaluation.get("command")
        if isinstance(command, str) and command.strip():
            return command
        if isinstance(command, (list, tuple)) and command:
            return [str(item) for item in command]
    command = spec.metadata.get("evaluation_command")
    if isinstance(command, str) and command.strip():
        return command
    return None


def _expand_command_env(command: str | list[str] | tuple[str, ...] | None):
    if isinstance(command, str):
        return os.path.expandvars(command)
    if isinstance(command, (list, tuple)):
        return [os.path.expandvars(str(item)) for item in command]
    return command


def _debugger_command(spec: HarnessSpec, explicit: str | list[str] | tuple[str, ...] | None = None):
    """解析 manifest 声明的可选失败分析命令。"""
    if explicit:
        return explicit
    for source in (spec.metadata.get("debugger"), spec.metadata.get("debug")):
        if isinstance(source, Mapping):
            command = source.get("command")
        else:
            command = source
        if isinstance(command, str) and command.strip():
            return command
        if isinstance(command, (list, tuple)) and command:
            return [str(item) for item in command]
    for key in ("debugger_command", "debug_command"):
        command = spec.metadata.get(key)
        if isinstance(command, str) and command.strip():
            return command
        if isinstance(command, (list, tuple)) and command:
            return [str(item) for item in command]
    command = spec.adapter.config.get("debugger_command", spec.adapter.config.get("debug_command"))
    if isinstance(command, str) and command.strip():
        return command
    if isinstance(command, (list, tuple)) and command:
        return [str(item) for item in command]
    return None


def _structured_patch_mode(spec: HarnessSpec) -> bool:
    """该 manifest 是否要求对既有文件使用真正的局部 patch。"""
    raw = spec.metadata.get("patch_mode", spec.metadata.get("mutation_mode", ""))
    return str(raw).strip().lower() in {"structured", "patch", "range", "local"}

