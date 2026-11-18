"""通用 harness optimizer 的 manifest 模型与发现。

本模块为任意 harness manifest 提供宽松加载，同时为新 adapter
暴露小型类型化合同。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


class ManifestError(ValueError):
    """harness manifest 无法被安全解释时抛出。"""


def _relative_path(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{field_name} must be a non-empty relative path")
    path = value.replace("\\", "/")
    if path.startswith("/") or (len(path) >= 2 and path[1] == ":"):
        raise ManifestError(f"{field_name} must be relative: {value!r}")
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ManifestError(f"{field_name} may not contain '..': {value!r}")
    return "/".join(parts) or "."


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ManifestError(f"{field_name} must be an object")
    return value


@dataclass(frozen=True)
class ComponentSpec:
    """optimizer 可编辑的、manifest 声明的文件或目录。"""

    id: str
    kind: str
    path: str
    mutable: bool = True

    @classmethod
    def from_value(cls, value: Any, index: int = 0) -> "ComponentSpec":
        if isinstance(value, str):
            path = _relative_path(value, f"components[{index}]")
            return cls(id=Path(path).name or f"component-{index}", kind="file", path=path)
        obj = _mapping(value, f"components[{index}]")
        component_id = obj.get("id")
        if not isinstance(component_id, str) or not component_id.strip():
            raise ManifestError(f"components[{index}].id must be non-empty")
        path = _relative_path(obj.get("path"), f"components[{index}].path")
        kind = obj.get("kind", "file")
        if not isinstance(kind, str) or not kind.strip():
            raise ManifestError(f"components[{index}].kind must be non-empty")
        mutable = obj.get("mutable", True)
        if not isinstance(mutable, bool):
            raise ManifestError(f"components[{index}].mutable must be boolean")
        return cls(component_id.strip(), kind.strip(), path, mutable)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "path": self.path, "mutable": self.mutable}


@dataclass(frozen=True)
class ValidationSpec:
    """候选如何被校验。

    命令可以是 shell 风格字符串或 argv 序列。runner 默认按 argv
    执行；调用方必须显式选择 shell。
    """

    command: str | tuple[str, ...] | None = None
    timeout_s: float = 7200.0
    env: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_value(cls, value: Any) -> "ValidationSpec":
        if value is None:
            return cls()
        if isinstance(value, str) or isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            command = value if isinstance(value, str) else tuple(str(item) for item in value)
            return cls(command=command)
        obj = _mapping(value, "validation")
        raw_command = obj.get("command")
        if raw_command is None or isinstance(raw_command, str):
            command = raw_command
        elif isinstance(raw_command, Sequence) and not isinstance(raw_command, (bytes, bytearray)):
            command = tuple(str(item) for item in raw_command)
        else:
            raise ManifestError("validation.command must be a string or argv array")
        try:
            timeout_s = float(obj.get("timeout_s", obj.get("timeout", 7200.0)))
        except (TypeError, ValueError) as exc:
            raise ManifestError("validation.timeout_s must be numeric") from exc
        if timeout_s <= 0:
            raise ManifestError("validation.timeout_s must be greater than zero")
        env = _mapping(obj.get("env"), "validation.env")
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
            raise ManifestError("validation.env keys and values must be strings")
        return cls(command=command, timeout_s=timeout_s, env=dict(env))

    def to_dict(self) -> dict[str, Any]:
        command: Any = self.command
        if isinstance(command, tuple):
            command = list(command)
        result: dict[str, Any] = {"command": command, "timeout_s": self.timeout_s}
        if self.env:
            result["env"] = dict(self.env)
        return result


@dataclass(frozen=True)
class AdapterSpec:
    """adapter 名称及 adapter 专属配置。"""

    name: str = "command"
    config: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_value(cls, value: Any) -> "AdapterSpec":
        if value is None:
            return cls()
        if isinstance(value, str):
            if not value.strip():
                raise ManifestError("adapter must be non-empty")
            return cls(value.strip())
        obj = _mapping(value, "adapter")
        name = obj.get("name", obj.get("type", "command"))
        if not isinstance(name, str) or not name.strip():
            raise ManifestError("adapter.name must be non-empty")
        config = {str(key): value for key, value in obj.items() if key not in {"name", "type"}}
        return cls(name.strip(), config)

    def to_value(self) -> str | dict[str, Any]:
        if not self.config:
            return self.name
        return {"name": self.name, **dict(self.config)}


@dataclass(frozen=True)
class RuntimeSpec:
    """暴露给 adapter 的 runtime checkout 与文件。

    harness 可以与其修改的 runtime 分属不同仓库（AHE manifest 在一个
    checkout，被优化的 runtime 在另一个）。``root`` 刻意用字符串而非
    ``Path``，使 ``$RUNTIME_ROOT`` 这类环境占位符保持可移植。
    runtime 文件与普通组件使用相同的相对路径与可变性检查。
    """

    root: str | None = None
    components: tuple[ComponentSpec, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_value(cls, value: Any) -> "RuntimeSpec":
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ManifestError("runtime must be an object")
        raw_root = value.get("root", value.get("runtime_root"))
        if raw_root is not None and (not isinstance(raw_root, str) or not raw_root.strip()):
            raise ManifestError("runtime.root must be a non-empty string when provided")
        raw_components = value.get("components", value.get("runtime_components", ()))
        if not isinstance(raw_components, Sequence) or isinstance(raw_components, (str, bytes, bytearray)):
            raise ManifestError("runtime.components must be an array")
        components = tuple(
            ComponentSpec.from_value(item, index) for index, item in enumerate(raw_components)
        )
        ids = [component.id for component in components]
        if len(ids) != len(set(ids)):
            raise ManifestError("runtime component ids must be unique")
        env = _mapping(value.get("env"), "runtime.env")
        if any(not isinstance(key, str) or not isinstance(item, str) for key, item in env.items()):
            raise ManifestError("runtime.env keys and values must be strings")
        return cls(root=raw_root.strip() if isinstance(raw_root, str) else None,
                   components=components, env=dict(env))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "components": [component.to_dict() for component in self.components],
        }
        if self.root is not None:
            result["root"] = self.root
        if self.env:
            result["env"] = dict(self.env)
        return result


@dataclass(frozen=True)
class HarnessSpec:
    """经验证、可移植的可优化 harness 描述。"""

    name: str
    version: int | str = 1
    description: str = ""
    components: tuple[ComponentSpec, ...] = ()
    validation: ValidationSpec = field(default_factory=ValidationSpec)
    adapter: AdapterSpec = field(default_factory=AdapterSpec)
    runtime: RuntimeSpec = field(default_factory=RuntimeSpec)
    root: Path | None = None
    manifest_path: Path | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        *,
        root: Path | None = None,
        manifest_path: Path | None = None,
    ) -> "HarnessSpec":
        if not isinstance(data, Mapping):
            raise ManifestError("manifest must be a JSON object")
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            name = root.name if root is not None else "harness"
        raw_components = data.get("components")
        if raw_components is None:
            inferred: list[dict[str, Any]] = []
            # 保持旧 AHE manifest 可加载，同时默认不把其 extension
            # 入口变为可变。
            if isinstance(data.get("engine"), str):
                inferred.append({"id": "engine", "kind": "engine", "path": data["engine"], "mutable": True})
            if isinstance(data.get("entry"), str):
                inferred.append({"id": "entry", "kind": "entry", "path": data["entry"], "mutable": False})
            # 纯 runtime manifest 绝不能默默把整个候选变为可变。
            # 其白名单由 runtime.components 提供。
            if inferred:
                raw_components = inferred
            elif data.get("runtime") is not None or "runtime_components" in data:
                raw_components = []
            else:
                raw_components = [{"id": "root", "kind": "directory", "path": ".", "mutable": True}]
        if not isinstance(raw_components, Sequence) or isinstance(raw_components, (str, bytes, bytearray)):
            raise ManifestError("components must be an array")
        components = tuple(ComponentSpec.from_value(value, index) for index, value in enumerate(raw_components))
        ids = [component.id for component in components]
        if len(ids) != len(set(ids)):
            raise ManifestError("component ids must be unique")
        validation = ValidationSpec.from_value(data.get("validation"))
        adapter = AdapterSpec.from_value(data.get("adapter"))
        runtime_value = data.get("runtime")
        # 序列化到显式 runtime 对象时，
        # 保留符合人体工学的顶层别名。
        if runtime_value is None and ("runtime_root" in data or "runtime_components" in data):
            runtime_value = {
                "root": data.get("runtime_root"),
                "components": data.get("runtime_components", ()),
                "env": data.get("runtime_env", {}),
            }
        runtime = RuntimeSpec.from_value(runtime_value)
        known = {"$schema", "format", "schema_version", "name", "version", "description", "components", "validation", "adapter",
                 "runtime", "runtime_root", "runtime_components", "runtime_env"}
        metadata = {key: value for key, value in data.items() if key not in known}
        version = data.get("version", data.get("format", data.get("schema_version", 1)))
        return cls(
            name=name.strip(),
            version=version,
            description=str(data.get("description", "")),
            components=components,
            validation=validation,
            adapter=adapter,
            runtime=runtime,
            root=root,
            manifest_path=manifest_path,
            metadata=metadata,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "components": [component.to_dict() for component in self.components],
            "validation": self.validation.to_dict(),
            "adapter": self.adapter.to_value(),
            "runtime": self.runtime.to_dict(),
        }
        result.update(dict(self.metadata))
        return result

    def component(self, component_id: str) -> ComponentSpec:
        for component in (*self.components, *self.runtime.components):
            if component.id == component_id:
                return component
        raise KeyError(component_id)

    @property
    def all_components(self) -> tuple[ComponentSpec, ...]:
        """全部声明文件，含 adapter 自有的 runtime 组件。"""
        return (*self.components, *self.runtime.components)


_MANIFEST_NAMES = (
    "harness.manifest.json",
    ".ahe-manifest.json",
    ".harness/manifest.json",
    "manifest.json",
)


def find_manifest(path: os.PathLike[str] | str) -> Path | None:
    """从 harness 目录或 manifest 文件路径定位 manifest。"""
    candidate = Path(path).expanduser()
    if candidate.is_file():
        return candidate if candidate.suffix.lower() in {".json", ".toml"} else None
    if not candidate.is_dir():
        return None
    for relative in _MANIFEST_NAMES:
        found = candidate / relative
        if found.is_file():
            # 很多插件仓库已有名为 ``manifest.json`` 的包清单。
            # 自动发现绝不能把它误认为可执行的 harness 合同；
            # 显式 --manifest 仍允许调用方加载旧式 manifest。
            if relative == "manifest.json":
                try:
                    data = json.loads(found.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(data, Mapping) or "components" not in data:
                    continue
            return found
    return None


def load_spec(path: os.PathLike[str] | str) -> HarnessSpec:
    """从文件或 harness 根加载并校验 JSON/TOML manifest。"""
    manifest = find_manifest(path)
    if manifest is None:
        raise FileNotFoundError(f"no harness manifest found under {path}")
    try:
        if manifest.suffix.lower() == ".toml":
            try:
                import tomllib  # type: ignore[attr-defined]
            except ImportError as exc:  # pragma: no cover - Python 3.10 fallback
                raise ManifestError("TOML manifests require Python 3.11+") from exc
            data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        else:
            data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"unable to read manifest {manifest}: {exc}") from exc
    return HarnessSpec.from_dict(data, root=manifest.parent, manifest_path=manifest)


__all__ = [
    "AdapterSpec",
    "ComponentSpec",
    "HarnessSpec",
    "ManifestError",
    "RuntimeSpec",
    "ValidationSpec",
    "find_manifest",
    "load_spec",
]
