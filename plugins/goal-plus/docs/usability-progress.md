# Lightweight Search Progress UX Plan

This plan defines the small, user-facing progress experience for Goal Plus.
It intentionally keeps full monitor, Evidence, and report data out of the
primary interaction.

The first implementation slice is a Pi TUI preview. It proves that the installed
extension can render the proposed status and widget without creating a Goal
Plus record, Search run, candidate workspace, or model request. The preview is
inspected directly in Pi; no separate mock or image artifact is part of the UX.

---

## Target Experience

Users should be able to answer three questions without reading tool JSON:

1. Did Search actually start?
2. What has happened so far?
3. What was the final measurable result?

The primary surface is deliberately small:

- one host-native status line;
- one five-to-seven-line progress or completion view;
- existing expandable tool calls for users who want more detail;
- a conversational progress query in Codex while workers continue running.

The primary surface must not show Audit labels, Evidence payloads, report
paths, transcript paths, or raw monitor JSON.

## Truthful Progress States

| Display state | Required observation |
|---|---|
| `SEARCH PREPARING` | A Search run exists, but no host worker session is known to have started. |
| `SEARCH ACTIVE` | At least one candidate has a host session launch/bind observation and the run is non-terminal. |
| `SELECTING` | Candidate execution is being closed and selection or promotion is pending. |
| `SEARCH COMPLETE` | Selection and terminal Goal Plus settlement are recorded; promotion is reported separately. |
| `SEARCH BLOCKED` | Goal Plus or the current Search cannot continue without an external change. |

Do not infer exact worker liveness from `AgentSessionRecord`. Pi may add
host-pool liveness, while Codex may add its native agent status. The shared
view uses `sessions_started` unless an authoritative host-owned live count is
available.

## Compact Progress View

The host-neutral view is a read-only projection of
`goal_plus_monitor_snapshot`, not another persisted state machine. Its intended
fields are:

```json
{
  "status": "active",
  "phase": "search",
  "elapsed_seconds": 126,
  "search": {
    "started": true,
    "run_id": "run_...",
    "state": "active",
    "candidates_total": 2,
    "sessions_started": 2,
    "metric_name": "sum_radii",
    "verifier_runs_total": 4,
    "best_candidate_id": "c001",
    "best_score": 1.93276,
    "candidates": [
      {"candidate_id": "c001", "model": "terra", "verifier_count": 2, "best_score": 1.93276},
      {"candidate_id": "c002", "model": "luna", "verifier_count": 2, "best_score": 1.90144}
    ]
  }
}
```

Process-verifier iterations make up `verifier_runs_total`. Promotion is a
separate `passed`, `failed`, or `not_observed` result so the UI does not mix two
different checks.

## Host Behavior

| Host | First useful version | Later option |
|---|---|---|
| Pi TUI | Native `setStatus` plus `setWidget`, refreshed at meaningful Goal Plus/Search events. | Bounded host-side refresh while a long pool wait is active. |
| Codex CLI/App | One visible Search-start message, on-demand progress questions through a read-only MCP call, and one final compact summary. | A dedicated native client surface only if the conversational experience proves insufficient. |
| Pi RPC | Emit the same extension UI requests. | Client-specific rendering; clients may ignore fire-and-forget UI requests. |

Codex must not reuse `update_plan` for Search telemetry because that would
overwrite or distort an unrelated user task plan.

---

## Delivery Plan

### Phase 0: Pi TUI preview

- Add a pure Pi progress presentation model with active and complete fixtures.
- Render the fixture through the real Goal Plus Pi extension using
  `setStatus` and `setWidget`.
- Gate the preview behind `GOAL_PLUS_UI_PREVIEW`.
- Validate the active and completed states directly in the Pi TUI.
- Do not create `.gp` state or contact a model.

### Phase 1: Shared compact projection

- Derive the compact progress view from saved monitor state.
- Define exact state transitions and unavailable-value behavior.
- Expose it through a small read-only MCP/facade call, or a backward-compatible
  compact projection on the existing monitor tool.
- Test multi-task goals, missing host observability, failed promotion, and
  resumed runs.

### Phase 2: Pi live integration

- Show `SEARCH PREPARING` after run creation.
- Switch to `SEARCH ACTIVE` only after host launch/bind is observed.
- Refresh after pool open, wait-any completion, continuation, selection, and
  promotion.
- Replace the current terminal stats-only message with a compact Search result
  followed by optional usage information.
- Clear the live widget after terminal settlement while preserving one final
  custom entry in the transcript.

### Phase 3: Codex conversational integration

- Announce Search only after native candidates have actually launched.
- Teach the main agent to answer progress-only user messages by calling the
  compact read-only progress API without stopping or replacing workers.
- Automatically render the terminal compact summary.
- Keep the existing plan untouched.

### Phase 4: Optional continuous refresh

Only pursue continuous per-verifier refresh if event-based Pi updates and
Codex on-demand queries feel too stale. Any polling belongs to the host UI
adapter, not the Search runtime or persisted run state.

## Preview And Verification

From this checkout, show the running fixture without starting a Goal Plus run:

```bash
GOAL_PLUS_UI_PREVIEW=active \
  pi --offline --no-session --no-extensions \
  -e assets/pi/extensions/goal-plus.ts
```

Inside Pi, switch between fixtures or clear the preview:

```text
/goal-plus-ui-preview active
/goal-plus-ui-preview complete
/goal-plus-ui-preview clear
```

The command is registered only when `GOAL_PLUS_UI_PREVIEW` is enabled.

Acceptance for Phase 0:

- Pi starts without creating `.gp` state.
- The widget appears above the editor and the compact status appears in the
  footer.
- Active and complete fixtures fit at 80 terminal columns without hiding the
  input editor.
- The completion view includes elapsed time, total verifier runs, each
  candidate best, selected candidate, and promotion result.
- `python -m pytest tests/test_pi_assets.py -q` and `git diff --check` pass.

---

## Non-goals

- No new worker supervisor or runtime observation bus.
- No claim that a session record proves a process is live.
- No per-verifier push protocol in the first implementation.
- No Codex custom card in the first implementation.
- No full Goal Plus report or Audit content in the compact surface.
