"""Controlled native process inputs shared by session and adapter tests."""

import os
from pathlib import Path
import sys

import pytest

from goal_plus.execution.native_session import CodexSession, PiSession
from goal_plus.execution.session_control import NativeSessionControl
from goal_plus.ports.process import ProcessRegistration
from goal_plus.ports.session import NativeSessionInput, SessionProcessInput


@pytest.fixture
def session_inputs(tmp_path):
    def build(agent_harness, scenario="normal"):
        marker = tmp_path / "executed"
        registered = []
        finished = []

        def register(pid):
            assert not marker.exists(), "native command ran before resource registration"
            registered.append(pid)
            return "token"

        process = SessionProcessInput(
            (sys.executable, str(Path(__file__).parent / "fixtures/native_session_rpc.py"),
             agent_harness, scenario, str(marker)), tmp_path,
            {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        )
        return (NativeSessionInput(agent_harness, process),
                ProcessRegistration(register, finished.append), registered, finished)
    return build


@pytest.fixture(params=[("codex", CodexSession), ("pi", PiSession)])
def controlled_session(request, session_inputs):
    controllers = []

    def create(scenario="normal"):
        agent_harness, actor = request.param
        config, registration, _, finished = session_inputs(agent_harness, scenario)
        control = NativeSessionControl(actor(config, registration, timeout=5))
        controllers.append(control)
        return control, finished

    yield create
    for control in controllers:
        control.close()
