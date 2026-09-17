from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import tempfile
from typing import Iterable, Iterator, Mapping

from goal_plus.workspaces import copy_source_tree


VERIFIER_TMPDIR_ENV = "GOAL_PLUS_VERIFIER_TMPDIR"
VERIFIER_DIAGNOSTICS_ENV = "GOAL_PLUS_VERIFIER_DIAGNOSTICS_DIR"


@dataclass(frozen=True)
class IsolatedVerifierWorkspace:
    live_workspace: Path
    workspace: Path
    temp_root: Path
    diagnostics_dir: Path | None


def sanitized_evaluator_environment(
    *,
    python_paths: Iterable[Path | str] = (),
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build an evaluator environment without the verifier's live PYTHONPATH.

    Goal Plus adds the verifier workspace to its process ``PYTHONPATH`` so
    verifier-local modules remain importable. A nested evaluator which builds
    and installs a Candidate package must not inherit that path, because the
    live source package can shadow the newly installed wheel.
    """
    env = dict(os.environ if base_environment is None else base_environment)
    explicit_paths = tuple(str(Path(path).resolve()) for path in python_paths)
    if explicit_paths:
        env["PYTHONPATH"] = os.pathsep.join(explicit_paths)
    else:
        env.pop("PYTHONPATH", None)
    return env


@contextmanager
def isolated_verifier_workspace(
    live_workspace: Path | str,
    *,
    required_paths: Iterable[Path | str] = (),
) -> Iterator[IsolatedVerifierWorkspace]:
    """Copy verifier inputs into invocation-local scratch before building.

    Runtime calls provide ``GOAL_PLUS_VERIFIER_TMPDIR``. Direct verifier
    self-tests receive an equivalent managed temporary directory.
    """
    live = Path(live_workspace).resolve()
    if not live.is_dir():
        raise ValueError(f"verifier workspace is not a directory: {live}")

    provided_temp = os.environ.get(VERIFIER_TMPDIR_ENV)
    managed_temp: tempfile.TemporaryDirectory[str] | None = None
    if provided_temp:
        temp_root = Path(provided_temp).resolve()
        temp_root.mkdir(parents=True, exist_ok=True)
    else:
        managed_temp = tempfile.TemporaryDirectory(
            prefix="goal-plus-verifier-local-"
        )
        temp_root = Path(managed_temp.name).resolve()

    try:
        if temp_root == live or live in temp_root.parents:
            raise ValueError(
                "GOAL_PLUS_VERIFIER_TMPDIR must be outside the live candidate workspace"
            )
        workspace = temp_root / "workspace"
        if workspace.is_symlink() or workspace.is_file():
            workspace.unlink()
        elif workspace.exists():
            shutil.rmtree(workspace)
        copy_source_tree(live, workspace)
        missing = [
            relative.as_posix()
            for relative in _required_relative_paths(required_paths)
            if not (workspace / relative).exists()
        ]
        if missing:
            raise ValueError(
                "isolated verifier workspace is missing required source inputs: "
                + ", ".join(missing)
            )

        diagnostics_value = os.environ.get(VERIFIER_DIAGNOSTICS_ENV)
        diagnostics_dir = (
            Path(diagnostics_value).resolve() if diagnostics_value else None
        )
        if diagnostics_dir is not None:
            if diagnostics_dir == live or live in diagnostics_dir.parents:
                raise ValueError(
                    "GOAL_PLUS_VERIFIER_DIAGNOSTICS_DIR must be outside the "
                    "live candidate workspace"
                )
            diagnostics_dir.mkdir(parents=True, exist_ok=True)

        yield IsolatedVerifierWorkspace(
            live_workspace=live,
            workspace=workspace,
            temp_root=temp_root,
            diagnostics_dir=diagnostics_dir,
        )
    finally:
        if managed_temp is not None:
            managed_temp.cleanup()


def _required_relative_paths(
    paths: Iterable[Path | str],
) -> tuple[Path, ...]:
    normalized: list[Path] = []
    for value in paths:
        path = Path(value)
        if path.is_absolute() or path == Path(".") or ".." in path.parts:
            raise ValueError(f"required verifier path must be relative: {value}")
        normalized.append(path)
    return tuple(normalized)
