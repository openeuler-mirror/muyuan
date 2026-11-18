"""为完成的 AHE 组件 campaign 生成可审阅 patch 报告。

campaign 结束后，本模块把被保留的轮次重建为 GitCode 可浏览的
目录，结构与 harness/patches/... 一致：

    <target>/
    README.md                      campaign 总览 + 轮次表
    01-round-1-3a2e2365.patch      累计 diff，基线 -> round 1
    01-round-1-3a2e2365.md         round 1 的改了什么/为什么/观测
    02-round-3-9f6c484d.patch      累计 diff，基线 -> round 3
    ...

每个被保留候选本身就是基线 + 此前全部保留 patch + 本轮新增，
因此每个 .patch 都是从冻结基线出发的自包含累计 diff：
复现某一轮只需应用一个 patch。
"""
from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

DIFF_IGNORE_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".ahe-candidates",
    ".ahe-traces", ".idea", ".vscode",
})
DIFF_IGNORE_SUFFIXES = frozenset({".pyc", ".pyo", ".pyd", ".so", ".dll", ".exe"})


def _safe_name(value: str) -> str:
    keep = [ch if (ch.isalnum() or ch in "-_.") else "-" for ch in value.strip()]
    return "".join(keep).strip("-") or "campaign"


def _iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        parts = [p.lower() for p in path.relative_to(root).parts]
        if any(part in DIFF_IGNORE_DIRS for part in parts[:-1]):
            continue
        if path.suffix.lower() in DIFF_IGNORE_SUFFIXES:
            continue
        yield path


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        return []


def cumulative_diff(baseline: Path, candidate: Path) -> tuple[str, list[str]]:
    """两个 harness 快照之间的统一 git 风格 diff。

    返回 diff 文本与变更相对路径列表。未变更文件不产生输出；
    新增与删除文件使用标准 git 头约定，使 patch 在 GitCode 中
    保持可审阅。
    """
    baseline_files = {p.relative_to(baseline).as_posix(): p for p in _iter_files(baseline)}
    candidate_files = {p.relative_to(candidate).as_posix(): p for p in _iter_files(candidate)}
    chunks: list[str] = []
    changed: list[str] = []
    for rel in sorted(set(baseline_files) | set(candidate_files)):
        old = baseline_files.get(rel)
        new = candidate_files.get(rel)
        if old is not None and new is not None:
            if _file_digest(old) == _file_digest(new):
                continue
            old_lines = _read_lines(old)
            new_lines = _read_lines(new)
            body = difflib.unified_diff(old_lines, new_lines,
                                        fromfile=f"a/{rel}", tofile=f"b/{rel}")
        elif old is not None:
            old_lines = _read_lines(old)
            body = difflib.unified_diff(old_lines, [], fromfile=f"a/{rel}", tofile=f"/dev/null")
        else:
            new_lines = _read_lines(new)
            body = difflib.unified_diff([], new_lines, fromfile="/dev/null", tofile=f"b/{rel}")
        chunk = list(body)
        if not chunk:
            continue
        header = [f"diff --git a/{rel} b/{rel}\n"]
        if old is None:
            header.append("new file mode 100644\n")
        if new is None:
            header.append("deleted file mode 100644\n")
        chunks.extend(header + chunk)
        changed.append(rel)
    if chunks and not chunks[-1].endswith("\n"):
        chunks[-1] += "\n"
    return "".join(chunks), changed


def _metrics_line(metrics: Any) -> str:
    if not isinstance(metrics, Mapping):
        return "无"
    keys = ("score", "accuracy", "f1", "kept_iterations",
            "iterations", "tokens", "time_s")
    bits = [f"{k}={metrics[k]}" for k in keys if metrics.get(k) is not None]
    return ", ".join(bits) if bits else "无"


def _mechanism_summary(proposal: Mapping[str, Any] | None, changed: list[str]) -> str:
    if isinstance(proposal, Mapping):
        reason = str(proposal.get("reason") or "").strip()
        if reason:
            first = reason.splitlines()[0]
            return first if len(first) <= 90 else first[:87] + "..."
    targets = []
    if isinstance(proposal, Mapping):
        for patch in proposal.get("patches") or []:
            name = str(patch.get("component_id") or patch.get("path") or "").strip()
            if name and name not in targets:
                targets.append(name)
    if targets:
        return "改 " + ", ".join(targets[:4]) + (" ..." if len(targets) > 4 else "")
    if changed:
        names = [Path(rel).name for rel in changed[:4]]
        return "改 " + ", ".join(names) + (" ..." if len(changed) > 4 else "")
    return "机制级修改"


