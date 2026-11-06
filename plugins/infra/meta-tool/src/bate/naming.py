"""Short callable identities derived from executable template content.

Candidate ranks and support are observations, not identities. Keep those and
the old descriptive name in metadata, out of the callable name and its digest.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace

NAMING_VERSION = "stable-short-v1"


def template_fingerprint(spec) -> str:
    payload = spec.to_dict()
    identity = {
        "version": NAMING_VERSION,
        "input_schema": payload["input_schema"],
        "steps": payload["steps"],
        "output": payload["output"],
    }
    encoded = json.dumps(
        identity, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def short_callable_name(fingerprint: str) -> str:
    # 48 bits is ample for a small catalog; full-digest collision checks below
    # still fail closed. Length is always 15, below compiler/provider limits.
    return "mt_" + fingerprint[:12]


def assign_short_names(specs: list) -> list:
    """Rename a catalog without modifying schemas, steps, output, or guards."""
    assigned: dict[str, str] = {}
    result = []
    for spec in specs:
        fingerprint = template_fingerprint(spec)
        name = short_callable_name(fingerprint)
        if name in assigned and assigned[name] != fingerprint:
            raise ValueError(f"Short callable name collision: {name}")
        assigned[name] = fingerprint
        metadata = {
            **dict(spec.metadata),
            "naming_version": NAMING_VERSION,
            "legacy_name": spec.metadata.get("legacy_name", spec.name),
            "callable_name": name,
            "template_fingerprint": fingerprint,
        }
        result.append(replace(spec, name=name, metadata=metadata))
    return result
