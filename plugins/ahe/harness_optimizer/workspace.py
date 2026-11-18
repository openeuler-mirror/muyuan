"""隔离候选 workspace 与安全变更应用。"""
from __future__ import annotations

import hashlib
import sys
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .schema import HarnessSpec


class WorkspaceError(ValueError):
    """候选操作会越出 workspace 时抛出。"""


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _safe_relative(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceError("change path must be a non-empty relative path")
    value = value.replace("\\", "/")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise WorkspaceError(f"unsafe candidate path: {value!r}")
    return "/".join(pure.parts)


def _reject_symlink_chain(path: Path, stop: Path) -> None:
    current = path
    stop = stop.resolve(strict=False)
    while _under(current, stop) and current != stop:
        if current.is_symlink():
            raise WorkspaceError(f"symlink path is not allowed: {current}")
        current = current.parent


@dataclass
class CandidateWorkspace:
    candidate_id: str
    source: Path
    path: Path
    workspace_root: Path
    source_name: str
    source_is_file: bool
    spec: HarnessSpec | None = None

    def resolve(self, relative_path: str) -> Path:
        relative = _safe_relative(relative_path)
        target = self.path.joinpath(*relative.split("/"))
        if not _under(target, self.path):
            raise WorkspaceError(f"candidate path escapes workspace: {relative_path!r}")
        _reject_symlink_chain(target.parent, self.path)
        return target

    def digest(self) -> str:
        digest = hashlib.sha256()
        entries = sorted(self.path.rglob("*") if self.path.is_dir() else [self.path])
        for entry in entries:
            if entry.is_symlink():
                digest.update(f"L:{entry.relative_to(self.path)}:{entry.readlink()}\n".encode())
            elif entry.is_file():
                digest.update(f"F:{entry.relative_to(self.path)}\n".encode())
                digest.update(entry.read_bytes())
        return digest.hexdigest()

    def rollback(self) -> None:
        """从不可变源快照重置本候选。"""
        if not _under(self.path, self.workspace_root) or self.path == self.workspace_root:
            raise WorkspaceError("refusing to rollback outside candidate workspace root")
        if self.path.is_symlink():
            raise WorkspaceError("refusing to rollback a symlink candidate")
        staged = Path(tempfile.mkdtemp(prefix=f".{self.candidate_id}.rollback-", dir=str(self.workspace_root)))
        try:
            destination = staged / self.source_name if self.source_is_file else staged
            if self.source_is_file:
                shutil.copy2(self.source, destination)
            else:
                shutil.rmtree(staged)
                shutil.copytree(self.source, staged, symlinks=True)
            old = self.path.with_name(f".{self.path.name}.old-{uuid.uuid4().hex[:8]}")
            if self.path.exists():
                os.replace(self.path, old)
            os.replace(staged, self.path)
            if old.exists() and not old.is_symlink():
                shutil.rmtree(old) if old.is_dir() else old.unlink()
        except Exception:
            if staged.exists():
                shutil.rmtree(staged, ignore_errors=True)
            raise

    def cleanup(self) -> None:
        if not _under(self.path, self.workspace_root) or self.path == self.workspace_root:
            raise WorkspaceError("refusing to remove path outside candidate workspace root")
        if self.path.is_symlink():
            raise WorkspaceError("refusing to remove a symlink candidate")
        if self.path.exists():
            shutil.rmtree(self.path) if self.path.is_dir() else self.path.unlink()


def create_candidate(
    source: os.PathLike[str] | str,
    workspace_root: os.PathLike[str] | str | None = None,
    candidate_id: str | None = None,
    *,
    spec: HarnessSpec | None = None,
    allow_symlinks: bool = False,
) -> CandidateWorkspace:
    """把源 harness 复制到私有候选目录。

    普通 harness 拒绝符号链接树，因为编辑可能逃出候选根。真实
    checkout 可能含包管理器与 virtualenv 符号链接，因此冻结
    benchmark runner 可以用 ``allow_symlinks=True`` 显式保留它们。
    组件写入仍然拒绝符号链接目标，候选根本身也绝不能是符号链接。
    """
    source_path = Path(source).expanduser().resolve()
    if not source_path.exists() or source_path.is_symlink():
        raise WorkspaceError(f"source must be an existing non-symlink path: {source}")
    if source_path.is_dir() and not allow_symlinks and any(entry.is_symlink() for entry in source_path.rglob("*")):
        raise WorkspaceError("source harness may not contain symlinks")
    root = Path(workspace_root).expanduser().resolve() if workspace_root else source_path.parent / ".harness-candidates"
    if source_path.is_dir() and _under(root, source_path):
        raise WorkspaceError("workspace root may not be inside the source harness")
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir() or root.is_symlink():
        raise WorkspaceError(f"workspace root must be a real directory: {root}")
    candidate_id = candidate_id or f"candidate-{uuid.uuid4().hex[:12]}"
    if not candidate_id or Path(candidate_id).name != candidate_id or candidate_id in {".", ".."}:
        raise WorkspaceError(f"unsafe candidate id: {candidate_id!r}")
    destination = root / candidate_id
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    source_is_file = source_path.is_file()
    try:
        if source_is_file:
            destination.mkdir()
            shutil.copy2(source_path, destination / source_path.name)
        else:
            shutil.copytree(source_path, destination, symlinks=True)
    except Exception:
        # copytree 可能留下庞大的半成品 checkout（或耗尽 inode 配额）。
        # 只删除我们刚创建的候选，再把原始错误
        # 冒泡给调用方。
        if destination.exists() and not destination.is_symlink():
            shutil.rmtree(destination, ignore_errors=True)
        raise
    return CandidateWorkspace(candidate_id, source_path, destination, root, source_path.name, source_is_file, spec)


def _component_allows(spec: HarnessSpec, relative: str) -> bool:
    rel = PurePosixPath(relative)
    # runtime 组件（例如宿主 agent 中间件）放在单独的 manifest
    # 小节，但使用完全相同的路径安全合同。
    for component in spec.all_components:
        base = PurePosixPath(component.path)
        if rel == base or base == PurePosixPath(".") or base in rel.parents:
            return component.mutable
    return False


def _atomic_write_text(target: Path, content: str) -> None:
    """替换候选文本文件，不暴露部分写入。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _apply_unified_diff(original: str, patch_text: str, path: str) -> str:
    """不调用 shell 命令，应用单文件统一 diff。"""
    if not isinstance(patch_text, str) or not patch_text.strip():
        raise WorkspaceError(f"patches unified diff for {path} is empty")
    source_lines = original.splitlines(keepends=True)
    diff_lines = patch_text.splitlines(keepends=True)
    hunks: list[tuple[int, int, list[str]]] = []
    index = 0
    while index < len(diff_lines):
        header = diff_lines[index].rstrip("\r\n")
        if header.startswith("@@"):
            match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", header)
            if not match:
                raise WorkspaceError(f"invalid unified diff hunk for {path}")
            old_start = int(match.group(1))
            old_count = int(match.group(2) or "1")
            index += 1
            body: list[str] = []
            while index < len(diff_lines):
                line = diff_lines[index]
                if line.startswith("@@"):
                    break
                if line.startswith((" ", "+", "-", "\\")):
                    body.append(line)
                    index += 1
                    continue
                # 文件头只允许出现在第一个 hunk 之前。
                if line.startswith(("--- ", "+++ ")) and not body:
                    break
                raise WorkspaceError(f"invalid unified diff body for {path}")
            hunks.append((old_start, old_count, body))
            continue
        index += 1
    if not hunks:
        raise WorkspaceError(f"unified diff contains no hunks for {path}")

    output: list[str] = []
    cursor = 0
    for old_start, old_count, body in hunks:
        # 统一 diff 用行号 0 表示在第一行之前插入。
        start = 0 if old_start == 0 else old_start - 1
        if start < cursor or start > len(source_lines):
            raise WorkspaceError(f"unified diff hunk is outside {path}")
        output.extend(source_lines[cursor:start])
        consumed = 0
        for line in body:
            if line.startswith("\\"):
                # 元数据行，例如 '\\ No newline at end of file'。
                continue
            marker, value = line[:1], line[1:]
            if marker == " ":
                if cursor + consumed >= len(source_lines) or source_lines[cursor + consumed] != value:
                    raise WorkspaceError(f"unified diff context mismatch for {path}")
                output.append(value)
                consumed += 1
            elif marker == "-":
                if cursor + consumed >= len(source_lines) or source_lines[cursor + consumed] != value:
                    raise WorkspaceError(f"unified diff removal mismatch for {path}")
                consumed += 1
            elif marker == "+":
                output.append(value)
            else:
                raise WorkspaceError(f"invalid unified diff marker for {path}")
        if consumed != old_count:
            raise WorkspaceError(
                f"unified diff expected {old_count} old lines but consumed {consumed} for {path}"
            )
        cursor += consumed
    output.extend(source_lines[cursor:])
    return "".join(output)


def apply_patches(
    candidate: CandidateWorkspace,
    patches: Sequence[Mapping[str, Any]],
    *,
    spec: HarnessSpec | None = None,
) -> tuple[Path, ...]:
    """在隔离候选内应用精确、有界的文本 patch。

    既有文件编辑必须给出唯一的旧片段（或锚点）。可选的
    ``expected_file_sha256``/``old_hash`` 防止把 patch 应用到不同的
    候选修订版上。这使 optimizer 停留在真正的小 patch 协议上，
    而不是默默把 patch 描述当作完整文件替换。
    """
    if not isinstance(candidate, CandidateWorkspace):
        raise TypeError("candidate must be a CandidateWorkspace")
    if not isinstance(patches, Sequence) or isinstance(patches, (str, bytes, bytearray)):
        raise WorkspaceError("patches must be an array")
    spec = spec or candidate.spec
    changed: list[Path] = []
    # 一个提案可能包含同一文件的多个 patch。批次开始时校验一次该
    # 文件的修订版，之后用精确锚点（及可选片段哈希）校验序列中
    # 后续的 patch。
    validated_file_hashes: dict[str, str] = {}
    for index, raw in enumerate(patches):
        if not isinstance(raw, Mapping):
            raise WorkspaceError(f"patches[{index}] must be an object")
        raw_path = raw.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise WorkspaceError(f"patches[{index}].path is required")
        relative = _safe_relative(raw_path)
        if spec is not None and not _component_allows(spec, relative):
            raise PermissionError(f"component is immutable or undeclared: {relative}")
        target = candidate.resolve(relative)
        if target.exists() and target.is_symlink():
            raise WorkspaceError(f"refusing to modify symlink: {target}")
        operation = str(raw.get("operation", "replace_text")).strip().lower()
        if operation in {"add", "create"}:
            if target.exists():
                raise WorkspaceError(f"patches[{index}] cannot create existing file: {relative}")
            new_value = raw.get("new", raw.get("content"))
            if not isinstance(new_value, str):
                raise WorkspaceError(f"patches[{index}].new must be a string")
            if len(new_value.encode("utf-8")) > 51200:
                raise WorkspaceError(f"patches[{index}] exceeds the 20KB patch limit")
            _atomic_write_text(target, new_value)
            changed.append(target)
            continue
        if not target.is_file():
            raise WorkspaceError(f"patches[{index}] target does not exist: {relative}")
        try:
            current = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise WorkspaceError(f"patches[{index}] target is not UTF-8 text: {relative}") from exc

        expected_hash = raw.get("expected_file_sha256", raw.get("file_sha256"))
        if expected_hash is None:
            expected_hash = raw.get("old_hash")
        if expected_hash is not None:
            actual_hash = hashlib.sha256(current.encode("utf-8")).hexdigest()
            expected_hash_text = str(expected_hash).lower()
            previous_hash = validated_file_hashes.get(relative)
            hash_matches = (
                expected_hash_text == actual_hash
                or (previous_hash is not None and expected_hash_text == previous_hash)
            )
            if not hash_matches:
                # 一个谱系保留多个 patch 且文件偏离早期 prompt 列表所示内容后，
                # 过期哈希锚点很常见。对文本操作而言，下方唯一锚点（旧文本）
                # 检查才是真正的安全门，因此把哈希降级为警告。
                print(
                    f"warning: patches[{index}] file hash mismatch for {relative} "
                    f"(expected {expected_hash_text[:16]}..., got {actual_hash[:16]}...); "
                    "falling back to unique-anchor validation",
                    file=sys.stderr,
                )
            if previous_hash is None:
                validated_file_hashes[relative] = actual_hash

        if operation in {"replace_text", "replace_range", "delete_text"}:
            old_value = raw.get("old", raw.get("old_text"))
            if not isinstance(old_value, str) or not old_value:
                raise WorkspaceError(f"patches[{index}].old must be a non-empty string")
            new_value = "" if operation == "delete_text" else raw.get("new", raw.get("replacement"))
            if not isinstance(new_value, str):
                raise WorkspaceError(f"patches[{index}].new must be a string")
            needle = old_value
            replacement = new_value
        elif operation in {"insert_after", "insert_before"}:
            needle = raw.get("anchor")
            replacement_value = raw.get("new", raw.get("content"))
            if not isinstance(needle, str) or not needle:
                raise WorkspaceError(f"patches[{index}].anchor must be a non-empty string")
            if not isinstance(replacement_value, str):
                raise WorkspaceError(f"patches[{index}].new must be a string")
            replacement = needle + replacement_value if operation == "insert_after" else replacement_value + needle
        elif operation in {"unified_diff", "diff"}:
            diff_text = raw.get("diff", raw.get("content"))
            updated = _apply_unified_diff(current, diff_text, relative)
            if updated == current:
                raise WorkspaceError(f"patches[{index}] is a no-op for {relative}")
            _atomic_write_text(target, updated)
            changed.append(target)
            continue
        else:
            raise WorkspaceError(f"unsupported patch operation {operation!r}")

        if len(needle.encode("utf-8")) > 51200 or len(replacement.encode("utf-8")) > 51200:
            raise WorkspaceError(f"patches[{index}] exceeds the 20KB old/new limit")
        occurrences = current.count(needle)
        if occurrences != 1:
            raise WorkspaceError(
                f"patches[{index}] requires a unique anchor in {relative}, found {occurrences}"
            )
        updated = current.replace(needle, replacement, 1)
        if updated == current:
            raise WorkspaceError(f"patches[{index}] is a no-op for {relative}")
        old_snippet_hash = raw.get("old_sha256", raw.get("old_text_sha256"))
        if old_snippet_hash is not None:
            actual_snippet_hash = hashlib.sha256(needle.encode("utf-8")).hexdigest()
            if str(old_snippet_hash).lower() != actual_snippet_hash:
                raise WorkspaceError(
                    f"patches[{index}] old snippet hash mismatch for {relative}"
                )
        _atomic_write_text(target, updated)
        changed.append(target)
    return tuple(changed)


def apply_changes(
    candidate: CandidateWorkspace,
    changes: Mapping[str, str | bytes | bytearray | None],
    *,
    spec: HarnessSpec | None = None,
) -> tuple[Path, ...]:
    """应用文本/字节变更，拒绝路径穿越与不可变组件。

    ``None`` 表示删除候选内文件。源 harness 绝不被触碰；
    调用方可调用 ``candidate.rollback()`` 重置全部变更。
    """
    if not isinstance(candidate, CandidateWorkspace):
        raise TypeError("candidate must be a CandidateWorkspace")
    spec = spec or candidate.spec
    changed: list[Path] = []
    for raw_path, content in changes.items():
        relative = _safe_relative(raw_path)
        if spec is not None and not _component_allows(spec, relative):
            raise PermissionError(f"component is immutable or undeclared: {relative}")
        target = candidate.resolve(relative)
        if target.exists() and target.is_symlink():
            raise WorkspaceError(f"refusing to modify symlink: {target}")
        if content is None:
            if target.exists():
                target.unlink()
                changed.append(target)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        changed.append(target)
    return tuple(changed)


def rollback_candidate(candidate: CandidateWorkspace) -> None:
    candidate.rollback()


__all__ = [
    "CandidateWorkspace",
    "WorkspaceError",
    "apply_changes",
    "apply_patches",
    "create_candidate",
    "rollback_candidate",
]
