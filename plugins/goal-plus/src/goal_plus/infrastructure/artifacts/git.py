"""Git reads shared by settlement, Annotation, and LLM Verifier."""

import subprocess
from pathlib import Path

from goal_plus.infrastructure.process_output import (
    BoundedOutput,
    capture_process,
)
from goal_plus.workspaces import IGNORED_NAMES, IGNORED_SUFFIXES

RESULTS_TSV_RELATIVE_PATH = "results.tsv"


def candidate_git_pathspecs() -> list[str]:
    exclusions = [f":(exclude){name}/**" for name in sorted(IGNORED_NAMES - {".git"})]
    exclusions.extend(
        f":(exclude)**/{name}/**" for name in sorted(IGNORED_NAMES - {".git"})
    )
    exclusions.extend(f":(exclude)**/*{suffix}" for suffix in sorted(IGNORED_SUFFIXES))
    return [".", f":(exclude){RESULTS_TSV_RELATIVE_PATH}", *exclusions]


def git_changed_files(
    workspace: Path,
    base: str,
    head: str,
) -> list[str]:
    value = git_output(
        workspace,
        [
            "git",
            "diff",
            "--name-only",
            "-z",
            base,
            head,
            "--",
            *candidate_git_pathspecs(),
        ],
    )
    return sorted(path for path in value.split("\0") if path)


def git_output(workspace: Path, command: list[str]) -> str:
    process = subprocess.Popen(
        command,
        cwd=workspace,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = process.communicate()
    if process.returncode:
        raise subprocess.CalledProcessError(
            process.returncode,
            command,
            output=stdout,
            stderr=stderr,
        )
    return stdout


def git_output_bounded(
    workspace: Path,
    command: list[str],
    *,
    max_bytes: int,
) -> str:
    process = subprocess.Popen(
        command,
        cwd=workspace,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout = BoundedOutput(max_bytes + 1)
    completed = capture_process(process, stdout=stdout)
    completed.check_returncode()
    if stdout.truncated or len(stdout.data) > max_bytes:
        raise RuntimeError(f"annotation diff exceeds {max_bytes} bytes")
    return bytes(stdout.data).decode("utf-8", errors="replace")


def git_returncode(workspace: Path, command: list[str]) -> int:
    process = subprocess.Popen(
        command,
        cwd=workspace,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    process.communicate()
    return process.returncode
