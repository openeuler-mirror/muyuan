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

# Environment auto-detection: any pytest invocation against this suite must
# run in this checkout's uv-managed venv on the interpreter the managed
# release builds with (.python-version, Python 3.12). Newer interpreters
# defer annotation evaluation (PEP 649), which silently hides imports
# referenced only by def-signature annotations - a regression class that
# shipped green on 3.14 and crashed the 3.12 runtime at class creation.
# A bare `pytest` picks whatever is first on PATH, so every mismatch fails
# fast here with the complete recipe for building the isolated environment.
_EXPECTED_PYTHON = (3, 12)
_CHECKOUT = Path(__file__).resolve().parents[1]


def _environment_failure(reason: str) -> "SystemExit":
    return SystemExit(f"""\
goal-plus test environment mismatch: {reason}

The suite must run in this checkout's uv-managed venv on Python
{'.'.join(map(str, _EXPECTED_PYTHON))} (same interpreter as the managed
release; see .python-version). Build the isolated environment and rerun:

    cd {_CHECKOUT}
    uv sync
    uv run pytest tests

`uv sync` creates .venv with the pinned interpreter and the dev tools
(pytest) in one step; always invoke tests through `uv run pytest`, never
a bare `pytest` - it may resolve a foreign environment from PATH.
""")


if sys.version_info[:2] != _EXPECTED_PYTHON:
    raise _environment_failure(
        f"interpreter is {sys.version.split()[0]}, expected "
        f"{'.'.join(map(str, _EXPECTED_PYTHON))}"
    )

try:
    import goal_plus  # noqa: E401 (guard reports the environment, not the import)
except ImportError:
    raise _environment_failure("goal_plus is not importable from this environment")

if Path(goal_plus.__file__).resolve().parent != (_CHECKOUT / "src" / "goal_plus").resolve():
    raise _environment_failure(
        f"goal_plus imported from {goal_plus.__file__}, not this checkout"
    )

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
    # A checkout-local basetemp is itself nested below the repository. Keep
    # plain test projects there from accidentally inheriting the checkout's
    # Git root; projects that create their own .git directory still resolve
    # normally before this ceiling is reached.
    os.environ.setdefault(
        "GIT_CEILING_DIRECTORIES",
        str(Path.cwd() / ".tmp"),
    )
    # Keep test locks inside the checkout instead of a shared host temporary
    # namespace (.tmp is gitignored); an exported value still wins.
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
