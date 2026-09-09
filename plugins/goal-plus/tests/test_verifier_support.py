from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from goal_plus.verifier_support import (
    isolated_verifier_workspace,
    sanitized_evaluator_environment,
)


def test_isolated_verifier_workspace_keeps_live_source_read_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / ".gitignore").write_text("build/\n*.so\n", encoding="utf-8")
    (source / "kernel.cpp").write_text("// source\n", encoding="utf-8")
    (source / "build").mkdir()
    (source / "build" / "old.o").write_text("old\n", encoding="utf-8")
    (source / "extension.so").write_text("old\n", encoding="utf-8")
    invocation = tmp_path / "invocation"
    diagnostics = tmp_path / "diagnostics"
    monkeypatch.setenv("GOAL_PLUS_VERIFIER_TMPDIR", str(invocation))
    monkeypatch.setenv("GOAL_PLUS_VERIFIER_DIAGNOSTICS_DIR", str(diagnostics))

    with isolated_verifier_workspace(source) as isolated:
        assert isolated.workspace == invocation / "workspace"
        assert isolated.diagnostics_dir == diagnostics
        assert (isolated.workspace / "kernel.cpp").is_file()
        assert not (isolated.workspace / "build").exists()
        assert not (isolated.workspace / "extension.so").exists()
        (isolated.workspace / "build").mkdir()
        (isolated.workspace / "build" / "new.o").write_text(
            "generated\n", encoding="utf-8"
        )
        diagnostics.joinpath("official-result.json").write_text(
            "{}\n", encoding="utf-8"
        )

    assert source.joinpath("kernel.cpp").read_text(encoding="utf-8") == "// source\n"
    assert not source.joinpath("build", "new.o").exists()
    assert diagnostics.joinpath("official-result.json").is_file()


def test_isolated_verifier_workspace_cleans_direct_self_test_scratch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "kernel.cpp").write_text("// source\n", encoding="utf-8")
    monkeypatch.delenv("GOAL_PLUS_VERIFIER_TMPDIR", raising=False)
    monkeypatch.delenv("GOAL_PLUS_VERIFIER_DIAGNOSTICS_DIR", raising=False)

    with isolated_verifier_workspace(source) as isolated:
        temp_root = isolated.temp_root
        assert isolated.workspace.joinpath("kernel.cpp").is_file()

    assert not temp_root.exists()


def test_isolated_verifier_workspace_rejects_diagnostics_in_live_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "kernel.cpp").write_text("// source\n", encoding="utf-8")
    monkeypatch.setenv(
        "GOAL_PLUS_VERIFIER_DIAGNOSTICS_DIR",
        str(source / "diagnostics"),
    )

    with pytest.raises(ValueError, match="DIAGNOSTICS_DIR must be outside"):
        with isolated_verifier_workspace(source):
            pass

    assert not source.joinpath("diagnostics").exists()


def test_isolated_verifier_workspace_preserves_required_build_scripts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    source.joinpath("scripts").mkdir(parents=True)
    source.joinpath("build.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    source.joinpath("setup.py").write_text("# setup\n", encoding="utf-8")
    source.joinpath("CMakeLists.txt").write_text("# cmake\n", encoding="utf-8")
    source.joinpath("scripts", "build_wheel.sh").write_text(
        "#!/bin/sh\n", encoding="utf-8"
    )
    source.joinpath("build").mkdir()
    source.joinpath("build", "kernel.o").write_text("generated\n", encoding="utf-8")
    source.joinpath("dist").mkdir()
    source.joinpath("dist", "candidate.whl").write_text(
        "generated\n", encoding="utf-8"
    )
    monkeypatch.setenv("GOAL_PLUS_VERIFIER_TMPDIR", str(tmp_path / "invocation"))

    required = (
        "build.sh",
        "setup.py",
        "CMakeLists.txt",
        "scripts/build_wheel.sh",
    )
    with isolated_verifier_workspace(source, required_paths=required) as isolated:
        for relative in required:
            assert isolated.workspace.joinpath(relative).is_file()
        assert not isolated.workspace.joinpath("build").exists()
        assert not isolated.workspace.joinpath("dist").exists()


def test_isolated_verifier_workspace_rejects_missing_required_source_input(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    source.joinpath("operator.cpp").write_text("// operator\n", encoding="utf-8")

    with pytest.raises(ValueError, match="scripts/build_wheel.sh"):
        with isolated_verifier_workspace(
            source,
            required_paths=("build.sh", "scripts/build_wheel.sh"),
        ):
            pass


def test_sanitized_evaluator_environment_drops_live_workspace_pythonpath(
    tmp_path: Path,
    monkeypatch,
) -> None:
    live_source = tmp_path / "live-source"
    pinned_source = tmp_path / "pinned-source"
    neutral_cwd = tmp_path / "neutral"
    for root, origin in ((live_source, "live"), (pinned_source, "pinned")):
        package = root / "cann_bench"
        package.mkdir(parents=True)
        package.joinpath("__init__.py").write_text(
            f"ORIGIN = {origin!r}\n",
            encoding="utf-8",
        )
    neutral_cwd.mkdir()
    monkeypatch.setenv("PYTHONPATH", str(live_source))

    evaluator_env = sanitized_evaluator_environment(
        python_paths=(pinned_source,),
    )
    completed = subprocess.run(
        [sys.executable, "-c", "import cann_bench; print(cann_bench.ORIGIN)"],
        cwd=neutral_cwd,
        env=evaluator_env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.strip() == "pinned"
    assert evaluator_env["PYTHONPATH"] == str(pinned_source.resolve())
    assert os.environ["PYTHONPATH"] == str(live_source)


def test_sanitized_evaluator_environment_removes_pythonpath_without_dependencies(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PYTHONPATH", "/candidate/workspace")

    evaluator_env = sanitized_evaluator_environment()

    assert "PYTHONPATH" not in evaluator_env
