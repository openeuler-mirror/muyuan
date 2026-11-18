"""harness 专属校验行为的 adapter 协议。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, Union, TYPE_CHECKING

from .schema import AdapterSpec, HarnessSpec

if TYPE_CHECKING:
    from .runner import RunRecord
    from .workspace import CandidateWorkspace


Command = Union[str, Sequence[str]]


class HarnessAdapter(Protocol):
    """harness 后端实现的最小合同。

    adapter 决定 manifest 如何运行、输出如何变成指标；
    workspace 安全与进程生命周期保持通用。
    """

    name: str

    def discover(self, root: Path, spec: HarnessSpec) -> Sequence[Any]:
        ...

    def snapshot(self, root: Path, workspace_root: Path | None = None, candidate_id: str | None = None, *, spec: HarnessSpec | None = None) -> "CandidateWorkspace":
        ...

    def apply(self, candidate: "CandidateWorkspace", changes: Mapping[str, str | bytes | bytearray | None], *, spec: HarnessSpec | None = None) -> Sequence[Path]:
        ...

    def build_command(self, spec: HarnessSpec, workspace: Path) -> Command:
        ...

    def run(self, spec: HarnessSpec, candidate: "CandidateWorkspace", *, shell: bool = False) -> "RunRecord":
        ...

    def collect_trace(self, record: "RunRecord") -> Mapping[str, Any]:
        ...

    def rollback(self, candidate: "CandidateWorkspace") -> None:
        ...

    def parse_metrics(self, stdout: str, stderr: str = "") -> Mapping[str, Any]:
        ...


class CommandAdapter:
    """默认 adapter：执行 ``validation.command`` 并解析 JSON 输出。"""

    name = "command"

    def discover(self, root: Path, spec: HarnessSpec) -> Sequence[Any]:
        return spec.components

    def snapshot(self, root: Path, workspace_root: Path | None = None, candidate_id: str | None = None, *, spec: HarnessSpec | None = None) -> "CandidateWorkspace":
        from .workspace import create_candidate

        return create_candidate(root, workspace_root, candidate_id, spec=spec)

    def apply(self, candidate: "CandidateWorkspace", changes: Mapping[str, str | bytes | bytearray | None], *, spec: HarnessSpec | None = None) -> Sequence[Path]:
        from .workspace import apply_changes

        return apply_changes(candidate, changes, spec=spec)

    def build_command(self, spec: HarnessSpec, workspace: Path) -> Command:
        command = spec.validation.command
        if command is None:
            raise ValueError(f"manifest {spec.name!r} has no validation.command")
        return command

    def parse_metrics(self, stdout: str, stderr: str = "") -> Mapping[str, Any]:
        # 惰性导入，使 adapter 不依赖进程 runner 也可用
        # （同时避免模块循环）。
        from .runner import parse_metrics

        return parse_metrics(stdout, stderr=stderr)

    def run(self, spec: HarnessSpec, candidate: "CandidateWorkspace", *, shell: bool = False) -> "RunRecord":
        from .runner import run_validation

        return run_validation(spec, candidate, adapter=self, shell=shell)

    def collect_trace(self, record: "RunRecord") -> Mapping[str, Any]:
        return record.to_dict()

    def rollback(self, candidate: "CandidateWorkspace") -> None:
        candidate.rollback()


_COMMAND_ADAPTER = CommandAdapter()
# 别名共用命令式实现，manifest 可在任意命令式 harness 之间移植；
# harness 专属行为通过 register_adapter 按需注册。
_ADAPTERS: dict[str, HarnessAdapter] = {
    "command": _COMMAND_ADAPTER,
    "generic": _COMMAND_ADAPTER,
}


def register_adapter(name: str, adapter: HarnessAdapter) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("adapter name must be non-empty")
    _ADAPTERS[name.strip().lower()] = adapter


def get_adapter(name: str) -> HarnessAdapter:
    try:
        return _ADAPTERS[name.strip().lower()]
    except KeyError as exc:
        known = ", ".join(sorted(_ADAPTERS))
        raise KeyError(f"unknown harness adapter {name!r}; known adapters: {known}") from exc


def resolve_adapter(spec: HarnessSpec | AdapterSpec | str) -> HarnessAdapter:
    if isinstance(spec, HarnessSpec):
        name = spec.adapter.name
    elif isinstance(spec, AdapterSpec):
        name = spec.name
    else:
        name = spec
    return get_adapter(name)


__all__ = [
    "Command",
    "CommandAdapter",
    "HarnessAdapter",
    "get_adapter",
    "register_adapter",
    "resolve_adapter",
]
