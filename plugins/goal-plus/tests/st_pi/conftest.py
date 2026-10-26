from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests._st_tmp import new_st_run_dir


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
    """Temporary run root for a Pi ST scenario, under the ST scratch base.

    Removed once the test finishes unless the run is kept for debugging; see
    tests/_st_tmp.py for the base location and retention rules.
    """
    return new_st_run_dir(request, kind="st-pi-runs")
