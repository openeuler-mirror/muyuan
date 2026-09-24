"""Run the offline recursive-submodule result verification."""

from __future__ import annotations

import json
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path

from goal_plus.domain.submodules import SubmoduleCheckError
from goal_plus.infrastructure.artifacts.submodules import inspect_submodules

PREPARE = runpy.run_path(str(Path(__file__).with_name("prepare_workspace.py")))
create_workspace = PREPARE["create_workspace"]


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        [
            "git",
            "-c", "protocol.file.allow=always",
            "-c", "user.name=goal-plus-e2e",
            "-c", "user.email=e2e@example.invalid",
            *args,
        ],
        cwd=root,
        text=True,
    ).strip()


def run_verifier(candidate: Path) -> dict[str, int]:
    output = subprocess.check_output(
        [sys.executable, str(candidate / "verifier.py")], cwd=candidate, text=True,
    )
    result = json.loads(output)
    if result != {"combined_score": 22}:
        raise AssertionError(f"unexpected exact verifier result: {result}")
    return result


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="goal-plus-submodule-e2e-") as directory:
        root = Path(directory)
        source = create_workspace(root / "source")
        source_root = Path(source["parent"])
        candidate = root / "candidate"
        git(root, "clone", "--no-local", "--recursive", str(source_root), str(candidate))
        initial = inspect_submodules(candidate)
        if initial.parent_head != source["P0"]:
            raise AssertionError("candidate did not start from P0")

        dirty_child = candidate / "modules/repo-a"
        (dirty_child / "value.py").write_text("VALUE = 2\n", encoding="utf-8")
        try:
            inspect_submodules(candidate)
        except SubmoduleCheckError as error:
            dirty_failure = error.failure_class
        else:
            raise AssertionError("dirty child was accepted")
        git(dirty_child, "add", "value.py")
        git(dirty_child, "commit", "-qm", "candidate A1")
        a1 = git(dirty_child, "rev-parse", "HEAD")

        try:
            inspect_submodules(candidate)
        except SubmoduleCheckError as error:
            mismatch_failure = error.failure_class
        else:
            raise AssertionError("stale parent gitlink was accepted")

        repo_b = candidate / "modules/repo-b"
        (repo_b / "value.py").write_text("VALUE = 20\n", encoding="utf-8")
        git(repo_b, "add", "value.py")
        git(repo_b, "commit", "-qm", "candidate B1")
        b1 = git(repo_b, "rev-parse", "HEAD")
        git(candidate, "add", "modules/repo-a", "modules/repo-b")
        git(candidate, "commit", "-qm", "candidate P1")
        p1 = git(candidate, "rev-parse", "HEAD")
        final = inspect_submodules(candidate)
        if final.parent_head != p1:
            raise AssertionError("parent checkpoint did not produce P1")
        result = run_verifier(candidate)
        payload = {
            "parent_commit": p1,
            "children": {
                "modules/repo-a": a1,
                "modules/repo-a/modules/plugin": source["AP0"],
                "modules/repo-b": b1,
            },
            "topology_digest": final.topology_digest,
            "verifier": result,
            "failure_assertions": {
                "dirty_child": dirty_failure,
                "head_mismatch": mismatch_failure,
            },
        }
        print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
