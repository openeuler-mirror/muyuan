from __future__ import annotations

import hashlib
import select
import subprocess
import sys
from pathlib import Path

import pytest

from goal_plus.domain.serialization import canonical_json, sha256_text
from goal_plus.infrastructure.clock import parse_utc_timestamp, utc_timestamp_from_epoch
from goal_plus.infrastructure.state import files


def test_json_wire_format_and_atomic_replacement(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "record.json"
    files.write_json(target, {"old": True})
    with target.open() as previous:
        files.write_json(target, {"z": "\u4e2d", "a": 1})
        assert previous.read() == '{\n  "old": true\n}\n'
    assert target.read_text() == '{\n  "a": 1,\n  "z": "\\u4e2d"\n}\n'
    assert files.load_json(target) == {"z": "\u4e2d", "a": 1}
    assert list(target.parent.iterdir()) == [target]


def test_failed_serialization_preserves_previous_file(tmp_path: Path) -> None:
    target = tmp_path / "record.json"
    files.write_json(target, {"settled": True})
    with pytest.raises(TypeError):
        files.write_json(target, {"bad": object()})
    assert files.load_json(target) == {"settled": True}


def test_text_and_identity_formats(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "text.txt"
    files.write_text(target, "first\n")
    files.write_text(target, "second\n")
    assert target.read_bytes() == b"second\n"
    assert files.sha256_file(target) == hashlib.sha256(b"second\n").hexdigest()
    assert sha256_text("second\n") == files.sha256_file(target)
    assert canonical_json({"z": "\u4e2d", "a": 1}) == '{"a":1,"z":"\\u4e2d"}'
    assert utc_timestamp_from_epoch(0) == "1970-01-01T00:00:00Z"
    assert parse_utc_timestamp("2026-09-05T00:00:00Z") == 1788566400.0
    with pytest.raises(ValueError):
        parse_utc_timestamp("invalid")


@pytest.mark.parametrize("directory_lock", [False, True])
def test_lock_excludes_other_processes_and_releases_after_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory_lock: bool
) -> None:
    lock = tmp_path / "state.lock"
    if directory_lock:
        monkeypatch.setattr(files, "fcntl", None)
    script = """
import sys
from pathlib import Path
from goal_plus.infrastructure.state import files
if sys.argv[2] == 'True':
    files.fcntl = None
print('ready', flush=True)
with files.exclusive_file_lock(Path(sys.argv[1])):
    print('acquired', flush=True)
"""
    process = None
    try:
        with pytest.raises(RuntimeError, match="release"):
            with files.exclusive_file_lock(lock):
                process = subprocess.Popen(
                    [sys.executable, "-c", script, str(lock), str(directory_lock)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                assert select.select([process.stdout], [], [], 15)[0]
                assert process.stdout.readline() == "ready\n"
                assert not select.select([process.stdout], [], [], 0.1)[0]
                raise RuntimeError("release")
        output, error = process.communicate(timeout=15)
        assert process.returncode == 0, error
        assert output == "acquired\n"
        if directory_lock:
            assert not lock.with_suffix(".lock.dir").exists()
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate()
