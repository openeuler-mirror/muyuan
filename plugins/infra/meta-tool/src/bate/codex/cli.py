"""Codex integration commands. Offline import/review need only stdlib."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from ..io import load_specs, write_json
from ..naming import template_fingerprint
from .traces import import_files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bate-codex", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    installer = sub.add_parser("install", help="Install the project-local meta-tool skill and MCP plugin")
    installer.add_argument("--project", type=Path, required=True)
    installer.add_argument("--config", type=Path)
    installer.add_argument("--trace-file", type=Path, action="append", default=[])
    imp = sub.add_parser("import-traces", help="Import explicitly supplied Codex JSONL files")
    imp.add_argument("--input", type=Path, nargs="+", required=True)
    imp.add_argument("--out", type=Path, required=True)
    review = sub.add_parser("review", help="Print executable contracts and fingerprints; does not approve")
    review.add_argument("--catalog", type=Path, required=True)
    for name in ("serve", "check"):
        command = sub.add_parser(name, help="Run MCP stdio server" if name == "serve"
                                 else "Connect upstreams and report catalog readiness without executing tools")
        command.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            from .install import install
            print(json.dumps(install(args.project, config=args.config, trace_files=args.trace_file),
                             ensure_ascii=False, indent=2))
            return 0
        if args.command == "import-traces":
            if args.out.resolve() in {path.resolve() for path in args.input}:
                raise ValueError("Output must not overwrite an input trajectory")
            imported = import_files(args.input)
            write_json(args.out, imported.to_dict())
            print(json.dumps({"traces": len(imported.traces),
                              "diagnostics": dict(imported.diagnostics)}, ensure_ascii=False))
            return 0 if imported.traces else 2
        if args.command == "review":
            specs = load_specs(args.catalog)
            print(json.dumps({"review_required": True,
                              "templates": [{"fingerprint": template_fingerprint(spec),
                                             "spec": spec.to_dict()} for spec in specs]},
                             ensure_ascii=False, indent=2))
            return 0
        from .server import run_server
        report = asyncio.run(run_server(args.config, check=args.command == "check"))
        if report is not None:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if (report["active"] or report["mode"] == "online") and not report["rejected"] else 2
        return 0
    except ModuleNotFoundError as error:
        if error.name in {"mcp", "jsonschema"}:
            print("Codex MCP transport needs: python -m pip install '.[codex]'", file=sys.stderr)
        else:
            print(f"Missing dependency: {error.name}", file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError) as error:
        print(f"bate-codex: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        # MCP/anyio wrap connection and timeout failures in exception groups.
        # Keep stderr useful without echoing upstream credentials/arguments.
        print(f"bate-codex: MCP connection/session failed ({type(error).__name__}); "
              "check upstream command, cwd, environment and timeout", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
