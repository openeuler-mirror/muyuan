#!/usr/bin/env bash
set -euo pipefail
goal_plus_script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec /bin/bash "$goal_plus_script_dir/scripts/bootstrap-python.sh" "$goal_plus_script_dir/scripts/uninstall.py" "$@"
