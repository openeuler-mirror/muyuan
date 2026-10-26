"""Installed, stdlib-only selector for each host's verified runtime binding."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser(prog="goal-plus-info")
    parser.add_argument("host", choices=("codex", "pi"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--root")
    parser.add_argument("--thinkthread", action="store_true", help="Inspect the managed goal-plus-HOST ThinkThread Profile binding")
    args = parser.parse_args()
    # This file lives beside the installed launcher, outside any removable release.
    install_home = Path(__file__).resolve().parents[1]
    binding_host = "thinkthread" if args.thinkthread else args.host
    if args.thinkthread:
        config_home = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser().resolve()
        config = (config_home / "thinkthread" / "profiles" / f"goal-plus-{args.host}.toml").resolve()
    else:
        config = Path(os.environ.get("CODEX_HOME", "~/.codex") if args.host == "codex"
                      else os.environ.get("PI_CODING_AGENT_DIR") or "~/.pi/agent").expanduser().resolve()
    key = hashlib.sha256(str(config).encode()).hexdigest()[:16]
    binding_path = install_home / "bindings" / f"{binding_host}-{key}.json"
    try:
        binding = json.loads(binding_path.read_text())
        if (binding.get("schema") != "goal-plus.managed-runtime.v1"
                or binding.get("host") != binding_host or Path(binding["config"]).expanduser().resolve() != config):
            raise ValueError("invalid host binding")
        package = Path(binding["package"])
        receipt = json.loads((package / ".goal-plus-runtime.json").read_text())
        if (receipt.get("schema") != "goal-plus.managed-runtime.v1"
                or Path(receipt["package"]).resolve() != package.resolve()
                or not Path(receipt["python"]).is_absolute() or not receipt.get("build")):
            raise ValueError("invalid runtime receipt")
        # Select the registered release, then let its existing launcher verify the build.
        command = [receipt["python"], "-I", "-m", "goal_plus.installation",
                   "--expected-build", receipt["build"], "--module", "goal_plus.cli",
                   "--runtime-info", args.host,
                   "--runtime-provider", "thinkthread" if args.thinkthread else "direct"]
        if args.root:
            command.extend(["--root", args.root])
        if args.json:
            command.append("--json")
        os.execv(command[0], command)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Cannot inspect Goal Plus for {args.host} ({config}): {exc}. "
              f"Run install.sh --{args.host}{' --thinkthread' if args.thinkthread else ''} "
              "from the source checkout.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
