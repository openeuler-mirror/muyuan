"""Read-only build identity and exact-build host launch entry point."""
from __future__ import annotations

import argparse
import importlib
import json
import platform
from pathlib import Path
import sys


def snapshot() -> dict:
    path = Path(__file__).with_name("_installation.json")
    value = json.loads(path.read_text()) if path.exists() else {"build": None, "source": str(path.parent.parent.parent)}
    value.setdefault("capabilities", [])
    return {**value, "python": sys.executable, "python_version": platform.python_version(), "module": str(Path(__file__).parent)}


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--expected-build")
    parser.add_argument("--module", choices=("goal_plus.cli", "goal_plus.pi_tool"))
    args, remaining = parser.parse_known_args()
    actual = snapshot()
    if args.module:
        if not args.expected_build or actual.get("build") != args.expected_build:
            parser.error("Goal Plus runtime build mismatch; reinstall the host package")
        sys.argv = [args.module, *remaining]
        raise SystemExit(importlib.import_module(args.module).main())
    if remaining:
        parser.error("unexpected arguments")
    print(json.dumps(actual))


if __name__ == "__main__":
    main()
