#!/usr/bin/env bash
set -euo pipefail
goal_plus_package="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
if [[ ! -f "$goal_plus_package/.goal-plus-python" || ! -f "$goal_plus_package/.goal-plus-build" ]]; then
  echo 'Goal Plus runtime is not prepared. Run ./install.sh --codex from the source checkout.' >&2
  exit 1
fi
IFS= read -r goal_plus_python < "$goal_plus_package/.goal-plus-python"
IFS= read -r goal_plus_build < "$goal_plus_package/.goal-plus-build"
exec "$goal_plus_python" -I -m goal_plus.installation --expected-build "$goal_plus_build" --module goal_plus.cli "$@"
