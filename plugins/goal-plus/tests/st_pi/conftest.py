from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    if not config.getoption("run_system_tests") or config.getoption("collectonly"):
        return
    st_pi_items = [
        item
        for item in items
        if "st_pi" in [marker.name for marker in item.iter_markers()]
    ]
    if not st_pi_items:
        return

    pi_binary = os.environ.get("ST_PI_BINARY", "pi")
    if shutil.which(pi_binary) is None:
        for item in st_pi_items:
            item.add_marker(pytest.mark.skip(reason=f"{pi_binary} binary not on PATH"))


@pytest.fixture()
def st_pi_run_root(request: pytest.FixtureRequest) -> Path:
    base = ROOT / ".tmp" / "st-pi-runs"
    base.mkdir(parents=True, exist_ok=True)
    node = request.node.name.replace("::", "_").replace("[", "_").replace("]", "")
    run_root = Path(tempfile.mkdtemp(prefix=f"{node}-", dir=base))
    return run_root
