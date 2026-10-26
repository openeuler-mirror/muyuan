"""Offline acceptance evidence readers must follow native session layouts."""

import json

import pytest

from tests.st_thinkthread.test_codex_circle_main import _last_codex_assistant_text


@pytest.mark.parametrize("session_home", ["sessions", "home/sessions"])
def test_codex_circle_reads_final_reply_from_native_session_layout(tmp_path, session_home):
    directory = tmp_path / "instances/tt-root/thinkthreads/tt-root/session" / session_home
    directory.mkdir(parents=True)
    rollout = directory / "rollout-test.jsonl"
    rollout.write_text(json.dumps({
        "type": "response_item",
        "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "Applied and verified."}],
        },
    }) + "\n")
    assert _last_codex_assistant_text(tmp_path) == "Applied and verified."
