"""Git reads shared by settlement, Annotation, and LLM Verifier."""

import subprocess
import threading
from pathlib import Path
from typing import Any

from goal_plus.infrastructure.process_output import (
    OUTPUT_LIMIT_BYTES,
    TERM_GRACE_SECONDS,
    BoundedOutput,
    terminate_process_group,
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
        start_new_session=True,
    )
    stdout = BoundedOutput(max_bytes + 1)
    stderr = BoundedOutput(OUTPUT_LIMIT_BYTES)

    def drain(stream: Any, capture: BoundedOutput) -> None:
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    return
                capture.append(chunk)
        except (OSError, ValueError):
            pass

    readers = [
        threading.Thread(
            target=drain,
            args=(process.stdout, stdout),
            daemon=True,
        ),
        threading.Thread(
            target=drain,
            args=(process.stderr, stderr),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()
    returncode = process.wait()
    for reader in readers:
        reader.join(timeout=TERM_GRACE_SECONDS)
    if any(reader.is_alive() for reader in readers):
        terminate_process_group(process)
        for reader in readers:
            reader.join(timeout=TERM_GRACE_SECONDS)
    for stream in (process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            stream.close()
    if returncode:
        raise subprocess.CalledProcessError(
            returncode,
            command,
            output=stdout.text(),
            stderr=stderr.text(),
        )
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
