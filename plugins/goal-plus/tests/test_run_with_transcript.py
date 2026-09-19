from __future__ import annotations

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_wrapper_exports_after_wrapped_command_failure(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "python-calls.log"
    (tmp_path / ".gp" / "runs").mkdir(parents=True)
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$*" >> "$GP_TEST_CALLS"\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    failing = tmp_path / "fail.sh"
    failing.write_text(
        "#!/usr/bin/env bash\n"
        "sleep 0.1\n"
        "exit 7\n",
        encoding="utf-8",
    )
    failing.chmod(0o755)

    result = subprocess.run(
        ["bash", str(ROOT / "scripts/run-with-transcript.sh"), str(failing)],
        cwd=tmp_path,
        env={
            **os.environ,
            "GOAL_PLUS_DEBUG": "1",
            "GP_TEST_CALLS": str(calls),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 7
    invocations = calls.read_text(encoding="utf-8").splitlines()
    assert any("--watch" in invocation for invocation in invocations)
    assert any("--watch" not in invocation for invocation in invocations)