def _round_markdown(index: int, candidate_id: str, round_number: Any,
                    proposal: Mapping[str, Any] | None, changed: list[str],
                    metrics: Any, baseline_metrics: Any,
                    baseline_dir: Path, candidate_dir: Path,
                    patch_name: str) -> str:
    summary = _mechanism_summary(proposal, changed)
    lines = [
        f"# Round {round_number}：{summary}",
        "",
        "## Patch",
        "",
        f"- [累计 patch：{patch_name}]({patch_name})",
        f"- 候选快照：{candidate_dir}",
        f"- 基线：{baseline_dir}",
        "",
        "## 改了什么",
        "",
    ]
    if changed:
        lines.append("相对基线发生变化的文件（本轮 patch 的累计结果）：")
        lines.append("")
        for rel in changed:
            lines.append(f"- `{rel}`")
    else:
        lines.append("（相对基线无文本变化）")
    lines.extend(["", "## 为了什么", ""])
    reason = ""
    if isinstance(proposal, Mapping):
        reason = str(proposal.get("reason") or "").strip()
    if not reason and isinstance(proposal, Mapping):
        patch_reasons = [str(p.get("reason", "")).strip()
                         for p in (proposal.get("patches") or []) if p.get("reason")]
        reason = "; ".join(dict.fromkeys(patch_reasons))
    lines.append(reason or "（proposal 未记录 reason）")
    lines.extend(["", "## 观测结果", ""])
    lines.append(f"- 本轮指标：{_metrics_line(metrics)}")
    lines.append(f"- 基线指标：{_metrics_line(baseline_metrics)}")
    lines.append("")
    return "\n".join(lines)


def export_patch_report(report_path: Path, target: str | None = None) -> Path | None:
    """从组件报告构建 harness/patches 风格的报告目录。"""
    try:
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot read report {report_path}: {exc}")
        return None
    if report.get("mode") != "component":
        print("ERROR: not a component-mode report")
        return None
    baseline_dir = Path(str(report.get("baseline_candidate") or "")).expanduser()
    if not baseline_dir.is_dir():
        print(f"ERROR: baseline candidate is missing on disk: {baseline_dir}")
        return None
    campaign = _safe_name(str(report_path.parent.name))
    target_path = (Path(target).expanduser().resolve() if target
                   else report_path.parent / "patch-report")
    if target_path.exists() and any(target_path.iterdir()):
        print(f"ERROR: patch report target is not empty: {target_path}")
        return None
    target_path.mkdir(parents=True, exist_ok=True)

    history = report.get("history") or []
    rows: list[dict[str, Any]] = []
    index = 0
    for entry in history:
        if not isinstance(entry, Mapping) or entry.get("action") != "keep":
            continue
        candidate_dir = Path(str(entry.get("candidate") or "")).expanduser()
        if not candidate_dir.is_dir():
            continue
        index += 1
        candidate_id = candidate_dir.name
        patch_name = f"{index:02d}-{candidate_id}.patch"
        diff_text, changed = cumulative_diff(baseline_dir, candidate_dir)
        (target_path / patch_name).write_text(diff_text, encoding="utf-8", newline="\n")
        md_name = f"{index:02d}-{candidate_id}.md"
        (target_path / md_name).write_text(
            _round_markdown(index, candidate_id, entry.get("round"),
                            entry.get("proposal"), changed,
                            entry.get("metrics"), report.get("baseline"),
                            baseline_dir, candidate_dir, patch_name),
            encoding="utf-8", newline="\n")
        rows.append({
            "index": index, "round": entry.get("round"), "candidate_id": candidate_id,
            "md": md_name, "patch": patch_name, "changed": changed,
            "proposal": entry.get("proposal"), "metrics": entry.get("metrics"),
        })

    baseline_metrics = report.get("baseline")
    final_metrics = report.get("final")
    lines = [
        f"# {campaign} patches",
        "",
        "这组 patch 来自一次 AHE component campaign 的 kept 轮次：",
        "",
        f"- harness：{report.get('harness')}",
        f"- manifest：{report.get('manifest')}",
        f"- trace：{report.get('trace_dir')}",
        f"- 报告：{report_path}",
        f"- 基线快照：{baseline_dir}",
        f"- 最终候选：{report.get('accepted_candidate')}",
        "",
        "## 使用方式",
        "",
        "每个 patch 都是从同一冻结基线到该轮候选快照的累计 diff（候选 = 基线 + 此前",
        "全部 keep + 本轮新增），不能把 01 到 NN 依次连续应用。需要复现某一轮时，",
        "只选择对应的一个 patch。",
        "",
        "## 汇总",
        "",
        f"- 基线指标：{_metrics_line(baseline_metrics)}",
        f"- 最终指标：{_metrics_line(final_metrics)}",
        f"- kept 轮数：{len(rows)}",
        "",
    ]
    if rows:
        lines.extend([
            "## 轮次",
            "",
            "| 轮次 | 这轮新增的机制 | 改动文件 | 相对基线指标 |",
            "|---|---|---|---|",
        ])
        for row in rows:
            summary = _mechanism_summary(row["proposal"], row["changed"])
            changed = ", ".join(f"`{rel}`" for rel in row["changed"][:3])
            if len(row["changed"]) > 3:
                changed += " ..."
            lines.append(
                f"| [Round {row['round']}]({row['md']}) | {summary} "
                f"| {changed or '无'} | {_metrics_line(row['metrics'])} |")
        lines.append("")
    else:
        lines.extend([
            "## 轮次",
            "",
            "本次 campaign 没有 keep 任何候选（全部 revert），因此没有可归档的 patch。",
            "",
        ])
    (target_path / "README.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"patch report written: {target_path} ({len(rows)} kept rounds)")
    return target_path


__all__ = ["cumulative_diff", "export_patch_report"]
