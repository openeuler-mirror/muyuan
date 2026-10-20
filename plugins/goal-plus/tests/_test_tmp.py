"""Retain failed test artifacts until all fixture teardown has completed."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest

DEBUG_ENV = "GOAL_PLUS_TEST_DEBUG"
TMP_ENV = "GOAL_PLUS_TEST_TMP"
_DEBUG = pytest.StashKey[bool]()
_DIRECTORIES = pytest.StashKey[list[Path]]()
_FAILED = pytest.StashKey[bool]()


def configure_test_tmp(config: pytest.Config) -> None:
    config.stash[_DEBUG] = os.environ.get(DEBUG_ENV) == "1"
    # Pytest's built-in ``tmp_path`` fixture otherwise defaults to the host
    # tempfile directory (usually /tmp).  A large suite can create thousands
    # of inodes there, and the location then depends on the host's mount
    # layout.  Keep the complete pytest scratch tree in the checkout's
    # ignored .tmp directory.  An explicit ``--basetemp`` remains authoritative
    # so callers can still choose a different location when needed.
    if config.getoption("basetemp", default=None) is None:
        configured = os.environ.get(TMP_ENV)
        basetemp = (
            Path(configured).expanduser()
            if configured
            else Path.cwd() / ".tmp" / "pytest"
        )
        config.option.basetemp = str(basetemp.resolve())


def track_test_directory(request: pytest.FixtureRequest, path: Path) -> Path:
    request.node.stash.setdefault(_DIRECTORIES, []).append(path)
    return path


@pytest.fixture
def tmp_path(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    # Tests put protected runtime state beside the workspace. Own that parent
    # too, so early cleanup cannot leave state behind when pytest reuses a name.
    track_test_directory(request, tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def _remove_directory(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
        return
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return
    except PermissionError:
        # Published tool fixtures contain read-only directories. Repair only
        # this test's remaining tree; never follow links to external artifacts.
        path.chmod(path.stat().st_mode | stat.S_IRWXU)
        for root, directories, files in os.walk(path, followlinks=False):
            for name in directories:
                child = Path(root) / name
                mode = child.lstat().st_mode
                if stat.S_ISDIR(mode):
                    child.chmod(mode | stat.S_IRWXU)
            if os.name == "nt":
                for name in files:
                    child = Path(root) / name
                    mode = child.lstat().st_mode
                    if stat.S_ISREG(mode):
                        child.chmod(mode | stat.S_IRUSR | stat.S_IWUSR)
        shutil.rmtree(path)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item, call):
    outcome = yield
    report = outcome.get_result()
    item.stash[_FAILED] = item.stash.get(_FAILED, False) or report.failed
    if report.when != "teardown":
        return
    directories = item.stash.get(_DIRECTORIES, [])
    if item.stash[_FAILED] or item.config.stash[_DEBUG]:
        return
    for path in directories:
        try:
            _remove_directory(path)
        except OSError as exc:
            item.warn(pytest.PytestWarning(f"test temporary directory cleanup failed: {path}: {exc}"))
