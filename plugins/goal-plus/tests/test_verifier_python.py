"""Python verifiers use the owning runtime while retaining workspace imports."""

import json
import os
import sys

import pytest

from goal_plus.infrastructure.verifiers.local import LocalVerifiers


@pytest.mark.parametrize("executable", ["python", "python3"])
def test_local_python_verifier_ignores_foreign_environment(tmp_path, monkeypatch, executable):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    for name in ("python", "python3"):
        command = foreign / name
        command.write_text("#!/bin/sh\nexit 86\n")
        command.chmod(0o755)
    (foreign / "json.py").write_text("raise RuntimeError('foreign Python module')\n")
    monkeypatch.setenv("PATH", str(foreign) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("PYTHONHOME", str(foreign))
    monkeypatch.setenv("PYTHONPATH", str(foreign))
    monkeypatch.setenv("GOAL_PLUS_PYTHON", str(foreign / "python"))
    workspace = tmp_path / "workspace"
    scripts = workspace / "verifiers"
    scripts.mkdir(parents=True)
    (workspace / "solution.py").write_text("VALUE = 42\n")
    evaluator = scripts / "score.py"
    evaluator.write_text(
        "import json, sys, solution\n"
        "print(json.dumps({'score': solution.VALUE, 'python': sys.executable, 'args': sys.argv[1:]}))\n"
    )
    verifiers = LocalVerifiers(tmp_path / ".gp")
    arguments = ["a b", "$(touch unexpected)", "X=value"]
    result = verifiers.execute_verifier_process(
        [executable, str(evaluator), *arguments], cwd=workspace,
        env=verifiers.verifier_environment(workspace, tmp_path / "scratch", phase="candidate"),
        text=True, capture_output=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"score": 42, "python": sys.executable, "args": arguments}
    assert not (workspace / "unexpected").exists()


def test_explicit_verifier_executable_is_preserved(tmp_path):
    command = tmp_path / "python"
    command.write_text('#!/bin/sh\nprintf \'{"score":17}\\n\'\n')
    command.chmod(0o755)
    result = LocalVerifiers(tmp_path / ".gp").execute_verifier_process(
        [str(command)], cwd=tmp_path, env={}, text=True, capture_output=True,
        timeout=10, check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"score": 17}
