"""Native patch target admission; not an arbitrary shell filesystem sandbox."""

import os
from pathlib import Path

from goal_plus.domain.edit_surface import path_matches
from goal_plus.ports.candidate_host import CandidateFileWriteBoundary


def patch_targets(command: object) -> list[str]:
    """Read native patch declarations, never model-generated JavaScript or shell."""
    if not isinstance(command, str):
        raise ValueError("native patch command must be text")
    lines = command.strip().splitlines()
    if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError("unrecognized native patch envelope")
    targets: list[str] = []
    operation = None
    for line in lines[1:-1]:
        for header in ("*** Add File: ", "*** Delete File: ", "*** Update File: ", "*** Move to: "):
            if line.startswith(header):
                if header == "*** Move to: " and operation != "*** Update File: ":
                    raise ValueError("unexpected native patch move")
                target = line[len(header):]
                if not target or target != target.strip() or "\x00" in target:
                    raise ValueError("invalid native patch target")
                targets.append(target)
                operation = header
                break
        else:
            if line == "*** End of File" or line.startswith((" ", "+", "-", "@")) or not line:
                continue
            raise ValueError("unrecognized native patch declaration")
    return targets


def require_candidate_patch(
    command: object, *, cwd: Path, boundary: CandidateFileWriteBoundary,
) -> None:
    workspace = boundary.workspace.resolve()
    for target in patch_targets(command):
        # Check both the named path and its symlink-resolved destination.
        raw_path = cwd / target
        named = Path(os.path.abspath(raw_path))
        resolved = raw_path.resolve()
        for path in (named, resolved):
            if not path.is_relative_to(workspace) or path == workspace:
                raise PermissionError("patch target is outside the candidate workspace")
            if path_matches(path.relative_to(workspace).as_posix(), boundary.denied_files):
                raise PermissionError("patch target is read-only in the candidate contract")
        if resolved.exists() and resolved.stat().st_nlink > 1:
            raise PermissionError("patch target has shared filesystem links")
