#!/usr/bin/env bash
# Same options as install.py: --pi / --codex / --project / --dry-run.
set -euo pipefail
script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec python3 "$script_dir/install.py" "$@"
