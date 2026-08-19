from __future__ import annotations

import os
from pathlib import Path


CANNBENCH_DIR = Path(__file__).resolve().parents[2]
MUYUAN_ROOT = CANNBENCH_DIR.parents[1]
CANN_BENCH_ROOT = Path(
    os.environ.get(
        "CANN_BENCH_ROOT",
        CANNBENCH_DIR / "third_party" / "cann-bench",
    )
).expanduser().resolve()
GOAL_PLUS_ROOT = MUYUAN_ROOT / "plugins" / "goal-plus"
RESOURCES_DIR = Path(__file__).resolve().parent / "resources"


def resolve_path(value: str | Path, *, config_path: Path | None = None) -> Path:
    """Resolve integration paths consistently from config, Muyuan, then CANN Bench."""

    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    bases = []
    if config_path is not None:
        bases.append(config_path.parent)
    bases.extend((MUYUAN_ROOT, CANN_BENCH_ROOT))
    for base in bases:
        resolved = (base / candidate).resolve()
        if resolved.exists():
            return resolved
    return (MUYUAN_ROOT / candidate).resolve()
