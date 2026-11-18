"""提案解析、散文编辑物化与锚点校验。

同时维护跨 run 的失败 patch 台账，把已经失败过的编辑喂给下一轮
提案。
"""
from __future__ import annotations

import json
import os
import re
import textwrap
from pathlib import Path
from typing import Any, Mapping

from harness_optimizer.schema import HarnessSpec

from .commands import _structured_patch_mode
from .errors import ComponentModeError
from .util import _extract_json_object

def _parse_change_payload(text: str, spec: HarnessSpec) -> dict[str, Any]:
    try:
        payload = _extract_json_object(text)
    except json.JSONDecodeError as exc:
        raise ComponentModeError(f"evolve model did not return JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ComponentModeError("evolve result must be a JSON object")
    raw_changes = payload.get("changes", payload.get("patches", []))
    if not isinstance(raw_changes, list):
        raise ComponentModeError("changes must be an array")
    components = {component.id: component for component in spec.all_components}
    changes: dict[str, str | None] = {}
    patches: list[dict[str, Any]] = []
    manifest_changes: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_changes):
        if not isinstance(raw, Mapping):
            raise ComponentModeError(f"changes[{index}] must be an object")
        # 部分 Responses 模型会把局部 patch 对象包在 content 里，
        # 同时在外层保留组件元数据。先解开这种等价形态，
        # 再应用正常 patch 合同。
        if isinstance(raw.get("content"), Mapping):
            nested = dict(raw["content"])
            if any(key in nested for key in ("old", "old_text", "anchor", "new", "replacement", "operation")):
                merged = dict(raw)
                merged.pop("content", None)
                merged.update(nested)
                for key in ("component_id", "evidence_ids", "reason"):
                    if key not in merged and key in raw:
                        merged[key] = raw[key]
                raw = merged
        component_id = raw.get("component_id")
        path = raw.get("path")
        if component_id is not None:
            if component_id not in components:
                raise ComponentModeError(f"unknown component_id: {component_id}")
            component_path = components[component_id].path.replace("\\", "/").rstrip("/")
            if path is None or not str(path).strip():
                path = component_path
            else:
                path = str(path).replace("\\", "/")
                if path != component_path and not path.startswith(component_path + "/"):
                    raise ComponentModeError(
                        f"changes[{index}].path must stay inside component {component_id}: {path}"
                    )
        if not isinstance(path, str) or not path.strip():
            raise ComponentModeError(f"changes[{index}].path is required")
        operation = str(raw.get("operation", "modify")).strip().lower()
        if operation in {
            "patch", "replace_text", "replace_range", "delete_text",
            "insert_after", "insert_before", "unified_diff", "diff",
        }:
            patch = dict(raw)
            patch["path"] = path
            patch["operation"] = "replace_text" if operation == "patch" else operation
            if patch["operation"] in {"unified_diff", "diff"}:
                diff_text = patch.get("diff", patch.get("content"))
                if not isinstance(diff_text, str) or not diff_text.strip():
                    raise ComponentModeError(
                        f"changes[{index}] unified_diff requires a non-empty diff"
                    )
            patches.append(patch)
            manifest_changes.append({
                "component_id": component_id,
                "path": path,
                "operation": patch["operation"],
                "patch": True,
                "evidence_ids": raw.get("evidence_ids", []),
                "reason": raw.get("reason", ""),
            })
            continue
        if operation in {"delete", "remove"}:
            content: str | None = None
        elif operation in {"add", "create", "modify", "replace", "update"}:
            if _structured_patch_mode(spec) and operation not in {"add", "create"}:
                raise ComponentModeError(
                    f"changes[{index}] must use a local patch operation in structured mode"
                )
            content = raw.get("content")
            if not isinstance(content, str):
                raise ComponentModeError(f"changes[{index}].content must be a string")
        else:
            raise ComponentModeError(f"unsupported operation {operation!r}")
        changes[path] = content
        manifest_changes.append({
            "component_id": component_id,
            "path": path,
            "operation": operation,
            "evidence_ids": raw.get("evidence_ids", []),
            "reason": raw.get("reason", ""),
        })
    return {
        "changes": changes,
        "patches": patches,
        "change_manifest": manifest_changes,
        "reason": payload.get("reason", ""),
        "predicted": payload.get("predicted", {}),
        "risks": payload.get("risks", []),
        "evidence_ids": payload.get("evidence_ids", []),
    }


