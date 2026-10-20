"""Guard the scratch space that real-host system tests run in.

ST run directories used to be created in the host's global tempfile directory.
These cases pin the checkout-local contract: the default scratch base is
``$PWD/.tmp/st``, an explicit override is honored, and a run directory is
released once its test finishes.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from tests._st_tmp import (
    TMP_ENV,
    new_st_log_dir,
    new_st_run_dir,
    st_tmp_root,
)
from tests._test_tmp import DEBUG_ENV, TMP_ENV as TEST_TMP_ENV

pytest_plugins = ["pytester"]


def _make_test_tree_removable(root: Path) -> None:
    """Restore owner permissions before pytester removes its temporary tree."""
    for directory, children, _files in os.walk(root, followlinks=False):
        Path(directory).chmod(Path(directory).stat().st_mode | stat.S_IRWXU)
        for name in children:
            child = Path(directory) / name
            if not child.is_symlink():
                child.chmod(child.stat().st_mode | stat.S_IRWXU)


def test_scratch_base_defaults_to_the_checkout_tmp_tree() -> None:
    base = st_tmp_root()
    assert base == (Path.cwd() / ".tmp" / "st").resolve()
    assert base.parent == (Path.cwd() / ".tmp").resolve()


def test_pytest_tmp_path_defaults_to_the_checkout_tmp_tree(tmp_path) -> None:
    configured = os.environ.get(TEST_TMP_ENV)
    expected = (
        Path(configured).expanduser().resolve()
        if configured
        else (Path.cwd() / ".tmp" / "pytest").resolve()
    )
    assert expected in tmp_path.parents


def test_scratch_base_follows_the_environment_override(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(TMP_ENV, str(tmp_path / "elsewhere"))
    assert st_tmp_root() == (tmp_path / "elsewhere").resolve()


def test_run_dir_is_created_under_the_base(tmp_path, monkeypatch, request) -> None:
    monkeypatch.setenv(TMP_ENV, str(tmp_path / "scratch"))

    run_dir = new_st_run_dir(request, kind="st-runs")

    assert run_dir.parent == (tmp_path / "scratch" / "st-runs").resolve()
    assert run_dir.name.startswith(request.node.name + "-")


def test_each_run_dir_is_unique(tmp_path, monkeypatch, request) -> None:
    monkeypatch.setenv(TMP_ENV, str(tmp_path / "scratch"))
    assert new_st_run_dir(request, kind="st-runs") != new_st_run_dir(
        request, kind="st-runs"
    )


def test_log_dir_is_node_keyed_and_outlives_the_run(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(TMP_ENV, str(tmp_path / "scratch"))

    log_dir = new_st_log_dir("test_scenario")

    assert log_dir == (tmp_path / "scratch" / "st-logs" / "test_scenario").resolve()
    assert new_st_log_dir("test_scenario") == log_dir


@pytest.fixture
def scratch_suite(
    pytester: pytest.Pytester,
    monkeypatch,
    request: pytest.FixtureRequest,
) -> pytest.Pytester:
    """Run one inert case through the real ST fixture in a subprocess."""
    root = Path(__file__).resolve().parents[1]
    monkeypatch.delenv(DEBUG_ENV, raising=False)
    monkeypatch.setenv(TMP_ENV, str(pytester.path / "scratch"))
    monkeypatch.setenv("PYTHONPATH", str(root / "src"))
    pytester.makeconftest(
        f"""
        import sys

        sys.path.insert(0, {str(root)!r})
        pytest_plugins = ["tests.conftest", "tests.st.conftest", "tests.st_pi.conftest"]
        """
    )
    pytester.makepyfile(
        test_scratch=r"""
        from pathlib import Path


        def test_records_its_scratch_root(st_project_root):
            assert st_project_root.is_dir()
            Path("scratch-root.txt").write_text(str(st_project_root))
        """
    )
    request.addfinalizer(lambda: _make_test_tree_removable(pytester.path))
    return pytester


def test_fixture_uses_the_scratch_base_and_releases_it(scratch_suite) -> None:
    result = scratch_suite.runpytest_subprocess("-q")

    result.assert_outcomes(passed=1)
    recorded = Path((scratch_suite.path / "scratch-root.txt").read_text())
    assert recorded.parent == scratch_suite.path / "scratch" / "st-runs"
    assert not recorded.exists()


@pytest.mark.parametrize("debug", [None, "0", "1"])
@pytest.mark.parametrize("workers", [0, 2])
def test_cleanup_waits_for_all_outcomes_and_honors_test_debug(
    scratch_suite, monkeypatch, debug, workers,
) -> None:
    if debug is not None:
        monkeypatch.setenv(DEBUG_ENV, debug)
    # Runtime diagnostics and the superseded ST switch must not retain tests.
    monkeypatch.setenv("GOAL_PLUS_DEBUG", "1")
    monkeypatch.setenv("GOAL_PLUS_ST_KEEP_RUNS", "1")
    external = scratch_suite.path / "external"
    external.mkdir()
    (external / "evidence.txt").write_text("untouched")
    external.chmod(0o500)
    scratch_suite.makepyfile(
        test_outcomes=r'''
        from pathlib import Path
        import pytest

        @pytest.fixture
        def artifacts(request, scratch_fixture, phase):
            directory = Path(str(request.getfixturevalue(scratch_fixture)))
            Path(f"record-{request.node.name}.txt").write_text(str(directory))
            if scratch_fixture in ("tmp_path", "tmpdir"):
                sibling = directory.parent / (directory.name + "-session-runtime")
                sibling.mkdir()
                (sibling / "state.txt").write_text("runtime evidence")
                Path(f"sibling-{request.node.name}.txt").write_text(str(sibling))
            protected = directory / "readonly"
            protected.mkdir()
            (protected / "evidence.txt").write_text("debug evidence")
            (protected / "external").symlink_to(Path("external").resolve(), target_is_directory=True)
            protected.chmod(0o500)
            request.addfinalizer(lambda: protected.chmod(0o700))
            if phase == "setup":
                pytest.fail("injected setup failure")
            yield directory
            assert (protected / "evidence.txt").read_text() == "debug evidence"
            if phase == "teardown":
                pytest.fail("injected teardown failure")

        @pytest.mark.parametrize("scratch_fixture", [
            "tmp_path", "tmpdir", "st_project_root", "st_pi_run_root",
        ])
        @pytest.mark.parametrize("phase", ["passed", "setup", "call", "teardown"])
        def test_outcome(artifacts, phase, monkeypatch):
            # Configuration is captured at startup, independent of test-local env changes.
            monkeypatch.setenv("GOAL_PLUS_TEST_DEBUG", "0")
            if phase == "call":
                pytest.fail("injected call failure")
        ''',
    )
    result = scratch_suite.runpytest_subprocess("-q", "-n", str(workers), "test_outcomes.py")
    result.assert_outcomes(passed=8, failed=4, errors=8)
    records = list(scratch_suite.path.glob("record-*.txt"))
    assert len(records) == 16
    for record in records:
        directory = Path(record.read_text())
        retained = debug == "1" or "[passed-" not in record.name
        assert directory.exists() == retained, record.name
        if retained:
            assert (directory / "readonly/evidence.txt").read_text() == "debug evidence"
    siblings = list(scratch_suite.path.glob("sibling-*.txt"))
    assert len(siblings) == 8
    for record in siblings:
        retained = debug == "1" or "[passed-" not in record.name
        assert Path(record.read_text()).exists() == retained, record.name
    assert external.stat().st_mode & 0o777 == 0o500
    assert (external / "evidence.txt").read_text() == "untouched"


def test_cleanup_errors_are_reported_and_leave_artifacts(scratch_suite) -> None:
    scratch_suite.makeconftest(
        (scratch_suite.path / "conftest.py").read_text()
        + '''
def pytest_sessionstart(session):
    import tests._test_tmp as temporary
    def fail_cleanup(path):
        raise PermissionError("injected cleanup failure")
    temporary._remove_directory = fail_cleanup
'''
    )
    result = scratch_suite.runpytest_subprocess("-q")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*test temporary directory cleanup failed:*injected cleanup failure*"])
    recorded = Path((scratch_suite.path / "scratch-root.txt").read_text())
    assert recorded.is_dir()
