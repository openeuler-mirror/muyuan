"""Read-only validation of formal, recursively initialized Git submodules."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from goal_plus.domain.serialization import canonical_json
from goal_plus.domain.submodules import (
    SubmoduleCheckError,
    SubmoduleNode,
    SubmoduleTopology,
)


def _git(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", *args], cwd=root, input=input_bytes, capture_output=True, check=False,
    )
    if result.returncode:
        raise SubmoduleCheckError(
            "SubmoduleGitUnavailable", path=str(root), stage="git " + " ".join(args),
            actual=result.stderr.decode("utf-8", errors="replace").strip()[:300],
        )
    return result.stdout


def _text(root: Path, *args: str) -> str:
    return _git(root, *args).decode("utf-8", errors="surrogateescape").strip()


def _gitlinks(root: Path, revision: str) -> dict[str, str]:
    links: dict[str, str] = {}
    for entry in _git(root, "ls-tree", "-r", "-z", revision).split(b"\0"):
        if not entry:
            continue
        metadata, separator, path = entry.partition(b"\t")
        if not separator:
            raise SubmoduleCheckError("SubmoduleTopologyInvalid", path="", stage="ls-tree")
        mode, kind, commit = metadata.split(b" ")
        if mode == b"160000":
            if kind != b"commit":
                raise SubmoduleCheckError("SubmoduleTopologyInvalid", path=path.decode(), stage="gitlink")
            links[path.decode("utf-8", errors="surrogateescape")] = commit.decode("ascii")
    return links


def _declarations(root: Path, revision: str) -> dict[str, str]:
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{revision}:.gitmodules"], cwd=root, capture_output=True,
        check=False,
    ).returncode == 0
    if not exists:
        return {}
    entries = _git(root, "config", "--blob", f"{revision}:.gitmodules", "--null", "--list")
    values: dict[str, dict[str, str]] = {}
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        key, separator, value = entry.partition(b"\n")
        if not separator:
            raise SubmoduleCheckError("SubmoduleTopologyInvalid", path=".gitmodules", stage="config")
        name = key.decode("utf-8", errors="surrogateescape")
        if not name.startswith("submodule."):
            continue
        section, dot, field = name.rpartition(".")
        if not dot or field not in {"path", "url"}:
            continue
        values.setdefault(section, {})[field] = value.decode("utf-8", errors="surrogateescape")
    declarations: dict[str, str] = {}
    for fields in values.values():
        path, url = fields.get("path"), fields.get("url")
        if not path or not url or path in declarations or Path(path).is_absolute() or ".." in Path(path).parts:
            raise SubmoduleCheckError("SubmoduleTopologyInvalid", path=path or ".gitmodules", stage="declaration")
        declarations[path] = url
    return declarations


def _unregistered_git_roots(root: Path, child_paths: set[str]) -> list[str]:
    found: list[str] = []
    for directory, names, files in os.walk(root, followlinks=False):
        relative = Path(directory).relative_to(root).as_posix()
        if relative == ".":
            relative = ""
        if relative and ".git" in names + files:
            found.append(relative)
        names[:] = [
            name for name in names
            if name != ".git"
            and not (Path(relative) / name).as_posix() in child_paths
            and not (Path(directory) / name).is_symlink()
        ]
    return found


def thinkthread_submodule_paths(source: Path) -> list[str]:
    """Return nested Git paths that an fs snapshot cannot preserve exactly.

    This is deliberately an admission probe rather than a topology validator:
    ThinkThread has no contract for retaining Git metadata or child object
    stores, so any committed or materialized nested repository is unsupported.
    """
    source = source.resolve()
    try:
        repository = Path(_text(source, "rev-parse", "--show-toplevel")).resolve()
        source_prefix = source.relative_to(repository)
    except (SubmoduleCheckError, FileNotFoundError, ValueError):
        repository = None

    paths: set[str] = set()
    if repository is not None:
        try:
            gitmodules_path = (source_prefix / ".gitmodules").as_posix()
            has_gitmodules = subprocess.run(
                ["git", "cat-file", "-e", f"HEAD:{gitmodules_path}"],
                cwd=repository,
                capture_output=True,
                check=False,
            ).returncode == 0
            if has_gitmodules:
                paths.add(".gitmodules")
            tree = "HEAD" if not source_prefix.parts else f"HEAD:{source_prefix.as_posix()}"
            paths.update(_gitlinks(repository, tree))
        except SubmoduleCheckError:
            # An untracked source subtree or not-yet-committed root tree has
            # no committed submodules; still inspect its materialized files.
            pass

    for directory, names, files in os.walk(source, followlinks=False):
        current = Path(directory)
        if current == source:
            names[:] = [name for name in names if name != ".git"]
            continue
        if ".git" in names or ".git" in files:
            paths.add(current.relative_to(source).as_posix())
            names[:] = []
            continue
        names[:] = [name for name in names if name != ".git"]
    return sorted(paths)


def ensure_thinkthread_submodules_supported(source: Path) -> None:
    paths = thinkthread_submodule_paths(source)
    if not paths:
        return
    preview = ", ".join(paths[:8])
    suffix = " ..." if len(paths) > 8 else ""
    raise ValueError(
        "ThinkThreadSubmoduleUnsupported: stage=thinkthread-admission; "
        "reason: fs_snapshot cannot preserve exact Git submodule metadata or "
        "child object closure; "
        f"path: {Path(source).resolve()}; nested paths: {preview}{suffix}"
    )


def inspect_submodules(root: Path, *, require_clean: bool = True) -> SubmoduleTopology:
    """Inspect the checked-out HEAD; every child must match its committed gitlink."""
    root = root.resolve()
    nodes: list[SubmoduleNode] = []
    active_roots: set[Path] = set()
    digest_entries: list[dict[str, str]] = []

    def visit(repository: Path, prefix: str, depth: int) -> str:
        actual_root = Path(_text(repository, "rev-parse", "--show-toplevel")).resolve()
        if actual_root != repository:
            raise SubmoduleCheckError(
                "SubmoduleRepositoryMismatch", path=prefix, stage="repository-root",
                expected=str(repository), actual=str(actual_root),
            )
        if repository in active_roots:
            raise SubmoduleCheckError("SubmoduleTopologyCycle", path=prefix, stage="recursion")
        active_roots.add(repository)
        try:
            head = _text(repository, "rev-parse", "HEAD")
            if _text(repository, "rev-parse", "--is-shallow-repository") == "true":
                raise SubmoduleCheckError("SubmoduleObjectUnavailable", path=prefix, stage="shallow")
            git_dir = Path(_text(repository, "rev-parse", "--absolute-git-dir"))
            for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REBASE_HEAD"):
                if (git_dir / marker).exists():
                    raise SubmoduleCheckError("SubmoduleWorkspaceDirty", path=prefix, stage=marker)
            if require_clean and _git(repository, "status", "--porcelain", "-z", "--untracked-files=all", "--ignore-submodules=all"):
                raise SubmoduleCheckError("SubmoduleWorkspaceDirty", path=prefix, stage="status")
            links = _gitlinks(repository, head)
            declared = _declarations(repository, head)
            if set(links) != set(declared):
                differing = min(set(links) ^ set(declared))
                raise SubmoduleCheckError(
                    "SubmoduleTopologyInvalid", path=f"{prefix}/{differing}".strip("/"),
                    stage="gitmodules-gitlink", expected=str(sorted(links)), actual=str(sorted(declared)),
                )
            extra = _unregistered_git_roots(repository, set(links))
            if extra:
                raise SubmoduleCheckError(
                    "SubmoduleUnregisteredGit", path=f"{prefix}/{extra[0]}".strip("/"), stage="nested-git",
                )
            for child_path, gitlink in sorted(links.items()):
                relative = f"{prefix}/{child_path}".strip("/")
                child = repository / child_path
                if not child.is_dir() or not (child / ".git").exists():
                    raise SubmoduleCheckError("SubmoduleUninitialized", path=relative, stage="checkout")
                if not child.resolve().is_relative_to(root):
                    raise SubmoduleCheckError("SubmoduleTopologyInvalid", path=relative, stage="path-escape")
                child_head = _text(child, "rev-parse", "HEAD")
                if child_head != gitlink:
                    raise SubmoduleCheckError(
                        "SubmoduleHeadMismatch", path=relative, stage="gitlink",
                        expected=gitlink, actual=child_head,
                    )
                if subprocess.run(
                    ["git", "cat-file", "-e", f"{gitlink}^{{commit}}"],
                    cwd=child,
                    capture_output=True,
                    check=False,
                ).returncode:
                    raise SubmoduleCheckError("SubmoduleObjectUnavailable", path=relative, stage="object", expected=gitlink)
                url = declared[child_path]
                nodes.append(SubmoduleNode(
                    path=relative, url=url, repository_identity=f"{relative}:{url}",
                    gitlink=gitlink, child_head=child_head, depth=depth + 1,
                ))
                digest_entries.append({"path": relative, "url": url, "gitlink": gitlink})
                visit(child.resolve(), relative, depth + 1)
            return head
        finally:
            active_roots.remove(repository)

    parent_head = visit(root, "", 0)
    digest = hashlib.sha256(canonical_json(digest_entries).encode("utf-8")).hexdigest()
    return SubmoduleTopology(parent_head=parent_head, nodes=nodes, topology_digest=digest)
