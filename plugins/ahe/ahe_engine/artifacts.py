"""将完成的 AHE 运行报告发布为可提交进 Git 的 artifact bundle。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

def _publish_report_artifacts(
    report_path: Path,
    artifact_workspace: str | None,
    *,
    include_candidates: bool = False,
    commit: bool = False,
    push: bool = False,
    remote: str = "origin",
    branch: str | None = None,
    message: str | None = None,
    allowed_source_roots: Sequence[os.PathLike[str] | str] | None = None,
):
    """发布单份报告，artifact 持久化不作为硬依赖。

    ``artifact_workspace`` 是 Git checkout 内的目标目录（例如
    ``<repo>/artifacts/autoharness``），不要求已经存在。bridge 会定位
    最近的 Git 根并推导仓库相对输出路径。发布失败只回报调用方，不会
    使一次已完成的 AHE 运行失效。

    ``allowed_source_roots`` 限定被复制证据的源目录；调用方只有在
    report 属于可信的本次运行产物时才显式传入（组件模式入口即如此），
    否则保持默认（只信任报告所在目录）。
    """
    if not artifact_workspace:
        return None
    try:
        from artifact_bridge import git_commit_artifacts, publish_run_artifacts

        target = Path(artifact_workspace).expanduser().resolve()
        probe = target
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        git_result = subprocess.run(
            ["git", "-C", str(probe), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False,
        )
        if git_result.returncode != 0:
            raise RuntimeError(f"artifact workspace is not inside a Git checkout: {target}")
        repo = Path(git_result.stdout.strip()).resolve()
        try:
            output_root = target.relative_to(repo).as_posix()
        except ValueError as exc:
            raise RuntimeError(f"artifact target escapes Git checkout: {target}") from exc
        if not output_root:
            output_root = "autoharness/runs"
        result = publish_run_artifacts(
            report_path,
            repo,
            output_root=output_root,
            include_candidates=include_candidates,
            allowed_source_roots=allowed_source_roots,
        )
        if commit:
            result["git"] = git_commit_artifacts(
                repo,
                result["destination"],
                message=message,
                push=push,
                remote=remote,
                branch=branch,
            )
        print(f"Artifact bundle: {result['destination']}")
        return result
    except Exception as exc:
        print(f"WARNING: artifact publish skipped: {exc}", file=sys.stderr)
        return {"error": str(exc), "report": str(report_path)}

