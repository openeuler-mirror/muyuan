"""Thin entry point for the ``goal-plus`` console script.

Hosts spawn this process once per hook event; the MCP server stack is only
imported when MCP mode is actually requested, so hook invocations do not pay
for ``mcp``/``fastmcp`` startup. ``goal_plus.server`` remains importable as
the previous entry module.
"""

from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(prog="goal-plus")
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the Goal Plus version and exit",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="Search runtime storage directory",
    )
    parser.add_argument(
        "--goal-plus-stop-hook",
        action="store_true",
        help="Run the Goal Plus Stop hook instead of starting the MCP server",
    )
    parser.add_argument(
        "--goal-plus-host-hook",
        action="store_true",
        help="Run the Goal Plus host hook instead of starting the MCP server",
    )
    parser.add_argument("--installation-info", action="store_true", help="Print exact build and Python identity")
    parser.add_argument("--runtime-info", nargs="?", const="codex", choices=("codex", "pi"),
                        help="Print this runtime's identity and host sandbox defaults without starting MCP or a model")
    parser.add_argument("--json", action="store_true", help="Print runtime information as JSON")
    parser.add_argument("--runtime-provider", choices=("direct", "thinkthread"),
                        help="Provider to describe with --runtime-info; defaults to the current host environment")
    args = parser.parse_args()

    if args.json and not args.runtime_info:
        parser.error("--json requires --runtime-info")
    if args.runtime_provider and not args.runtime_info:
        parser.error("--runtime-provider requires --runtime-info")
    if args.runtime_info:
        import json
        from pathlib import Path
        from goal_plus.infrastructure.runtime_info import runtime_info, runtime_info_text
        from goal_plus.paths import DEFAULT_RUNTIME_ROOT

        root = Path(args.root or DEFAULT_RUNTIME_ROOT) if args.root or args.runtime_provider != "thinkthread" else None
        info = runtime_info(root, agent_harness=args.runtime_info, runtime_provider=args.runtime_provider)
        print(json.dumps(info, ensure_ascii=False) if args.json else runtime_info_text(info))
        return

    if args.installation_info:
        import json
        from goal_plus.installation import snapshot

        print(json.dumps(snapshot()))
        return

    if args.version:
        from goal_plus._version import __version__

        print(f"goal-plus {__version__}")
        return

    if args.goal_plus_stop_hook or args.goal_plus_host_hook:
        from goal_plus.goal_plus_stop_hook import main as hook_main

        raise SystemExit(hook_main())

    from goal_plus.paths import DEFAULT_RUNTIME_ROOT
    from goal_plus.server import create_codex_mcp

    create_codex_mcp(args.root or DEFAULT_RUNTIME_ROOT).run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
