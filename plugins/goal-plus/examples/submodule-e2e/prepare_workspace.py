"""Create an offline recursive submodule source for Goal Plus tests."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "-c", "user.name=goal-plus-e2e",
         "-c", "user.email=e2e@example.invalid", *args],
        cwd=root, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def create_workspace(directory: Path) -> dict[str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    remotes = directory / "remotes"
    remotes.mkdir()

    def repository(name: str, files: dict[str, str]) -> Path:
        root = directory / f"{name}-source"
        root.mkdir()
        git(root, "init", "-q")
        for relative, content in files.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        git(root, "add", "-A")
        git(root, "commit", "-qm", f"{name} baseline")
        return root

    plugin = repository("plugin", {"plugin.py": "VALUE = 100\n"})
    plugin_remote = remotes / "plugin.git"
    git(remotes, "clone", "-q", "--bare", str(plugin), str(plugin_remote))

    repo_a = repository("repo-a", {"value.py": "VALUE = 1\n"})
    git(repo_a, "submodule", "add", "-q", str(plugin_remote), "modules/plugin")
    git(repo_a, "commit", "-qam", "add nested plugin")
    repo_a_remote = remotes / "repo-a.git"
    git(remotes, "clone", "-q", "--bare", str(repo_a), str(repo_a_remote))

    repo_b = repository("repo-b", {"value.py": "VALUE = 10\n"})
    repo_b_remote = remotes / "repo-b.git"
    git(remotes, "clone", "-q", "--bare", str(repo_b), str(repo_b_remote))

    parent = directory / "parent"
    parent.mkdir()
    git(parent, "init", "-q")
    (parent / "verifier.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "a = int(Path('modules/repo-a/value.py').read_text().split('=')[1])\n"
        "b = int(Path('modules/repo-b/value.py').read_text().split('=')[1])\n"
        "print(json.dumps({'combined_score': a + b}))\n",
        encoding="utf-8",
    )
    git(parent, "add", "verifier.py")
    git(parent, "commit", "-qm", "parent baseline")
    git(parent, "submodule", "add", "-q", str(repo_a_remote), "modules/repo-a")
    git(parent, "submodule", "add", "-q", str(repo_b_remote), "modules/repo-b")
    git(parent, "commit", "-qam", "add child repositories")
    git(parent, "submodule", "update", "--init", "--recursive")
    return {
        "parent": str(parent),
        "P0": git(parent, "rev-parse", "HEAD"),
        "A0": git(parent / "modules/repo-a", "rev-parse", "HEAD"),
        "AP0": git(parent / "modules/repo-a/modules/plugin", "rev-parse", "HEAD"),
        "B0": git(parent / "modules/repo-b", "rev-parse", "HEAD"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output is None:
        temporary_root = Path(
            os.environ.get("GOAL_PLUS_TMPDIR") or Path.cwd() / ".tmp"
        ).expanduser()
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="goal-plus-submodule-e2e-", dir=temporary_root
        ) as directory:
            print(json.dumps(create_workspace(Path(directory)), indent=2, sort_keys=True))
    else:
        print(json.dumps(create_workspace(args.output), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
