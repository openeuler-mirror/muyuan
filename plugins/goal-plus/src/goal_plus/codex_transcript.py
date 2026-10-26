from __future__ import annotations

import json
from itertools import islice
from pathlib import Path
from typing import Any, Iterator


# Codex currently writes session_meta as its first transcript record. Scan a
# bounded header so hooks remain cheap while tolerating malformed prefix lines
# and compatible header records added by newer hosts.
CODEX_TRANSCRIPT_HEADER_MAX_LINES = 32


def iter_codex_transcript_header(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8") as stream:
            for line in islice(stream, CODEX_TRANSCRIPT_HEADER_MAX_LINES):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    yield event
    except (OSError, UnicodeDecodeError):
        return


def read_codex_session_meta(path: Path) -> dict[str, Any] | None:
    for event in iter_codex_transcript_header(path):
        payload = event.get("payload")
        if event.get("type") == "session_meta" and isinstance(payload, dict):
            return payload
    return None
