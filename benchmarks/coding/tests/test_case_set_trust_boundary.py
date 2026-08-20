"""A case set is data the harness received, not code the harness wrote.

`export-bundle` exists to hand a case set to another person, and `_clean2026` ships with the
repository, so the JSON reaching `run` and `audit-cases` comes from outside. These tests hold
the boundary that follows from that: nothing in a case JSON reaches a shell, and the code being
graded does not get the caller's credentials.
"""

from __future__ import annotations

import json
import re

import pytest

from aibench.grading import _GRADER_ENV_ALLOWLIST, _grader_env
from aibench.io_util import repo_root, safe_command
from aibench.models import Case, RunConfig
from aibench.workspace import WorkspaceSpec

_MINIMAL_RUN = {
    "experiment_name": "e",
    "algorithm_name": "a",
    "algorithm_version": "v",
    "budget_axis": "calls",
    "budget_value": "1",
    "branches": 1,
    "max_attempts": 1,
    "max_steps": 1,
    "max_wall_time_s": 1,
    "selection_strategy": "first-submit",
    "case_set": "seed-v0",
    "benchmark_name": "b",
    "grouping": "task_type",
    "agent_config": "a.yaml",
    "model_config": "m.yaml",
}


def _seed() -> dict:
    path = repo_root() / "tests/fixtures/case_sets/seed-v0/case_001_fizzbuzz.json"
    return json.loads(path.read_text(encoding="utf-8"))


class TestACaseCommandNeverReachesAShell:
    """`grader.command` ran through `sh -c` with the caller's whole environment, so a case set
    was arbitrary code on the host of anyone who ran `audit-cases` on it."""

    @pytest.mark.parametrize(
        "command",
        [
            "python -m pytest -q; touch /tmp/x",
            "python -m pytest -q > /tmp/x",
            "python -m pytest -q && curl http://example.invalid",
            "python -m pytest -q $(id)",
            "python -m pytest -q `id`",
            "python -m pytest -q | tee /tmp/x",
            "python -m pytest -q & sleep 5",
            "python -m pytest -q\ntouch /tmp/x",
            "python -m pytest -q ~/secret",
            "python -m pytest -q *.py",
        ],
    )
    def test_shell_syntax_is_refused(self, command):
        with pytest.raises(ValueError, match="not run through a shell"):
            safe_command(command, field="grader.command")

    @pytest.mark.parametrize(
        ("command", "argv"),
        [
            ("python -m pytest -q", ["python", "-m", "pytest", "-q"]),
            ("node --test", ["node", "--test"]),
            ("python -m pytest -q test_calc.py", ["python", "-m", "pytest", "-q", "test_calc.py"]),
            ('echo "a b"', ["echo", "a b"]),
        ],
    )
    def test_the_commands_that_ship_still_parse(self, command, argv):
        assert safe_command(command, field="grader.command") == argv

    def test_an_unparseable_command_is_refused(self):
        with pytest.raises(ValueError, match="cannot be parsed"):
            safe_command('python -m pytest -q "unbalanced', field="grader.command")

    def test_loading_a_case_refuses_it(self):
        raw = _seed()
        raw["grader"]["command"] = "python -m pytest -q; touch /tmp/x"
        with pytest.raises(ValueError, match=re.escape("grader.command")):
            Case.from_dict(raw)

    def test_setup_commands_are_held_to_the_same_rule(self):
        """They run during materialization, so `audit-cases` executed them before grading."""
        with pytest.raises(ValueError, match=re.escape("workspace.setup_commands")):
            WorkspaceSpec.from_dict({"mode": "inline", "setup_commands": ["echo hi > /tmp/x"]})
        spec = WorkspaceSpec.from_dict({"mode": "inline", "setup_commands": ["echo hi"]})
        assert spec.setup_commands == ["echo hi"]


class TestValidateCasesSeesWhatTheRunningCommandsRefuse:
    """`validate-cases` executes nothing, which is what makes it the safe first command on a set
    someone gave you — so it has to refuse everything the executing commands would."""

    def test_it_reports_a_shell_command(self, tmp_path, monkeypatch):
        from aibench.cases import validate_case_set

        case_root = tmp_path / "cases"
        (case_root / "probe").mkdir(parents=True)
        raw = _seed()
        raw["grader"]["command"] = "python -m pytest -q; touch /tmp/x"
        (case_root / "probe" / "c.json").write_text(json.dumps(raw), encoding="utf-8")
        monkeypatch.setenv("AIBENCH_CASE_ROOT", str(case_root))
        errors = validate_case_set("probe")
        assert any("not run through a shell" in e for e in errors)

    def test_the_sets_that_ship_still_validate(self):
        from aibench.cases import validate_case_set

        assert validate_case_set("seed-v0") == []


class TestTheGradedCodeCannotReadTheCallersCredentials:
    """The grader is handed code the model wrote or the case set shipped, and the caller's
    environment holds `OPENAI_API_KEY` and `AIBENCH_DB_URL`."""

    def test_credentials_are_not_inherited(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-not-a-real-key")
        monkeypatch.setenv("AIBENCH_DB_URL", "mysql+pymysql://u:p@h/db")
        monkeypatch.setenv("GH_PAT", "not-a-real-token")
        env = _grader_env()
        assert "OPENAI_API_KEY" not in env
        assert "AIBENCH_DB_URL" not in env
        # A denylist of credential-shaped names would have kept this one.
        assert "GH_PAT" not in env

    def test_what_a_suite_needs_survives(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        env = _grader_env()
        assert env["PATH"] == "/usr/bin"
        assert env["PYTHONHASHSEED"] == "0"
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert set(env) <= set(_GRADER_ENV_ALLOWLIST) | {
            "PYTHONHASHSEED",
            "PYTHONDONTWRITEBYTECODE",
        }

    def test_the_run_config_can_name_an_exception(self, monkeypatch):
        monkeypatch.setenv("HF_TOKEN", "not-a-real-token")
        assert "HF_TOKEN" not in _grader_env()
        assert _grader_env(("HF_TOKEN",))["HF_TOKEN"] == "not-a-real-token"

    def test_the_exception_list_comes_from_the_run_config(self):
        cfg = RunConfig.from_dict({**_MINIMAL_RUN, "grader_env_passthrough": ["HF_TOKEN"]})
        assert cfg.grader_env_passthrough == ("HF_TOKEN",)
        assert RunConfig.from_dict({**_MINIMAL_RUN}).grader_env_passthrough == ()


class TestTheShellAdapterQuotesWhatTheCaseControls:
    """The `command_template` is the operator's and may use shell syntax — that is the adapter's
    purpose. `{case_id}` is substituted into it and comes from the case JSON."""

    def test_case_id_is_quoted(self, tmp_path):
        from aibench.agents.shell_agent import ShellAgent
        from aibench.models import AgentConfig, ModelConfig

        marker = tmp_path / "INJECTED.txt"
        agent = ShellAgent(
            AgentConfig.from_dict(
                {
                    "name": "sh",
                    "adapter": "shell",
                    "version": "1",
                    "options": {"command_template": "echo {case_id}"},
                }
            ),
            ModelConfig.from_dict({"name": "m", "model": "m", "provider": "openai_compat"}),
        )
        raw = _seed()
        # `safe_case_id` already refuses this shape; the quoting is the second layer.
        raw["case_id"] = "benign"
        case = Case.from_dict(raw)
        object.__setattr__(case, "case_id", f"benign; touch {marker}")
        ws = tmp_path / "ws"
        ws.mkdir()
        agent.run(case, ws, max_steps=1, max_wall_time_s=30)
        assert not marker.exists()
