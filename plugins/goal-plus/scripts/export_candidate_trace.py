"""Export immutable candidate execution traces as JSON and Markdown."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from goal_plus.infrastructure.candidate_trace_export import CandidateTraceExporter  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime_root", type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--candidate-id")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    root = args.runtime_root.expanduser()
    if not root.is_dir():
        parser.error(f"runtime root does not exist: {root}")
    try:
        result = CandidateTraceExporter(root).export(
            args.run_id,
            candidate_id=args.candidate_id,
            output=args.output,
        )
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    print(result["output"])
    for candidate in result["candidates"]:
        print(
            f"  {candidate['candidate_id']}: {candidate['completeness']} "
            f"({candidate['directory']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
