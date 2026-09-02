#!/usr/bin/env bash
set -euo pipefail

goal_plus_script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
goal_plus_python="${PYTHON:-}"

goal_plus_python_works() {
  local goal_plus_candidate="$1"
  [[ -n "$goal_plus_candidate" ]] || return 1
  "$goal_plus_candidate" -c \
    'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    >/dev/null 2>&1
}

goal_plus_shebang_is_python() {
  local goal_plus_candidate="$1"
  local goal_plus_candidate_name="${goal_plus_candidate##*/}"
  case "$goal_plus_candidate_name" in
    python|python[0-9]*|pypy|pypy[0-9]*) ;;
    *) return 1 ;;
  esac
  goal_plus_python_works "$goal_plus_candidate"
}

if [[ -n "$goal_plus_python" ]]; then
  if ! goal_plus_python_works "$goal_plus_python"; then
    echo "error: PYTHON is not a working Python 3.10+ interpreter: $goal_plus_python" >&2
    exit 1
  fi
else
  goal_plus_command="$(command -v goal-plus || true)"
  if [[ -n "$goal_plus_command" && -f "$goal_plus_command" ]]; then
    IFS= read -r goal_plus_shebang < "$goal_plus_command" || true
    case "$goal_plus_shebang" in
      '#!'/*)
        goal_plus_candidate="${goal_plus_shebang#\#!}"
        if [[ "$goal_plus_candidate" != *" "* ]] && \
          goal_plus_shebang_is_python "$goal_plus_candidate"; then
          goal_plus_python="$goal_plus_candidate"
        fi
        ;;
    esac

    if [[ -z "$goal_plus_python" ]]; then
      goal_plus_command_dir="$(CDPATH= cd -- "$(dirname -- "$goal_plus_command")" && pwd)"
      for goal_plus_candidate in \
        "$goal_plus_command_dir/python3" \
        "$goal_plus_command_dir/python"
      do
        if goal_plus_python_works "$goal_plus_candidate"; then
          goal_plus_python="$goal_plus_candidate"
          break
        fi
      done
    fi
  fi
fi

if [[ -z "$goal_plus_python" ]]; then
  goal_plus_python="$(command -v python3 || true)"
  if ! goal_plus_python_works "$goal_plus_python"; then
    echo "error: could not find a working Python 3.10+ interpreter" >&2
    exit 1
  fi
fi

exec "$goal_plus_python" "$goal_plus_script_dir/scripts/uninstall.py" "$@"
