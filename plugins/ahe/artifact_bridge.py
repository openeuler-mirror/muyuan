#!/usr/bin/env python3
"""把 AHE 运行产物发布进 Git workspace。

optimizer 刻意把 trace 写在 harness 旁的本地临时目录。本 bridge
把一份完成的报告转换为可提交进 GitCode 等仓库的可移植 artifact
bundle。

它刻意独立于 optimizer 循环。运行后调用 ``publish_run_artifacts``，
或使用命令行::

    python3 artifact_bridge.py --report /tmp/traces/ahe-component-report.json \\
      --workspace /path/to/target-repo --commit

发布是复制操作，源 harness 与候选 workspace 绝不被修改。
Git commit/push 需显式开启，使 evaluator 能在不可信或只读
checkout 上运行而不产生意外副作用。

安全边界：

- 被复制的证据（``trace_dir`` / ``workspace_dir``）默认必须位于
  报告自身目录（即本次运行的 trace 根）之内；跨目录证据需要
  显式 ``--allow-source-root`` 信任，避免本机无关文件进入随后
  被 commit/push 的 artifact。
- commit 采用路径限定提交（``git commit -- <artifact 目录>``），
  只提交 artifact 路径；调用方预先暂存的其他修改绝不会进入
  本次提交，也不会随 ``--push`` 被推送。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
DEFAULT_OUTPUT_ROOT = "autoharness/runs"
DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024
_METRIC_KEYS = (
    "accuracy",
    "f1",
    "tokens",
    "time_s",
    "tool_calls",
    "turns",
    "retries",
    "cost",
)
_METRIC_ALIASES = {
    "pass_rate": "accuracy",
    "pass_at_1": "accuracy",
    "score": "accuracy",
    "f1_score": "f1",
    "f1score": "f1",
    "token_count": "tokens",
    "total_tokens": "tokens",
    "duration_s": "time_s",
    "time": "time_s",
    "elapsed_s": "time_s",
}


class ArtifactBridgeError(ValueError):
    """artifact bundle 无法被安全发布时抛出。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _safe_name(value: Any, *, fallback: str = "run") -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip(".-")
    if not text or text in {".", ".."}:
        return fallback
    return text[:96]


def _run_id(report: Mapping[str, Any], requested: str | None = None) -> str:
    if requested:
        return _safe_name(requested, fallback="run")
    explicit = report.get("run_id") or report.get("id")
    if explicit:
        return _safe_name(explicit, fallback="run")
    timestamp = report.get("timestamp") or report.get("created_at") or _utc_now()
    stamp = _safe_name(timestamp, fallback="run")
    return f"{stamp}-{uuid.uuid4().hex[:10]}"


def _number(value: Any) -> Any:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return value
        return int(number) if number.is_integer() else number
    return value