def _looks_like_change_description(path: str, content: str, root: Path) -> bool:
    """检测本应提供完整组件文件却输出了散文的情况。

    模型偶尔会用自然语言 patch 描述（例如 ``after `anchor`, add: ...``）
    回答 JSON 合同。把这类文本当作源码只会产生必然校验失败的候选。
    此检查刻意保守：正常源码内容原样保留，只有明确的指令形文本
    才在下方被物化。
    """
    value = content.strip()
    if not value:
        return True
    if re.search(r"^\s*(?:in|modify|change|replace|update)\b", value, re.IGNORECASE):
        if "`" in value or re.search(r"\b(?:add|insert|remove)\s*:", value, re.IGNORECASE):
            return True
    if re.search(r"\bcontext\s*:", value, re.IGNORECASE) and re.search(
        r"\bafter\s+[`\"]", value, re.IGNORECASE
    ):
        return True
    current_path = root / Path(path)
    if current_path.is_file():
        try:
            current_size = current_path.stat().st_size
        except OSError:
            current_size = 0
        # 大源码文件的散文片段远短于合法的完整替换。
        # 不对小文件应用此启发式——小文件的真实替换也可能很短。
        if current_size >= 1000 and len(value) < max(400, int(current_size * 0.15)):
            if not any(marker in value for marker in ("export ", "function ", "class ", "{", "}")):
                return True
    return False


def _materialize_inline_change(path: str, description: str, root: Path) -> str | None:
    """物化常见的 ``after anchor, add`` 应答形式。

    这是对合法 JSON 提案的兼容路径：仅限已声明的既有文件，
    且要求精确锚点行；不支持的描述保持拒绝，不做猜测。
    """
    target = root / Path(path)
    if not target.is_file():
        return None
    try:
        current = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    add_match = re.search(r"\badd\s*:\s*", description, re.IGNORECASE)
    if not add_match:
        return None
    before_add = description[: add_match.start()]
    anchors = re.findall(r"[`\"]([^`\"]+)[`\"]", before_add)
    if not anchors:
        return None
    anchor = anchors[-1].strip()
    insertion_text = description[add_match.end() :]
    context_match = re.search(r"\n\s*context\s*:", insertion_text, re.IGNORECASE)
    if context_match:
        insertion_text = insertion_text[: context_match.start()]
    insertion_lines: list[str] = []
    for line in textwrap.dedent(insertion_text).strip("\n").splitlines():
        # 若模型带了统一 diff 风格的前缀，接受并去掉它。
        insertion_lines.append(line[1:] if line.lstrip().startswith("+") else line)
    insertion = "\n".join(insertion_lines).strip("\n")
    if not insertion:
        return None
    lines = current.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.strip() == anchor or anchor in line:
            newline = "\n" if line.endswith("\n") else ""
            payload = insertion + "\n"
            lines.insert(index + 1, payload)
            return "".join(lines)
    return None


def _materialize_proposal(proposal: dict[str, Any], root: Path) -> dict[str, Any]:
    """apply 之前，把受支持的散文编辑转换为完整文件内容。"""
    changes = dict(proposal.get("changes", {}))
    repaired: list[str] = []
    unresolved: list[str] = []
    for path, content in changes.items():
        if content is None or not _looks_like_change_description(path, content, root):
            continue
        materialized = _materialize_inline_change(path, content, root)
        if materialized is None:
            unresolved.append(path)
        else:
            changes[path] = materialized
            repaired.append(path)
    if unresolved:
        raise ComponentModeError(
            "evolve proposal did not contain complete source for: " + ", ".join(unresolved)
        )
    if repaired:
        proposal = dict(proposal)
        proposal["changes"] = changes
        proposal["repair_notes"] = {
            "materialized_inline_changes": repaired,
            "policy": "exact-anchor-only",
        }
    return proposal


