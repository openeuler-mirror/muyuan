"""为变异 agent 构建有界的只读上下文。"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Mapping

from harness_optimizer.schema import HarnessSpec

def _reference_materials(spec: HarnessSpec, root: Path, max_chars: int = 24000) -> str:
    """为变异 Agent 读取有界的只读参考材料。

    参考路径与 ``components`` 刻意分离：它们永远不会进入变更白名单，
    因此模型提案无法修改它们。相对路径先按 manifest 解析，再按
    runtime root 解析，保证 manifest 可移植。
    """
    raw = spec.metadata.get("references", spec.metadata.get("reference_materials", ()))
    if isinstance(raw, (str, Mapping)):
        items = [raw]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = []
    if not items:
        return "(none declared)"
    manifest_dir = spec.manifest_path.parent if spec.manifest_path else root
    chunks: list[str] = []
    used = 0
    for index, item in enumerate(items):
        if isinstance(item, Mapping):
            raw_path = item.get("path")
            label = str(item.get("id", raw_path or f"reference-{index}"))
            try:
                item_limit = int(item.get("max_chars", max_chars))
            except (TypeError, ValueError):
                item_limit = max_chars
        else:
            raw_path = item
            label = str(item)
            item_limit = max_chars
        if not isinstance(raw_path, str) or not raw_path.strip():
            chunks.append(f"--- {label} [invalid path] ---")
            continue
        expanded = Path(os.path.expandvars(os.path.expanduser(raw_path.strip())))
        candidates = [expanded] if expanded.is_absolute() else [manifest_dir / expanded, root / expanded]
        path = next((candidate.resolve() for candidate in candidates if candidate.exists()), None)
        if path is None:
            chunks.append(f"--- {label} ({raw_path}) [missing] ---")
            continue
        files = [path] if path.is_file() else sorted(
            file_path for file_path in path.rglob("*")
            if file_path.is_file() and file_path.suffix.lower() in {
                ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".py", ".ts", ".js"
            }
        )
        if not files:
            chunks.append(f"--- {label} ({path}) [no readable files] ---")
            continue
        for file_path in files:
            remaining = max_chars - used
            if remaining <= 0:
                break
            try:
                content = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            limit = min(max(1000, item_limit), remaining)
            relative = file_path.relative_to(path).as_posix() if path.is_dir() else file_path.name
            chunks.append(f"--- {label}: {relative} ---\n{content[:limit]}")
            used += min(len(content), limit)
        if used >= max_chars:
            break
    return "\n\n".join(chunks) or "(none readable)"


def _component_files(root: Path, spec: HarnessSpec, max_chars: int = 150000) -> str:
    chunks: list[str] = []
    used = 0
    for component in spec.all_components:
        path = root / Path(component.path)
        if path.is_dir():
            files = sorted(p for p in path.rglob("*") if p.is_file())
        elif path.is_file():
            files = [path]
        else:
            files = []
        if not files:
            chunks.append(f"--- {component.id} ({component.kind}) {component.path} [missing] ---")
            continue
        for file_path in files:
            try:
                content = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            relative = file_path.relative_to(root).as_posix()
            remaining = max_chars - used
            if remaining <= 0:
                break
            content = content[:max(1000, remaining)]
            full_content = file_path.read_text(encoding="utf-8")
            file_hash = hashlib.sha256(full_content.encode("utf-8")).hexdigest()
            chunks.append(
                f"--- {component.id} ({component.kind}) {relative} ---\n"
                f"file_sha256: {file_hash}\n"
                f"line_count: {full_content.count(chr(10)) + 1}\n{content}"
            )
            used += len(content)
        if used >= max_chars:
            break
    return "\n\n".join(chunks)

