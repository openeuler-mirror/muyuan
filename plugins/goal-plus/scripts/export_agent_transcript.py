"""Export human-readable, line-by-line transcripts of Goal Plus agent sessions.

Reads local native histories (Pi session JSONL, Codex rollouts, ThinkThread
diagnostic archives and controller journals) under a runtime root and renders a
plain-text transcript plus a self-contained HTML view. Content-complete and
debug-only: output may contain prompts, tool arguments and outputs, so keep it
under host-logs/ and never commit it.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from goal_plus.infrastructure.agent_transcript import (  # noqa: E402
    export_transcripts,
    read_codex_transcript,
    read_pi_transcript,
    write_transcript_files,
)
from goal_plus.projections.agent_transcript import AgentTranscriptSession  # noqa: E402


def _single_session(args: argparse.Namespace) -> AgentTranscriptSession:
    if args.pi:
        parsed = read_pi_transcript(Path(args.pi).expanduser(), leaf_id=args.leaf_id)
        return AgentTranscriptSession(session_id=parsed.session_id or Path(args.pi).stem,
                                      harness="pi", role=args.role,
                                      source_path=str(Path(args.pi).expanduser()),
                                      events=parsed.events, warnings=parsed.warnings)
    parsed = read_codex_transcript(Path(args.codex).expanduser())
    return AgentTranscriptSession(session_id=parsed.session_id or Path(args.codex).stem,
                                  harness="codex", role=args.role,
                                  source_path=str(Path(args.codex).expanduser()),
                                  events=parsed.events, warnings=parsed.warnings)


def _source_fingerprint(root: Path) -> dict[str, float]:
    fingerprint: dict[str, float] = {}
    for pattern in ("host-logs/session-diagnostics/**/*.json*",
                    "host-logs/inference-transcripts/**/*",
                    "runs/*/agent_sessions/*.json",
                    "host-scopes/*/thinkthread-sessions/*.json",
                    "host-logs/codex-time-advisory/sent/*.json"):
        for path in root.glob(pattern):
            try:
                fingerprint[str(path)] = path.stat().st_mtime
            except OSError:
                continue
    return fingerprint


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--pi", metavar="FILE", help="Read one raw Pi session JSONL directly")
    source.add_argument("--codex", metavar="FILE", help="Read one raw Codex rollout JSONL directly")
    parser.add_argument("root", nargs="?", type=Path, help="Runtime root (default .gp) to collect all sessions from")
    parser.add_argument("--leaf-id", help="Pi branch leaf; defaults to the last persisted entry")
    parser.add_argument("--role", default="session", help="Role label for --pi/--codex single-file exports")
    parser.add_argument("--output", type=Path, help="Output directory (default <root>/host-logs/agent-transcript)")
    parser.add_argument("--max-chars", type=int, default=20000, help="Per-event content cap in the HTML view")
    parser.add_argument("--gap-seconds", type=float, default=120.0,
                        help="Mark inactivity gaps of at least this many seconds (0 disables)")
    parser.add_argument("--no-jsonl", action="store_true", help="Skip the flat events.jsonl export")
    parser.add_argument("--watch", action="store_true", help="Re-export whenever runtime sources change")
    parser.add_argument("--interval", type=float, default=5.0, help="Watch poll interval in seconds")
    args = parser.parse_args(argv)

    if args.pi or args.codex:
        if args.watch:
            parser.error("--watch requires a runtime root, not a single file")
        session = _single_session(args)
        output = args.output or Path.cwd() / "agent-transcript"
        summary = write_transcript_files(output, [session], html_max_chars=args.max_chars,
                                         gap_seconds=args.gap_seconds, include_jsonl=not args.no_jsonl)
    else:
        root = (args.root or Path(".gp")).expanduser()
        if not root.exists():
            parser.error(f"runtime root does not exist: {root}")
        if not args.output and not (root / "host-logs").is_dir() and root.name not in (".gp", ".search"):
            parser.error(f"refusing to treat {root} as a runtime root; pass --output or use .gp")
        options = {"output": args.output, "html_max_chars": args.max_chars,
                   "gap_seconds": args.gap_seconds, "include_jsonl": not args.no_jsonl}
        summary = export_transcripts(root, **options)
        if not summary["sessions"] and not args.watch:
            print("no readable session histories found", file=sys.stderr)
            return 2
        if args.watch:
            print(f"watching {root}; Ctrl-C to stop", file=sys.stderr)
            fingerprint = _source_fingerprint(root)
            try:
                while True:
                    time.sleep(max(1.0, args.interval))
                    current = _source_fingerprint(root)
                    if current == fingerprint:
                        continue
                    fingerprint = current
                    summary = export_transcripts(root, **options)
                    print(f"re-exported {len(summary['sessions'])} session(s) -> {summary['output']}",
                          file=sys.stderr)
            except KeyboardInterrupt:
                return 0
    print(summary["output"])
    for item in summary["sessions"]:
        print(f"  {item['name']}: {item['events']} events ({item['role']}/{item['harness']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
