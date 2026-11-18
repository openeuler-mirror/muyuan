"""运行环境组装与 adapter 命令渲染。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from harness_optimizer.schema import HarnessSpec

def _env_for_run(
    root: Path,
    task: str,
    trace_dir: Path,
    spec: HarnessSpec,
    runtime_root: str | None,
    adapter: Any | None = None,
    stage: str = "dev",
) -> dict[str, str]:
    env = {str(k): str(v) for k, v in spec.validation.env.items()}
    if adapter is not None and hasattr(adapter, "build_environment"):
        # adapter 自有变量（runtime root、组件 manifest、模型设置）先于
        # 下方 AHE 运行身份合并，保证 task/trace 值在 dev 与 holdout
        # 运行中都保持权威。
        env.update({str(k): str(v) for k, v in adapter.build_environment(spec, root).items()})
    env.update({
        "AHE_HARNESS_ROOT": str(root),
        "AHE_HARNESS": str(root),
        "AHE_TASK": task,
        "AHE_TRACE_DIR": str(trace_dir),
        "AHE_COMPONENT_MANIFEST": str(spec.manifest_path or ""),
        "AHE_ADAPTER": spec.adapter.name,
        "AHE_EVAL_STAGE": stage,
    })
    declared_runtime = bool(str(spec.runtime.root or "").strip()) or bool(spec.runtime.components)
    if declared_runtime:
        # manifest 声明的 runtime checkout：候选快照本身就是 runtime 副本，
        # 评测对象即运行时根。
        env["AHE_RUNTIME_ROOT"] = str(root)
        env["RUNTIME_ROOT"] = str(root)
    elif runtime_root:
        env["AHE_RUNTIME_ROOT"] = runtime_root
        env["RUNTIME_ROOT"] = runtime_root
    return env


def _render_adapter_command(adapter: Any | None, spec: HarnessSpec,
                            root: Path, command: str | list[str] | tuple[str, ...],
                            runtime_root: str | None = None) -> str | list[str] | tuple[str, ...]:
    """渲染 runtime 占位符，同时保留通用 adapter 支持。"""
    if adapter is None:
        return command
    renderer = getattr(adapter, "render_command_for", None)
    if callable(renderer):
        return renderer(spec, root, command, runtime_root)
    return command

