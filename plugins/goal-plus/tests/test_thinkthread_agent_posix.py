from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from goal_plus.thinkthread_agent_posix import (
    AGENT_POSIX_CONTRACT_FINGERPRINT,
    AgentPosixBridgeError,
    AgentPosixSdkClient,
)


def test_sdk_bridge_preflight_uses_official_contract_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bridge = tmp_path / "bridge.mjs"
    bridge.write_text("// test bridge\n", encoding="utf-8")
    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["request"] = json.loads(kwargs["input"])
        seen["environment"] = kwargs["env"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "result": {
                        "contractFingerprint": AGENT_POSIX_CONTRACT_FINGERPRINT,
                        "controlProtocolVersion": 2,
                        "methods": ["self"],
                    },
                }
            ),
            stderr="",
        )

    monkeypatch.setattr("goal_plus.thinkthread_agent_posix.subprocess.run", fake_run)
    result = AgentPosixSdkClient(
        bridge_path=bridge,
        environment={
            "NODE_OPTIONS": "--import=/tmp/pi-loader.mjs",
            "THINKTHREAD_CONTROL_SOCKET": "/tmp/control.sock",
        },
    ).preflight()

    assert seen["request"] == {"operation": "bridge.meta", "params": {}}
    assert seen["environment"] == {
        "THINKTHREAD_CONTROL_SOCKET": "/tmp/control.sock"
    }
    assert result["contractFingerprint"] == AGENT_POSIX_CONTRACT_FINGERPRINT


def test_sdk_bridge_invalid_json_surfaces_bounded_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bridge = tmp_path / "bridge.mjs"
    bridge.write_text("// test bridge\n", encoding="utf-8")
    monkeypatch.setattr(
        "goal_plus.thinkthread_agent_posix.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=9,
            stdout="",
            stderr="x" * 2100 + "node loader rejected",
        ),
    )

    with pytest.raises(AgentPosixBridgeError) as captured:
        AgentPosixSdkClient(bridge_path=bridge).invoke("bridge.meta")

    assert "node loader rejected" in str(captured.value)
    assert "x" * 2001 not in str(captured.value)


def test_sdk_bridge_preserves_completion_unknown(monkeypatch, tmp_path: Path) -> None:
    bridge = tmp_path / "bridge.mjs"
    bridge.write_text("// test bridge\n", encoding="utf-8")
    monkeypatch.setattr(
        "goal_plus.thinkthread_agent_posix.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout=json.dumps(
                {
                    "ok": False,
                    "error": {
                        "name": "TransportError",
                        "message": "socket closed",
                        "category": "transport",
                        "delivery": "completion_unknown",
                    },
                }
            ),
            stderr="",
        ),
    )

    with pytest.raises(AgentPosixBridgeError) as captured:
        AgentPosixSdkClient(bridge_path=bridge).invoke("fs.snapshot.create")

    assert captured.value.completion_unknown is True


def test_snapshot_diff_all_uses_sdk_page_limit_and_cursor(monkeypatch, tmp_path: Path) -> None:
    bridge = tmp_path / "bridge.mjs"
    bridge.write_text("// test bridge\n", encoding="utf-8")
    client = AgentPosixSdkClient(bridge_path=bridge)
    calls: list[tuple[str, dict]] = []

    def fake_invoke(operation: str, params: dict):
        calls.append((operation, params))
        if len(calls) == 1:
            return {
                "changes": [{"path": "first.py"}],
                "hasMore": True,
                "nextCursor": "cursor-1",
            }
        return {
            "changes": [{"path": "second.py"}],
            "hasMore": False,
            "nextCursor": None,
        }

    monkeypatch.setattr(client, "invoke", fake_invoke)

    assert client.snapshot_diff_all("fsnap-base", "fsnap-target") == [
        {"path": "first.py"},
        {"path": "second.py"},
    ]
    assert calls == [
        (
            "fs.snapshot.diff",
            {
                "baseSnapshotId": "fsnap-base",
                "targetSnapshotId": "fsnap-target",
                "limit": 256,
            },
        ),
        (
            "fs.snapshot.diff",
            {
                "baseSnapshotId": "fsnap-base",
                "targetSnapshotId": "fsnap-target",
                "limit": 256,
                "cursor": "cursor-1",
            },
        ),
    ]


