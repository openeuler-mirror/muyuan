#!/usr/bin/env bash
# Launch a Goal Plus task with a live host-side transcript watcher.
#
# With GOAL_PLUS_DEBUG=1 this wrapper starts the task, locates its runtime
# root, and keeps `export_agent_transcript.py --watch` running alongside so
# full-content HTML/JSONL snapshots refresh in real time — the only reliable
# way to preserve TT child thinking and native tool bodies, which live only
# until child release and are invisible to in-sandbox processes.
#
# Without GOAL_PLUS_DEBUG=1 the command runs unchanged.
#
# Usage:
#   GOAL_PLUS_DEBUG=1 scripts/run-with-transcript.sh tt run --profile goal-plus-pi --fs <ws> -- --model zai/glm-5.3-flash --thinking low
#   GOAL_PLUS_DEBUG=1 scripts/run-with-transcript.sh pi -a --model zai/glm-5.3-flash --thinking low -p "$(cat prompt.md)"
#   GOAL_PLUS_DEBUG=1 scripts/run-with-transcript.sh codex ...
set -euo pipefail

GP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WATCH_INTERVAL="${GOAL_PLUS_TRANSCRIPT_INTERVAL:-5}"

if [ "${GOAL_PLUS_DEBUG:-0}" != "1" ] || [ $# -eq 0 ]; then
    exec "$@"
fi

TASK_PID=""
WATCH_PID=""
RUNTIME_ROOT=""

cleanup() {
    [ -n "$WATCH_PID" ] && kill "$WATCH_PID" 2>/dev/null || true
}
trap 'cleanup; [ -n "$TASK_PID" ] && kill "$TASK_PID" 2>/dev/null || true' INT TERM

start_watcher() {
    if [ -z "$RUNTIME_ROOT" ]; then
        return
    fi
    echo "[run-with-transcript] watching $RUNTIME_ROOT (interval ${WATCH_INTERVAL}s)" >&2
    PYTHONPATH="$GP_DIR/src" python3 "$GP_DIR/scripts/export_agent_transcript.py" \
        "$RUNTIME_ROOT" --watch --interval "$WATCH_INTERVAL" >&2 &
    WATCH_PID=$!
}

final_export() {
    if [ -n "$RUNTIME_ROOT" ]; then
        PYTHONPATH="$GP_DIR/src" python3 "$GP_DIR/scripts/export_agent_transcript.py" \
            "$RUNTIME_ROOT" >&2 || true
    fi
}

"$@" &
TASK_PID=$!

if [ "$1" = "tt" ]; then
    # TT keeps its runtime root inside a fresh managed instance; wait for a
    # NEW one to appear (fall back to the newest after a grace period).
    BASELINE="$(ls ~/.local/state/thinkthread/instances 2>/dev/null | sort || true)"
    for _ in $(seq 1 120); do
        kill -0 "$TASK_PID" 2>/dev/null || break
        NEW="$(comm -13 <(echo "$BASELINE") <(ls ~/.local/state/thinkthread/instances 2>/dev/null | sort) | head -1)"
        if [ -n "$NEW" ]; then
            RUNTIME_ROOT="$HOME/.local/state/thinkthread/instances/$NEW/thinkthreads/$NEW/session/goal-plus"
            break
        fi
        sleep 1
    done
    if [ -z "$RUNTIME_ROOT" ]; then
        RUNTIME_ROOT="$(ls -td ~/.local/state/thinkthread/instances/*/thinkthreads/*/session/goal-plus 2>/dev/null | head -1 || true)"
    fi
else
    # Direct pi/codex runs keep their state in <cwd>/.gp.
    for _ in $(seq 1 120); do
        kill -0 "$TASK_PID" 2>/dev/null || break
        if [ -d "$PWD/.gp/runs" ] || [ -d "$PWD/.gp/host-logs" ]; then
            RUNTIME_ROOT="$PWD/.gp"
            break
        fi
        sleep 1
    done
fi

start_watcher
wait "$TASK_PID"
STATUS=$?
cleanup
final_export
if [ -n "$RUNTIME_ROOT" ]; then
    echo "[run-with-transcript] transcripts: $RUNTIME_ROOT/host-logs/agent-transcript/index.html" >&2
fi
exit "$STATUS"