def _metrics(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    source = {str(key): value for key, value in raw.items()}
    result: dict[str, Any] = {}
    for key in _METRIC_KEYS:
        if key in source:
            result[key] = _number(source[key])
    for alias, canonical in _METRIC_ALIASES.items():
        if canonical not in result and alias in source:
            result[canonical] = _number(source[alias])
    return result


def _difference(final: Mapping[str, Any], baseline: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in _METRIC_KEYS:
        before, after = baseline.get(key), final.get(key)
        if isinstance(before, (int, float)) and not isinstance(before, bool) and isinstance(after, (int, float)) and not isinstance(after, bool):
            delta = after - before
            result[f"{key}_delta"] = round(delta, 10) if isinstance(delta, float) else delta
    if "tokens_delta" in result:
        result["tokens_saved"] = -result["tokens_delta"]
    if "time_s_delta" in result:
        result["time_saved_s"] = -result["time_s_delta"]
    return result


def _git_root(workspace: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ArtifactBridgeError(f"workspace is not a Git checkout: {workspace}")
    return Path(result.stdout.strip()).resolve()


def _git_metadata(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str | None:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
        value = result.stdout.strip()
        return value if result.returncode == 0 and value else None

    return {
        "root": str(root),
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "remote": run("remote", "get-url", "origin"),
        "dirty": run("status", "--porcelain") is not None,
    }


def _copy_file(source: Path, destination: Path, max_file_bytes: int) -> dict[str, Any] | None:
    if not source.exists() or not source.is_file() or source.is_symlink():
        return None
    size = source.stat().st_size
    if size > max_file_bytes:
        return {"source": str(source), "skipped": "file_too_large", "bytes": size}
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    return {"source": str(source), "path": str(destination), "bytes": size, "sha256": digest}


def _copy_tree(source: Path, destination: Path, max_file_bytes: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if source.is_file():
        record = _copy_file(source, destination, max_file_bytes)
        if record:
            records.append(record)
        return records
    if not source.is_dir() or source.is_symlink():
        return records
    for item in sorted(source.rglob("*")):
        if not item.is_file() or item.is_symlink():
            continue
        record = _copy_file(item, destination / item.relative_to(source), max_file_bytes)
        if record:
            records.append(record)
    return records


def _referenced_path(
    report_path: Path,
    value: Any,
    allowed_roots: Sequence[Path],
) -> Path | None:
    """解析 report 引用的证据路径，并把源范围限制在可信根内。

    ``trace_dir`` / ``workspace_dir`` 来自 report；report 可能是
    本次运行之外的文件，绝对路径会把本机任意可读文件复制进随后
    被 commit/push 的 artifact。允许的源根是报告所在目录（即 AHE
    运行的 trace 根）加上调用方显式传入的 ``allowed_roots``。
    越界且真实存在的路径会被拒绝；不存在的引用保持旧行为（跳过）。
    """
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = report_path.parent / candidate
    candidate = candidate.resolve(strict=False)
    if candidate.exists() and not any(_under(candidate, root) for root in allowed_roots):
        roots = ", ".join(str(root) for root in allowed_roots)
        raise ArtifactBridgeError(
            f"refusing to copy evidence from outside allowed source roots: {candidate}; "
            f"allowed roots: {roots}; extend with allowed_source_roots "
            "(CLI: --allow-source-root) to trust additional directories"
        )
    return candidate


def _history(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = report.get("change_manifest")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, Mapping)]
    raw = report.get("history")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, Mapping) and item.get("round", 0)]
    return []


def publish_run_artifacts(
    report_path: os.PathLike[str] | str,
    workspace: os.PathLike[str] | str,
    *,
    output_root: str = DEFAULT_OUTPUT_ROOT,
    run_id: str | None = None,
    include_candidates: bool = False,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    allow_existing: bool = False,
    allowed_source_roots: Iterable[os.PathLike[str] | str] | None = None,
) -> dict[str, Any]:
    """把一份 AHE 报告及其引用证据复制进 Git workspace。

    报告本身仍是事实来源。生成的摘要刻意只做增量，并保持显式
    ``f1`` 与 accuracy 分离；evaluator 未输出 F1 时该字段省略，
    而不是默默把 accuracy 当作 F1。

    ``allowed_source_roots`` 限定 ``trace_dir`` / ``workspace_dir``
    等引用证据的源目录。报告所在目录（即 AHE 运行的 trace 根，
    亦是调用方显式指定的输入）始终被信任；显式根在其基础上追加，
    空输入等价于默认。
    """
    source_report = Path(report_path).expanduser().resolve()
    if not source_report.is_file() or source_report.is_symlink():
        raise ArtifactBridgeError(f"report does not exist or is a symlink: {report_path}")
    source_roots = (
        source_report.parent,
        *(Path(root).expanduser().resolve() for root in (allowed_source_roots or ())),
    )
    try:
        report = json.loads(source_report.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArtifactBridgeError(f"unable to read report {source_report}: {exc}") from exc
    if not isinstance(report, Mapping):
        raise ArtifactBridgeError("AHE report must be a JSON object")
    if max_file_bytes <= 0:
        raise ArtifactBridgeError("max_file_bytes must be greater than zero")
    # 引用证据先于任何写入校验：被拒绝的发布不得在仓库里留下
    # 半成品 run 目录，否则 run id 被烧掉且无法重试。
    trace_dir = _referenced_path(source_report, report.get("trace_dir"), source_roots)
    candidate_dir = (
        _referenced_path(source_report, report.get("workspace_dir"), source_roots)
        if include_candidates else None
    )

    repo = _git_root(Path(workspace).expanduser().resolve())
    relative_root = Path(output_root)
    if relative_root.is_absolute() or any(part in {"", ".", ".."} for part in relative_root.parts):
        raise ArtifactBridgeError("output_root must be a safe relative path")
    selected_run_id = _run_id(report, run_id)
    destination = (repo / relative_root / selected_run_id).resolve()
    if not _under(destination, repo):
        raise ArtifactBridgeError("artifact destination escapes Git workspace")
    if destination.exists() and not allow_existing:
        raise ArtifactBridgeError(f"artifact run already exists: {destination}")
    destination.mkdir(parents=True, exist_ok=True)

    files: list[dict[str, Any]] = []
    report_copy = _copy_file(source_report, destination / "report.json", max_file_bytes)
    if report_copy:
        files.append(report_copy)

    baseline = _metrics(report.get("baseline"))
    final = _metrics(report.get("final"))
    summary = {
        "schema_version": SCHEMA_VERSION,
        "run_id": selected_run_id,
        "published_at": _utc_now(),
        "source_report": str(source_report),
        "mode": report.get("mode", "legacy"),
        "harness": report.get("harness"),
        "manifest": report.get("manifest"),
        "task": report.get("task"),
        "rounds": report.get("rounds"),
        "baseline": baseline,
        "final": final,
        "delta": _difference(final, baseline),
        "change_count": len(_history(report)),
    }
    _write_json(destination / "summary.json", summary)
    _write_json(destination / "metadata.json", {"git": _git_metadata(repo), "bridge": "ahe-artifact-bridge", "schema_version": SCHEMA_VERSION})

    history = _history(report)
    if history:
        _write_json(destination / "changes.json", history)
        for index, change in enumerate(history, 1):
            try:
                round_number = int(change.get("round", index))
            except (TypeError, ValueError):
                round_number = index
            _write_json(destination / "changes" / f"round-{round_number:03d}.json", change)

    if trace_dir and trace_dir.exists():
        files.extend(_copy_tree(trace_dir, destination / "trace", max_file_bytes))
    if candidate_dir and candidate_dir.exists():
        files.extend(_copy_tree(candidate_dir, destination / "candidates", max_file_bytes))
    _write_json(destination / "files.json", files)
    return {**summary, "destination": str(destination), "files": files}


def _unrelated_staged_paths(repo: Path, relative: str) -> list[str]:
    """列出暂存区中位于 artifact 目录之外的路径。

    ``git diff --cached --name-only`` 在没有 HEAD 的全新仓库同样
    可用（对比空树）。失败时返回空列表，不阻塞发布流程。
    """
    result = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--name-only", "-z"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    prefix = f"{relative}/"
    return [
        path
        for path in result.stdout.split("\0")
        if path and path != relative and not path.startswith(prefix)
    ]


def git_commit_artifacts(
    workspace: os.PathLike[str] | str,
    destination: os.PathLike[str] | str,
    *,
    message: str | None = None,
    push: bool = False,
    remote: str = "origin",
    branch: str | None = None,
) -> dict[str, Any]:
    """暂存并可选地 commit/push 一个 artifact 目录。

    提交被限定在 artifact 路径内（``git commit -- <dir>``），因此
    调用方预先暂存的其他修改绝不会进入本次提交，也不会被推送；
    它们原样保留在暂存区。
    """
    repo = _git_root(Path(workspace).expanduser().resolve())
    target = Path(destination).expanduser().resolve()
    if not _under(target, repo) or not target.is_dir():
        raise ArtifactBridgeError("destination must be a directory inside the Git workspace")
    relative = target.relative_to(repo).as_posix()
    add = subprocess.run(["git", "-C", str(repo), "add", "--", relative], capture_output=True, text=True, check=False)
    if add.returncode != 0:
        raise ArtifactBridgeError(f"git add failed: {add.stderr.strip()}")
    staged = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet", "--", relative], check=False)
    if staged.returncode == 0:
        return {"committed": False, "pushed": False, "destination": str(target)}
    unrelated = _unrelated_staged_paths(repo, relative)
    if unrelated:
        print(
            f"note: leaving {len(unrelated)} unrelated staged path(s) out of this "
            f"artifact commit (e.g. {unrelated[0]})",
            file=sys.stderr,
        )
    commit_message = message or f"chore(ahe): publish run artifacts {target.name}"
    commit = subprocess.run(["git", "-C", str(repo), "commit", "-m", commit_message, "--", relative], capture_output=True, text=True, check=False)
    if commit.returncode != 0:
        raise ArtifactBridgeError(f"git commit failed: {commit.stderr.strip() or commit.stdout.strip()}")
    commit_sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=False).stdout.strip()
    pushed = False
    if push:
        ref = f"HEAD:{branch}" if branch else "HEAD"
        pushed_result = subprocess.run(["git", "-C", str(repo), "push", remote, ref], capture_output=True, text=True, check=False)
        if pushed_result.returncode != 0:
            raise ArtifactBridgeError(f"git push failed: {pushed_result.stderr.strip() or pushed_result.stdout.strip()}")
        pushed = True
    return {"committed": True, "pushed": pushed, "commit": commit_sha, "destination": str(target)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish AHE run artifacts into a Git workspace")
    parser.add_argument("--report", required=True, help="AHE report JSON path")
    parser.add_argument("--workspace", required=True, help="Git workspace checkout")
    parser.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT, help="relative artifact root (default: %(default)s)")
    parser.add_argument("--run-id", help="stable run id; defaults to report id/timestamp plus nonce")
    parser.add_argument("--include-candidates", action="store_true", help="copy candidate workspace files too")
    parser.add_argument("--allow-source-root", action="append", metavar="PATH",
                        help="trust an extra source root for referenced evidence (repeatable); "
                             "default: only the report's own directory")
    parser.add_argument("--allow-existing", action="store_true", help="reuse an existing run directory")
    parser.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES)
    parser.add_argument("--commit", action="store_true", help="stage and commit the generated artifact directory")
    parser.add_argument("--push", action="store_true", help="push the commit; requires --commit")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch")
    parser.add_argument("--message")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    if args.push and not args.commit:
        _parser().error("--push requires --commit")
    try:
        result = publish_run_artifacts(
            args.report,
            args.workspace,
            output_root=args.output_root,
            run_id=args.run_id,
            include_candidates=args.include_candidates,
            max_file_bytes=args.max_file_bytes,
            allow_existing=args.allow_existing,
            allowed_source_roots=args.allow_source_root or None,
        )
        if args.commit:
            result["git"] = git_commit_artifacts(
                args.workspace,
                result["destination"],
                message=args.message,
                push=args.push,
                remote=args.remote,
                branch=args.branch,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except ArtifactBridgeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ArtifactBridgeError", "git_commit_artifacts", "main", "publish_run_artifacts"]