def _validate_patch_targets(proposal: dict[str, Any], root: Path) -> dict[str, Any]:
    """只保留锚点存在于当前候选源码中的 patch。"""
    patches = list(proposal.get("patches") or [])
    valid: list[dict[str, Any]] = []
    dropped: list[str] = []
    for index, patch in enumerate(patches):
        if not isinstance(patch, Mapping):
            dropped.append(f"patches[{index}] is not an object")
            continue
        patch = dict(patch)
        raw_path = patch.get("path")
        operation = str(patch.get("operation", "replace_text")).strip().lower()
        if operation in {"insert_after", "insert_before"} and not patch.get("anchor"):
            legacy_anchor = patch.get("old", patch.get("old_text"))
            if isinstance(legacy_anchor, str) and legacy_anchor:
                patch["anchor"] = legacy_anchor
        needle = patch.get("anchor") if operation in {"insert_after", "insert_before"} else patch.get("old", patch.get("old_text", patch.get("anchor")))
        if not isinstance(raw_path, str) or not raw_path.strip():
            dropped.append(f"patches[{index}].path must be a non-empty string")
            continue
        # 既有文件的文本操作必须携带其要编辑的确切源文本。
        # add/create 与统一 diff 使用各自的载荷。
        if operation not in {"add", "create", "unified_diff", "diff"} and (not isinstance(needle, str) or not needle):
            dropped.append(
                f"patches[{index}] {operation} requires a non-empty old/anchor string"
            )
            continue
        target = root / Path(raw_path)
        try:
            if target.is_file():
                try:
                    text = target.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    valid.append(dict(patch))
                    continue
                if needle not in text:
                    raise ComponentModeError(
                        f"patches[{index}] anchor is absent from {raw_path}; use an exact anchor from Current component files"
                    )
            elif target.is_dir():
                matches: list[Path] = []
                for item in target.rglob("*"):
                    if not item.is_file() or item.is_symlink():
                        continue
                    if any(part in {"node_modules", ".git", "__pycache__"} for part in item.relative_to(target).parts):
                        continue
                    try:
                        if needle in item.read_text(encoding="utf-8"):
                            matches.append(item)
                    except (OSError, UnicodeDecodeError):
                        continue
                if not matches:
                    raise ComponentModeError(
                        f"patches[{index}] anchor is absent from component directory {raw_path}; "
                        "use an exact anchor from Current component files"
                    )
                if len(matches) > 1:
                    raise ComponentModeError(
                        f"patches[{index}] anchor is ambiguous in {raw_path}; provide the actual file path"
                    )
            valid.append(dict(patch))
        except ComponentModeError as exc:
            dropped.append(str(exc))
    if dropped:
        if not valid:
            raise ComponentModeError("; ".join(dropped))
        repaired = dict(proposal)
        repaired["patches"] = valid
        repaired["repair_notes"] = {
            **(repaired.get("repair_notes") or {}),
            "dropped_invalid_patches": dropped,
        }
        return repaired
    return proposal


FAILED_PATCHES_FILE = os.environ.get(
    "AHE_FAILED_PATCHES_FILE", "failed-patches.jsonl")


def _load_persistent_failures() -> list[str]:
    """之前运行中失败过的 patch 的单行摘要。"""
    try:
        out: list[str] = []
        with open(FAILED_PATCHES_FILE, encoding="utf-8") as fh:
            for raw in fh:
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                old_txt = str(rec.get("old", ""))[:150].replace("\n", " ")
                new_txt = str(rec.get("new", ""))[:150].replace("\n", " ")
                out.append(
                    f"- [prior run {str(rec.get('run_id', '?'))[-16:]} r{rec.get('round', '?')}] "
                    f"{rec.get('summary', 'edit')}: old={old_txt!r} -> new={new_txt!r} "
                    f"| outcome: {rec.get('outcome', 'failed')}")
        return out
    except OSError:
        return []


def _record_persistent_failure(record: dict) -> None:
    try:
        from datetime import datetime as _dt, timezone as _tz
        record = {"ts": _dt.now(_tz.utc).isoformat(), **record}
        with open(FAILED_PATCHES_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass

