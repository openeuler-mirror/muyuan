from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

STATE_SCHEMA_VERSION = 1
STATE_RELATIVE_PATH = Path(".git/goal-plus/state.json")
RESULTS_TSV_RELATIVE_PATH = Path("results.tsv")
PRIVATE_DIRECTORY_NAMES = (".git", ".gp", ".search", ".tmp")


class InnerGitError(RuntimeError):
    pass


def _workspace(value: str) -> Path:
    relative = PurePosixPath(value or ".")
    if relative.is_absolute() or ".." in relative.parts:
        raise InnerGitError("inner Git workspace must be a safe relative path")
    root = Path.cwd().resolve()
    workspace = (root / Path(*relative.parts)).resolve()
    try:
        workspace.relative_to(root)
    except ValueError as exc:
        raise InnerGitError("inner Git workspace escapes the Child root") from exc
    if not workspace.is_dir():
        raise InnerGitError(f"inner Git workspace does not exist: {value}")
    return workspace


def _git(workspace: Path, *args: str, output: bool = False) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise InnerGitError(
            f"inner Git command failed: git {' '.join(args)}: {detail.strip()}"
        ) from exc
    return completed.stdout.strip() if output else ""


def _state_path(workspace: Path) -> Path:
    return workspace / STATE_RELATIVE_PATH


def _load_state(workspace: Path) -> dict[str, Any]:
    path = _state_path(workspace)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InnerGitError("inner Git state is missing or invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise InnerGitError("inner Git state has an unsupported schema")
    return payload


def _write_state(workspace: Path, state: dict[str, Any]) -> None:
    path = _state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _results_text(workspace: Path) -> str:
    try:
        return (workspace / RESULTS_TSV_RELATIVE_PATH).read_text(encoding="utf-8")
    except OSError as exc:
        raise InnerGitError("results.tsv is missing from the inner Git workspace") from exc


def _validate_results_text(value: str, metric_name: str) -> int:
    lines = value.splitlines()
    expected_header = f"commit\t{_tsv_cell(metric_name) or 'metric'}\tstatus\thypothesis"
    if not lines or lines[0] != expected_header:
        raise InnerGitError("results.tsv header does not match the frozen metric")
    if any(len(line.split("\t")) != 4 for line in lines[1:]):
        raise InnerGitError("results.tsv contains a malformed row")
    return len(lines) - 1


def _assert_settled_base(
    workspace: Path,
    state: dict[str, Any],
    *,
    require_clean_product: bool,
) -> None:
    head = _git(workspace, "rev-parse", "HEAD", output=True)
    if head != state.get("ledger_git_head"):
        raise InnerGitError("inner Git HEAD does not match the settled ledger commit")
    results = _results_text(workspace)
    if _sha256_text(results) != state.get("results_sha256"):
        raise InnerGitError(
            "ResultsLedgerMutation: results.tsv changed outside the trusted helper"
        )
    row_count = _validate_results_text(results, str(state.get("metric_name") or ""))
    if row_count != state.get("result_count"):
        raise InnerGitError("inner Git result count does not match results.tsv")
    if state.get("pending_attempt") is not None:
        raise InnerGitError("inner Git has an unsettled attempt")
    if require_clean_product:
        status = _git(
            workspace,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            *_product_pathspecs(),
            output=True,
        )
        if status:
            raise InnerGitError("inner Git product tree is dirty after settlement")


def _commit(workspace: Path, message: str) -> str:
    _git(workspace, "commit", "--quiet", "--allow-empty", "-m", message)
    return _git(workspace, "rev-parse", "HEAD", output=True)


def _product_pathspecs() -> list[str]:
    pathspecs = [
        ".",
        ":(exclude,top)results.tsv",
        ":(exclude,glob)**/results.tsv",
    ]
    for name in PRIVATE_DIRECTORY_NAMES:
        pathspecs.extend(
            (
                f":(exclude,top){name}",
                f":(exclude,top){name}/**",
                f":(exclude,glob)**/{name}",
                f":(exclude,glob)**/{name}/**",
            )
        )
    return pathspecs


def _stage_product(workspace: Path) -> None:
    _git(workspace, "add", "-A", "-f", "--", *_product_pathspecs())


def _has_staged_changes(workspace: Path) -> bool:
    completed = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=workspace,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode not in {0, 1}:
        raise InnerGitError(
            "inner Git could not inspect staged restoration: "
            + completed.stderr.strip()
        )
    return completed.returncode == 1


def _tsv_cell(value: str) -> str:
    return " ".join(value.replace("\t", " ").split()).strip()


def _score_text(value: float | None) -> str:
    if value is None:
        return ""
    if not math.isfinite(value):
        raise InnerGitError("inner Git result score must be finite or null")
    if value.is_integer():
        return str(int(value))
    return str(value)


def initialize(
    workspace: Path,
    *,
    metric_name: str,
    initial_results: str,
) -> dict[str, Any]:
    normalized_metric = _tsv_cell(metric_name)
    if not normalized_metric:
        raise InnerGitError("inner Git requires a non-empty metric name")
    _validate_results_text(initial_results, normalized_metric)
    state_path = _state_path(workspace)
    if state_path.is_file():
        state = _load_state(workspace)
        if state.get("metric_name") != normalized_metric:
            raise InnerGitError("inner Git metric changed after initialization")
        _assert_settled_base(workspace, state, require_clean_product=False)
        return state

    git_path = workspace / ".git"
    if git_path.exists() or git_path.is_symlink():
        if git_path.is_dir() and not git_path.is_symlink():
            shutil.rmtree(git_path)
        else:
            git_path.unlink()
    _git(workspace, "init", "--quiet")
    _git(workspace, "config", "user.name", "Goal Plus AutoResearch")
    _git(workspace, "config", "user.email", "goal-plus@localhost")
    _stage_product(workspace)
    baseline_git_head = _commit(workspace, "goal-plus autoresearch baseline")
    results_path = workspace / RESULTS_TSV_RELATIVE_PATH
    results_path.write_text(initial_results, encoding="utf-8")
    _git(workspace, "add", "-f", "--", RESULTS_TSV_RELATIVE_PATH.as_posix())
    ledger_git_head = _commit(workspace, "goal-plus results ledger initialization")
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "adapter": "autoresearch-git",
        "metric_name": normalized_metric,
        "baseline_git_head": baseline_git_head,
        "initial_ledger_git_head": ledger_git_head,
        "ledger_git_head": ledger_git_head,
        "results_sha256": _sha256_text(initial_results),
        "result_count": _validate_results_text(initial_results, normalized_metric),
        "attempt_git_heads": {},
        "pending_attempt": None,
    }
    _write_state(workspace, state)
    _assert_settled_base(workspace, state, require_clean_product=True)
    return state


def prepare_attempt(
    workspace: Path,
    *,
    dispatch_nonce: str,
) -> dict[str, Any]:
    state = _load_state(workspace)
    pending = state.get("pending_attempt")
    if isinstance(pending, dict):
        if pending.get("dispatch_nonce") != dispatch_nonce:
            raise InnerGitError("inner Git already has an attempt for another dispatch")
        head = _git(workspace, "rev-parse", "HEAD", output=True)
        if head != pending.get("attempt_git_head"):
            raise InnerGitError("inner Git pending attempt HEAD changed")
        return {
            "protocol": "goal-plus.inner-git.v1",
            "adapter": "autoresearch-git",
            "attempt_git_head": head,
            "baseline_git_head": state["baseline_git_head"],
            "result_count_before": state["result_count"],
        }
    _assert_settled_base(workspace, state, require_clean_product=False)
    # A worker may stage files directly. Rebuild the index from the trusted
    # settled HEAD so results.tsv and private runtime paths cannot hitchhike.
    _git(workspace, "reset", "--mixed", "--quiet", "HEAD", "--")
    _stage_product(workspace)
    staged_results = _git(
        workspace,
        "diff",
        "--cached",
        "--name-only",
        "--",
        RESULTS_TSV_RELATIVE_PATH.as_posix(),
        output=True,
    )
    if staged_results:
        raise InnerGitError("ResultsLedgerMutation: results.tsv may not be staged by a worker")
    attempt_git_head = _commit(
        workspace,
        f"goal-plus autoresearch attempt {dispatch_nonce}",
    )
    state["pending_attempt"] = {
        "dispatch_nonce": dispatch_nonce,
        "attempt_git_head": attempt_git_head,
    }
    _write_state(workspace, state)
    return {
        "protocol": "goal-plus.inner-git.v1",
        "adapter": "autoresearch-git",
        "attempt_git_head": attempt_git_head,
        "baseline_git_head": state["baseline_git_head"],
        "result_count_before": state["result_count"],
    }


