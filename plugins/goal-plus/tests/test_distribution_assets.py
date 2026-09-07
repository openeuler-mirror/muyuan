from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.integration
def test_wheel_contains_thinkthread_agent_posix_bridge(tmp_path: Path) -> None:
    pytest.importorskip("build")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(tmp_path),
            str(ROOT),
        ],
        check=True,
    )
    [wheel] = tmp_path.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert "goal_plus/assets/thinkthread-agent-posix-bridge.mjs" in names
    assert "goal_plus/inner_git.py" in names
