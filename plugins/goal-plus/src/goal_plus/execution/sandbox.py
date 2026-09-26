"""Resolve trusted child-process sandbox policies into OS launch commands."""

from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile
from collections.abc import Callable, Sequence

from goal_plus.ports.sandbox import ProcessSandbox


class SandboxUnavailable(RuntimeError):
    """The requested protected execution backend is not usable on this host."""


def resolve_sandbox_command(
    command: Sequence[str],
    *,
    cwd: Path,
    sandbox: ProcessSandbox | None,
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[str, ...]:
    """Resolve one command without silently weakening a requested policy."""

    if sandbox is None or sandbox.backend == "native":
        return tuple(command)
    platform = platform or sys.platform
    if platform == "darwin":
        executable = which("sandbox-exec")
        if executable is None:
            raise SandboxUnavailable("macOS protected execution requires sandbox-exec")
        return _seatbelt_command(executable, command, sandbox)
    if platform.startswith("linux"):
        executable = which("bwrap")
        if executable is None:
            raise SandboxUnavailable("Linux protected execution requires bubblewrap (bwrap)")
        return _bubblewrap_command(executable, command, cwd, sandbox)
    raise SandboxUnavailable(f"no protected execution backend for platform {platform!r}")


def _normal_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _unique_writable_roots(sandbox: ProcessSandbox) -> tuple[Path, ...]:
    roots: list[Path] = []
    if sandbox.mode == "workspace-write":
        roots.append(_normal_path(sandbox.workspace))
    roots.extend(_normal_path(path) for path in sandbox.writable_roots)
    roots.append(_normal_path(Path(tempfile.gettempdir())))
    seen: set[Path] = set()
    result: list[Path] = []
    for root in roots:
        if root == Path("/"):
            raise ValueError("sandbox writable root cannot be filesystem root")
        if root not in seen:
            seen.add(root)
            result.append(root)
    return tuple(result)


def _sbpl_string(path: Path) -> str:
    value = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{value}"'


def _seatbelt_command(
    executable: str, command: Sequence[str], sandbox: ProcessSandbox,
) -> tuple[str, ...]:
    roots = _unique_writable_roots(sandbox)
    if Path("/private/tmp") not in roots:
        roots = (*roots, Path("/private/tmp"))
    allowed_paths = [f"(subpath {_sbpl_string(root)})" for root in roots]
    # Keep standard character devices usable without granting filesystem writes.
    allowed_paths.extend(
        f"(literal {_sbpl_string(Path(path))})"
        for path in ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom", "/dev/ptmx")
    )
    allowed = " ".join(allowed_paths)
    profile = (
        '(version 1) (allow default) '
        f"(deny file-write* (require-not (require-any {allowed})))"
    )
    if sandbox.network == "deny":
        profile += " (deny network*)"
    return (executable, "-p", profile, *command)


def _bubblewrap_command(
    executable: str, command: Sequence[str], cwd: Path, sandbox: ProcessSandbox,
) -> tuple[str, ...]:
    roots = _unique_writable_roots(sandbox)
    cwd = _normal_path(cwd)
    visible_roots = [sandbox.workspace, *sandbox.writable_roots]
    if not any(
        cwd == _normal_path(root) or cwd.is_relative_to(_normal_path(root))
        for root in visible_roots
    ):
        raise ValueError(f"sandbox cwd {cwd} is outside its declared roots")
    args: list[str] = [
        executable, "--die-with-parent", "--new-session", "--unshare-user", "--unshare-pid",
        "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
    ]
    for root in roots:
        args.extend(("--bind", str(root), str(root)))
    if sandbox.network == "deny":
        args.append("--unshare-net")
    args.extend(("--chdir", str(cwd), "--", *command))
    return tuple(args)