def test_snapshot_read_file_respects_sdk_pread_length_limit(
    monkeypatch, tmp_path: Path
) -> None:
    bridge = tmp_path / "bridge.mjs"
    bridge.write_text("// test bridge\n", encoding="utf-8")
    client = AgentPosixSdkClient(bridge_path=bridge)
    payload = b"x" * (64 * 1024 + 7)
    calls: list[dict] = []

    def fake_invoke(operation: str, params: dict):
        assert operation == "fs.snapshot.pread"
        calls.append(params)
        offset = params["offset"]
        length = params["length"]
        assert 1 <= length <= 64 * 1024
        chunk = payload[offset : offset + length]
        return {
            "offset": offset,
            "dataBase64": base64.b64encode(chunk).decode("ascii"),
            "bytesRead": len(chunk),
            "eof": offset + len(chunk) >= len(payload),
        }

    monkeypatch.setattr(client, "invoke", fake_invoke)

    assert client.snapshot_read_file("fsnap-1", "model.py") == payload
    assert [call["length"] for call in calls] == [64 * 1024, 64 * 1024]


@pytest.mark.parametrize(
    ("method", "collection_key"),
    [
        ("snapshot_diff_all", "changes"),
        ("snapshot_readdir_all", "entries"),
    ],
)
def test_snapshot_pagination_rejects_non_object_items(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    method: str,
    collection_key: str,
) -> None:
    bridge = tmp_path / "bridge.mjs"
    bridge.write_text("// test bridge\n", encoding="utf-8")
    client = AgentPosixSdkClient(bridge_path=bridge)
    monkeypatch.setattr(
        client,
        "invoke",
        lambda *_args, **_kwargs: {
            collection_key: [{"path": "valid.py"}, "invalid"],
            "hasMore": False,
        },
    )

    with pytest.raises(AgentPosixBridgeError, match="non-object"):
        if method == "snapshot_diff_all":
            client.snapshot_diff_all("fsnap-base", "fsnap-target")
        else:
            client.snapshot_readdir_all("fsnap-target", ".")


@pytest.mark.parametrize(
    "response",
    [
        {
            "offset": 1,
            "dataBase64": base64.b64encode(b"abc").decode("ascii"),
            "bytesRead": 3,
            "eof": True,
        },
        {
            "offset": 0,
            "dataBase64": base64.b64encode(b"abc").decode("ascii"),
            "bytesRead": 2,
            "eof": True,
        },
        {
            "offset": 0,
            "dataBase64": base64.b64encode(b"abc").decode("ascii"),
            "bytesRead": 3,
            "eof": "true",
        },
    ],
)
def test_snapshot_read_file_rejects_inconsistent_pread_dto(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    response: dict[str, object],
) -> None:
    bridge = tmp_path / "bridge.mjs"
    bridge.write_text("// test bridge\n", encoding="utf-8")
    client = AgentPosixSdkClient(bridge_path=bridge)
    monkeypatch.setattr(client, "invoke", lambda *_args, **_kwargs: response)

    with pytest.raises(AgentPosixBridgeError):
        client.snapshot_read_file("fsnap-1", "model.py")


def test_snapshot_pagination_rejects_invalid_flags_and_repeated_cursor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bridge = tmp_path / "bridge.mjs"
    bridge.write_text("// test bridge\n", encoding="utf-8")
    client = AgentPosixSdkClient(bridge_path=bridge)
    monkeypatch.setattr(
        client,
        "invoke",
        lambda *_args, **_kwargs: {"changes": [], "hasMore": 1},
    )
    with pytest.raises(AgentPosixBridgeError, match="hasMore"):
        client.snapshot_diff_all("fsnap-base", "fsnap-target")

    monkeypatch.setattr(
        client,
        "invoke",
        lambda *_args, **_kwargs: {
            "entries": [],
            "hasMore": True,
            "nextCursor": "same",
        },
    )
    with pytest.raises(AgentPosixBridgeError, match="new nextCursor"):
        client.snapshot_readdir_all("fsnap-target", ".")
