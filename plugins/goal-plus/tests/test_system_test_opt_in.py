"""Exercise real collection hooks with inert cases, never real host agents."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]


@pytest.fixture
def gated_suite(pytester: pytest.Pytester) -> pytest.Pytester:
    root = Path(__file__).resolve().parents[1]
    pytester.makeini(
        """
        [pytest]
        markers =
            st: system test
            st_codex: Codex
            st_pi_rpc: Pi RPC
            st_pi: Pi native
            st_pi_thinkthread: ThinkThread
            st_future: future system test
            fast: host-free test
            linux_procfs: Linux process observation
        """
    )
    pytester.makeconftest(
        f"""
        import sys
        from pathlib import Path
        sys.path.insert(0, {str(root)!r})
        pytest_plugins = [
            "tests.conftest", "tests.st.conftest",
            "tests.st_pi.conftest", "tests.st_thinkthread.conftest",
        ]

        def pytest_sessionstart(session):
            import tests.st.conftest as common
            import tests.st_pi.conftest as native

            def checked(name):
                with Path("preflight.txt").open("a") as stream:
                    stream.write(name + "\\n")
                return []

            common._host_checks = checked
            native.shutil.which = lambda name: checked(name) or "/inert/host"
        """
    )
    pytester.makepyfile(
        test_cases=r"""
        import pytest

        @pytest.fixture
        def host():
            from pathlib import Path
            with Path("executed.txt").open("a") as stream:
                stream.write("host\n")

        @pytest.mark.st
        @pytest.mark.st_codex
        def test_codex(host): pass

        @pytest.mark.st
        @pytest.mark.st_pi_rpc
        def test_rpc(host): pass

        @pytest.mark.st_pi
        def test_native(host): pass

        @pytest.mark.st
        @pytest.mark.st_pi_thinkthread
        def test_thinkthread(host): pass

        @pytest.mark.fast
        def test_fast(): pass
        """
    )
    return pytester


@pytest.mark.parametrize(
    "selection",
    [(), ("-m", "st"), ("-k", "codex"),
     ("-m", "not st and not st_pi"), ("test_cases.py::test_codex",)],
)
def test_selection_without_opt_in_never_checks_or_runs_hosts(
    gated_suite: pytest.Pytester, selection: tuple[str, ...]
) -> None:
    result = gated_suite.runpytest_subprocess("-q", *selection)
    assert result.ret == pytest.ExitCode.OK
    assert not (gated_suite.path / "preflight.txt").exists()
    assert not (gated_suite.path / "executed.txt").exists()


@pytest.mark.parametrize(
    ("expression", "checks"),
    [
        ("st_codex", ["codex"]),
        ("st_pi_rpc", ["pi-rpc"]),
        ("st_pi", ["pi"]),
        ("st_pi_thinkthread", ["tt", "pi", "node"]),
        ("not st and not st_pi", []),
    ],
)
def test_opt_in_preflights_only_selected_hosts(
    gated_suite: pytest.Pytester, expression: str, checks: list[str]
) -> None:
    result = gated_suite.runpytest_subprocess(
        "-q", "--run-system-tests", "-m", expression
    )
    result.assert_outcomes(passed=1, deselected=4)
    preflight = gated_suite.path / "preflight.txt"
    assert (preflight.read_text().splitlines() if preflight.exists() else []) == checks
    assert (gated_suite.path / "executed.txt").exists() == bool(checks)


def test_collection_is_host_free_even_with_opt_in(gated_suite: pytest.Pytester) -> None:
    result = gated_suite.runpytest_subprocess("-q", "--run-system-tests", "--collect-only")
    assert result.ret == pytest.ExitCode.OK
    assert not (gated_suite.path / "preflight.txt").exists()
    assert not (gated_suite.path / "executed.txt").exists()


@pytest.mark.parametrize("nested", [False, True])
def test_invalid_system_test_execution_fails_before_preflight(
    gated_suite: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, nested: bool
) -> None:
    if nested:
        monkeypatch.setenv("GOAL_PLUS_ST_ACTIVE", "inert-opt-in-test")
    result = gated_suite.runpytest_subprocess(
        "-q", "--run-system-tests", "-n", "0" if nested else "2"
    )
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert not (gated_suite.path / "preflight.txt").exists()
    assert not (gated_suite.path / "executed.txt").exists()


def test_new_system_marker_is_gated_outside_host_directories(
    gated_suite: pytest.Pytester,
) -> None:
    gated_suite.makepyfile(
        test_future="""
        import pytest
        @pytest.mark.st_future
        def test_future():
            pytest.fail("must be gated before execution")
        """
    )
    result = gated_suite.runpytest_subprocess("-q", "test_future.py")
    result.assert_outcomes(skipped=1)
    assert not (gated_suite.path / "preflight.txt").exists()


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_linux_procfs_marker_gates_fixtures_only_on_other_platforms(
    gated_suite: pytest.Pytester, platform: str,
) -> None:
    conftest = gated_suite.path / "conftest.py"
    with conftest.open("a") as stream:
        stream.write(
            "\ndef pytest_configure(config):\n"
            "    from types import SimpleNamespace\n"
            "    import tests.conftest as gates\n"
            f"    gates.sys = SimpleNamespace(platform={platform!r})\n"
        )
    gated_suite.makepyfile(
        test_platform="""
        from pathlib import Path
        import pytest

        @pytest.fixture
        def observed_process():
            Path("process-observed.txt").touch()

        @pytest.mark.linux_procfs
        def test_process(observed_process): pass

        def test_pure(): pass
        """
    )
    result = gated_suite.runpytest_subprocess("-q", "test_platform.py")
    result.assert_outcomes(passed=2 if platform == "linux" else 1, skipped=0 if platform == "linux" else 1)
    assert (gated_suite.path / "process-observed.txt").exists() == (platform == "linux")