def settle_attempt(
    workspace: Path,
    *,
    attempt_git_head: str,
    iteration: int,
    metric_name: str,
    score: float | None,
    status: str,
    hypothesis: str,
    restore_git_head: str | None,
) -> dict[str, Any]:
    state = _load_state(workspace)
    if state.get("metric_name") != _tsv_cell(metric_name):
        raise InnerGitError("inner Git settlement metric does not match initialization")
    pending = state.get("pending_attempt")
    if not isinstance(pending, dict) or pending.get("attempt_git_head") != attempt_git_head:
        raise InnerGitError("inner Git settlement does not match the pending attempt")
    head = _git(workspace, "rev-parse", "HEAD", output=True)
    if head != attempt_git_head:
        raise InnerGitError("inner Git HEAD changed after attempt submission")
    preserved_results = _results_text(workspace)
    if _sha256_text(preserved_results) != state.get("results_sha256"):
        raise InnerGitError(
            "ResultsLedgerMutation: results.tsv changed after attempt submission"
        )
    if restore_git_head is not None:
        _git(workspace, "cat-file", "-e", f"{restore_git_head}^{{commit}}")
        _git(
            workspace,
            "restore",
            f"--source={restore_git_head}",
            "--staged",
            "--worktree",
            "--",
            ".",
        )
        results_path = workspace / RESULTS_TSV_RELATIVE_PATH
        results_path.write_text(preserved_results, encoding="utf-8")
        _git(workspace, "add", "-f", "--", RESULTS_TSV_RELATIVE_PATH.as_posix())
        if _has_staged_changes(workspace):
            _commit(
                workspace,
                f"goal-plus restore best before iteration {iteration}",
            )
    results_path = workspace / RESULTS_TSV_RELATIVE_PATH
    row = "\t".join(
        (
            attempt_git_head,
            _score_text(score),
            _tsv_cell(status) or "unknown",
            _tsv_cell(hypothesis),
        )
    )
    settled_results = preserved_results + row + "\n"
    if len(settled_results.splitlines()) != len(preserved_results.splitlines()) + 1:
        raise InnerGitError("inner Git settlement must append exactly one results.tsv row")
    results_path.write_text(settled_results, encoding="utf-8")
    _git(workspace, "add", "-f", "--", RESULTS_TSV_RELATIVE_PATH.as_posix())
    ledger_git_head = _commit(
        workspace,
        f"goal-plus results ledger iteration {iteration}",
    )
    attempt_heads = dict(state.get("attempt_git_heads") or {})
    attempt_heads[str(iteration)] = attempt_git_head
    state.update(
        {
            "ledger_git_head": ledger_git_head,
            "results_sha256": _sha256_text(settled_results),
            "result_count": int(state.get("result_count") or 0) + 1,
            "attempt_git_heads": attempt_heads,
            "pending_attempt": None,
        }
    )
    _write_state(workspace, state)
    _assert_settled_base(workspace, state, require_clean_product=True)
    return {
        "protocol": "goal-plus.inner-git.v1",
        "adapter": "autoresearch-git",
        "attempt_git_head": attempt_git_head,
        "ledger_git_head": ledger_git_head,
        "baseline_git_head": state["baseline_git_head"],
        "result_count": state["result_count"],
        "restored_to_git_head": restore_git_head,
    }


def _initial_results(value: str) -> str:
    try:
        return base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise InnerGitError("initial results.tsv payload is invalid") from exc


def _score(value: str) -> float | None:
    if value == "null":
        return None
    try:
        score = float(value)
    except ValueError as exc:
        raise InnerGitError("inner Git score must be numeric or null") from exc
    if not math.isfinite(score):
        raise InnerGitError("inner Git score must be finite")
    return score


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m goal_plus.inner_git")
    parser.add_argument("--workspace", default=".")
    subparsers = parser.add_subparsers(dest="action", required=True)
    initialize_parser = subparsers.add_parser("initialize")
    initialize_parser.add_argument("--metric-name", required=True)
    initialize_parser.add_argument("--initial-results-base64", required=True)
    prepare_parser = subparsers.add_parser("prepare-attempt")
    prepare_parser.add_argument("--dispatch-nonce", required=True)
    settle_parser = subparsers.add_parser("settle-attempt")
    settle_parser.add_argument("--attempt-git-head", required=True)
    settle_parser.add_argument("--iteration", required=True, type=int)
    settle_parser.add_argument("--metric-name", required=True)
    settle_parser.add_argument("--score", required=True)
    settle_parser.add_argument("--status", required=True)
    settle_parser.add_argument("--hypothesis", required=True)
    settle_parser.add_argument("--restore-git-head")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        workspace = _workspace(args.workspace)
        if args.action == "initialize":
            result = initialize(
                workspace,
                metric_name=args.metric_name,
                initial_results=_initial_results(args.initial_results_base64),
            )
        elif args.action == "prepare-attempt":
            result = prepare_attempt(workspace, dispatch_nonce=args.dispatch_nonce)
        else:
            result = settle_attempt(
                workspace,
                attempt_git_head=args.attempt_git_head,
                iteration=args.iteration,
                metric_name=args.metric_name,
                score=_score(args.score),
                status=args.status,
                hypothesis=args.hypothesis,
                restore_git_head=args.restore_git_head,
            )
    except InnerGitError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps({"ok": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
