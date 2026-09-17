"""Shared pytest configuration for the goal-plus test suite.

Tests carrying ``integration`` or ``example``
markers are skipped unless the corresponding marker name appears in the
``-m`` selection expression. This keeps the default ``pytest`` invocation
focused on maintained fast tests.
Real-host system tests additionally require ``--run-system-tests``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests._test_tmp import (
    configure_test_tmp,
    pytest_runtest_makereport,  # noqa: F401 (registers the hook)
    tmp_path,  # noqa: F401 (registers the fixture)
)
from tests.st.hosts import ST_ACTIVE_ENV

_OPT_IN_MARKERS = ("integration", "example")


@pytest.fixture
def scripted_process_output(monkeypatch):
    """Keep host contract doubles at the pipe collector boundary, not in production."""
    from types import SimpleNamespace
    import goal_plus.execution.managed_process as process_module

    monkeypatch.setattr(
        process_module, "ProcessOutput",
        lambda process, **_options: SimpleNamespace(communicate=process.communicate, close=lambda: None),
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-system-tests",
        action="store_true",
        default=False,
        help="Allow explicitly requested real-host system tests (run with -n 0).",
    )


def pytest_configure(config: pytest.Config) -> None:
    configure_test_tmp(config)
    # On shared machines /tmp/goal-plus-verifier-locks belongs to whichever
    # user created it first, so other users cannot allocate their lock
    # namespace. Keep test locks inside the checkout instead (.tmp is
    # gitignored); an exported value still wins.
    os.environ.setdefault(
        "GOAL_PLUS_VERIFIER_RESOURCE_LOCK_DIR",
        str(Path(__file__).resolve().parents[1] / ".tmp" / "verifier-locks"),
    )
    if not config.getoption("run_system_tests"):
        return
    if os.environ.get(ST_ACTIVE_ENV):
        raise pytest.UsageError("refusing nested system tests inside an active ST host")
    if config.getoption("numprocesses", default=None) not in (None, 0):
        raise pytest.UsageError("real-host system tests must run serially with -n 0")


@pytest.fixture(autouse=True)
def disable_real_evidence_annotator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unit tests opt into annotator process launches explicitly."""
    monkeypatch.setenv("GOAL_PLUS_EVIDENCE_ANNOTATOR_DISABLED", "1")
    monkeypatch.delenv("GOAL_PLUS_GLOBAL_EVIDENCE_MODE", raising=False)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Gate selected tests before any fixture can start a real host."""
    if sys.platform != "linux":
        for item in items:
            if item.get_closest_marker("thinkthread_linux") is not None:
                item.add_marker(pytest.mark.skip(reason="requires the ThinkThread Linux runtime"))
    system_items = [
        item
        for item in items
        if any(m.name == "st" or m.name.startswith("st_") for m in item.iter_markers())
    ]
    if not config.getoption("run_system_tests"):
        for item in system_items:
            item.add_marker(
                pytest.mark.skip(
                    reason="system tests require explicit --run-system-tests"
                )
            )
    markexpr = config.getoption("-m") or ""
    for name in _OPT_IN_MARKERS:
        if name in markexpr:
            continue
        for item in items:
            if any(m.name == name for m in item.iter_markers()):
                item.add_marker(
                    pytest.mark.skip(
                        reason=f"{name} tests not selected (use `-m {name}` to run)"
                    )
                )
