#!/usr/bin/env bash
# Install git pre-commit hooks (ruff format/import/lint).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if command -v uv >/dev/null 2>&1; then
  # Both extras, always. A bare `uv sync --extra dev` *removes* the grading extra, and this
  # script is the README's very next line after the install command — so following the
  # README in order used to leave numpy, pandas, matplotlib, flask, requests and av
  # uninstalled and `test_every_promise_is_importable` red.
  uv sync --extra dev --extra grading
else
  python -m pip install -e ".[dev,grading]" -q
fi

git_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -n "$git_root" ] && [ "$(cd "$git_root" && pwd)" = "$ROOT" ]; then
  if command -v uv >/dev/null 2>&1; then
    uv run pre-commit install
    echo "Installed pre-commit hook via: uv run pre-commit install"
    echo "Optional full-repo check: uv run pre-commit run --all-files"
  else
    pre-commit install
    echo "Installed pre-commit hook via: pre-commit install"
  fi
else
  echo "Skipped pre-commit install: this module is nested in ${git_root:-no-git}."
  echo "The parent repository owns hooks. To install these hooks there:"
  echo "  uv run --directory \"$ROOT\" pre-commit install --config \"$ROOT/.pre-commit-config.yaml\""
fi
