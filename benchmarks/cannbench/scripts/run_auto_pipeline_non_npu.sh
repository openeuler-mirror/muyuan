#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MUYUAN_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
PYTHON_BIN="${PYTHON:-python3}"
CANN_BENCH_ROOT="${CANN_BENCH_ROOT:-${MUYUAN_ROOT}/benchmarks/cannbench/third_party/cann-bench}"

if [[ ! -f "${CANN_BENCH_ROOT}/src/auto_pipeline/core.py" ]]; then
  echo "CANN Bench third-party library is missing; run: git submodule update --init --recursive benchmarks/cannbench/third_party/cann-bench" >&2
  exit 2
fi

export CANN_BENCH_ROOT
export PYTHONPATH="${MUYUAN_ROOT}/benchmarks/cannbench/src:${CANN_BENCH_ROOT}/src:${MUYUAN_ROOT}/plugins/goal-plus/src${PYTHONPATH:+:${PYTHONPATH}}"
export GOAL_PLUS_EVIDENCE_ANNOTATOR_DISABLED=1
export MUYUAN_CANNBENCH_AUTO_REGISTER=1
export MUYUAN_CANNBENCH_NON_NPU_EVAL=1

cd "${MUYUAN_ROOT}"
exec "${PYTHON_BIN}" -m auto_pipeline.cli "$@"
