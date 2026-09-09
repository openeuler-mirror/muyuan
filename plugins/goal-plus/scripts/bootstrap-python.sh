#!/usr/bin/env bash
# Shared installer controller. Never selects Python from the active shell.
set -euo pipefail
goal_plus_home="${GOAL_PLUS_INSTALL_HOME:-$HOME/.local/share/goal-plus}"
goal_plus_uv="${GOAL_PLUS_UV:-}"
if [[ -z "$goal_plus_uv" ]]; then
  goal_plus_uv="$(command -v uv || true)"
fi
if [[ -z "$goal_plus_uv" ]]; then
  goal_plus_uv="$goal_plus_home/bootstrap/bin/uv"
  if [[ ! -x "$goal_plus_uv" ]]; then
    mkdir -p "$goal_plus_home/bootstrap/bin"
    goal_plus_download="$(mktemp)"
    trap 'rm -f "$goal_plus_download"' EXIT
    curl --fail --location --silent --show-error https://astral.sh/uv/0.11.6/install.sh -o "$goal_plus_download"
    UV_INSTALL_DIR="$goal_plus_home/bootstrap/bin" UV_NO_MODIFY_PATH=1 sh "$goal_plus_download"
    rm -f "$goal_plus_download"
    trap - EXIT
  fi
fi
export GOAL_PLUS_UV="$goal_plus_uv"
unset PYTHONPATH PYTHONHOME VIRTUAL_ENV CONDA_PREFIX
for goal_plus_uv_setting in ${!UV_@}; do unset "$goal_plus_uv_setting"; done
export UV_PYTHON_INSTALL_DIR="$goal_plus_home/bootstrap/python"
exec "$goal_plus_uv" run --no-config --managed-python --python 3.12 --no-project "$@"
