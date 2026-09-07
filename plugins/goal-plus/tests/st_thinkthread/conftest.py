from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROFILE = "goal-plus-pi"


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    if not config.getoption("run_system_tests") or config.getoption("collectonly"):
        return
    selected = [
        item
        for item in items
        if "st_pi_thinkthread" in {marker.name for marker in item.iter_markers()}
    ]
    if not selected:
        return
    missing = [name for name in ("tt", "pi", "node") if shutil.which(name) is None]
    reason = f"missing required executables: {', '.join(missing)}" if missing else ""
    if reason:
        for item in selected:
            item.add_marker(pytest.mark.skip(reason=reason))


def _run_tt(*args: str, state_root: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [os.environ.get("ST_THINKTHREAD_BINARY", "tt"), *args, "--state-root", str(state_root)],
        cwd=ROOT,
        check=check,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _wait_for_report(path: Path, *, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        time.sleep(0.2)
    raise TimeoutError(f"ThinkThread ST driver did not write {path}")


def _output(command: list[str]) -> str:
    return subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


@pytest.fixture()
def thinkthread_root(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    workspace = tmp_path / "workspace"
    state_root = tmp_path / "thinkthread-state"
    workspace.mkdir()
    state_root.mkdir()
    yield workspace, state_root
    listed = _run_tt("ps", "-e", "-q", state_root=state_root, check=False)
    ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if ids:
        _run_tt("stop", *ids, state_root=state_root, check=False)
        if os.environ.get("ST_THINKTHREAD_PRESERVE_ON_FAILURE") != "1":
            _run_tt("rm", "--force", *ids, state_root=state_root, check=False)


@pytest.fixture()
def run_thinkthread_scenario(thinkthread_root: tuple[Path, Path]):
    workspace, state_root = thinkthread_root

    def run(scenario: str) -> dict[str, Any]:
        report_path = workspace / ".goal-plus-thinkthread-st-report.json"
        driver_result_path = workspace / ".goal-plus-thinkthread-st-driver.json"
        report_path.unlink(missing_ok=True)
        driver_result_path.unlink(missing_ok=True)
        config = {
            "schema_version": 1,
            "scenario": scenario,
            "report_path": str(report_path),
            "model": os.environ.get("ST_PI_MODEL"),
            "thinking": os.environ.get("ST_PI_THINKING", "low"),
            "host_evidence": {
                "kernel": platform.release(),
                "machine": platform.machine(),
                "thinkthread_version": _output(
                    [os.environ.get("ST_THINKTHREAD_BINARY", "tt"), "--version"]
                ),
                "pi_version": _output(
                    [os.environ.get("ST_PI_BINARY", "pi"), "--version"]
                ),
                "checkout_sha": _output(["git", "rev-parse", "HEAD"]),
                "profile_sha256": hashlib.sha256(
                    _output(
                        [
                            os.environ.get("ST_THINKTHREAD_BINARY", "tt"),
                            "profile",
                            "show",
                            os.environ.get("ST_THINKTHREAD_PROFILE", PROFILE),
                        ]
                    ).encode()
                ).hexdigest(),
            },
        }
        (workspace / ".goal-plus-thinkthread-st.json").write_text(
            json.dumps(config, indent=2) + "\n",
            encoding="utf-8",
        )
        command = [
            os.environ.get("ST_THINKTHREAD_BINARY", "tt"),
            "run",
            "--profile",
            os.environ.get("ST_THINKTHREAD_PROFILE", PROFILE),
            "--fs",
            str(workspace),
            "--state-root",
            str(state_root),
            "--detach",
            "--",
            "--extension",
            str(ROOT / "tests" / "st_thinkthread" / "controller-extension.ts"),
        ]
        if config["model"]:
            command.extend(["--model", config["model"]])
        command.extend(["--thinking", config["thinking"]])
        started = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert started.returncode == 0, started.stderr or started.stdout
        result = _wait_for_report(
            driver_result_path,
            timeout_seconds=int(os.environ.get("ST_THINKTHREAD_TIMEOUT", "1200")),
        )
        assert result["ok"], result
        return _wait_for_report(report_path, timeout_seconds=5)

    return run
