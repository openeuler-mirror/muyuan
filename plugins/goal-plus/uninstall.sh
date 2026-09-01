#!/usr/bin/env bash
set -euo pipefail

goal_plus_script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
goal_plus_python="${PYTHON:-}"

if [[ -z "$goal_plus_python" ]]; then
  goal_plus_command="$(command -v goal-plus || true)"
  if [[ -n "$goal_plus_command" ]]; then
    IFS= read -r goal_plus_shebang < "$goal_plus_command" || true
    case "$goal_plus_shebang" in
      '#!'/*)
        goal_plus_candidate="${goal_plus_shebang#\#!}"
        if [[ "$goal_plus_candidate" != *" "* && -x "$goal_plus_candidate" ]]; then
          goal_plus_python="$goal_plus_candidate"
        fi
        ;;
    esac
  fi
fi

if [[ -z "$goal_plus_python" ]]; then
  goal_plus_python="python3"
fi

exec "$goal_plus_python" "$goal_plus_script_dir/scripts/uninstall.py" "$@"
