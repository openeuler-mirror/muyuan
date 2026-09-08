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

import pytest

from tests.st.hosts import ST_ACTIVE_ENV

_OPT_IN_MARKERS = ("integration", "example")


@pytest.fixture
def scope_owner(monkeypatch):
    """Model only the current owner for scope-state tests with no real children."""
    from goal_plus.execution import process_identity, scope_activation
    from goal_plus.ports.process_identity import ProcessSnapshot

    owner = ProcessSnapshot(os.getpid(), "test-owner", os.getpgrp(), "S")

    def inspect(pid):
        assert pid == owner.pid, "scope_owner does not model child processes"
        return owner

    monkeypatch.setattr(process_identity, "inspect_process", inspect)
    monkeypatch.setattr(scope_activation, "inspect_process", inspect)
    return owner


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
            if item.get_closest_marker("linux_procfs") is not None:
                item.add_marker(pytest.mark.skip(reason="requires Linux /proc process identity and lifecycle"))
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
