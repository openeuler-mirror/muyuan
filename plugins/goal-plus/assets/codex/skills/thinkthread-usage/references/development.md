# Goal Plus ThinkThread Adapter Development

Use this page when changing or reviewing Goal Plus's ThinkThread integration. Runtime preparation
and incident diagnosis are separate entries in [the skill](../SKILL.md); read them only when a
specific behavior must be reproduced. The maintained contracts are in Goal Plus's
`docs/agent-host-adapters.md`, `docs/shared-plane.md`, and `docs/search-recovery.md`.

## Locate the owning boundary

1. State the requested behavior and select its [capability](capabilities.md). Separate TT runtime
   availability, Goal Plus adapter support, host/role authorization, and benchmark configuration.
2. Read the installed interface/schema and the owning Goal Plus port. Define exact inputs, role,
   identities, result meaning, external side effects, and uncertain-completion handling.
3. Change the owning module. Keep Goal/Search state, policy, budgets, Evidence and promotion in
   Goal Plus; keep native Child/session facts in the host adapter. Do not add a second scheduler.
4. Validate with the smallest relevant controlled tests, including an observed failure branch.
   A real-host result is separate evidence and needs explicit task scope.
5. Recheck the same capability on the actual target build and update its support note. An
   installation refresh does not alter the provenance of a retained Root or session.

Paths below are relative to the Goal Plus module:

| Change | Source owner and maintained contract |
| --- | --- |
| Host binding, model and role delegation | `scripts/install.py`, `src/goal_plus/agent_hosts.py`, `src/goal_plus/thinkthread_agent_posix.py`; `docs/codex.md`, `docs/pi.md` |
| Child, Session and Message | `src/goal_plus/execution/thinkthread_session.py`, `thinkthread_child.py`, `thinkthread_rpc.py`; `docs/agent-host-adapters.md` |
| Workspace and artifact identity | `src/goal_plus/infrastructure/artifacts/thinkthread_*.py`; `docs/shared-plane.md` |
| Verifier execution and settlement | `src/goal_plus/infrastructure/verifiers/thinkthread*.py`, `src/goal_plus/application/thinkthread_verification.py` |
| Request observation and recovery | `src/goal_plus/execution/thinkthread_requests.py`, `src/goal_plus/infrastructure/artifacts/thinkthread_mutations.py`; `docs/search-recovery.md` |

Preserve run, candidate, generation, session, call, request, message, attempt, snapshot and
artifact identities across the adapter. Reconcile unknown completion before retrying; a timeout
does not establish failure or release a resource. Use public Goal Plus tools and typed ports, not
private state edits or a transcript-derived result.

## Validation boundary

Run the closest module tests and asset tests for changed host wiring. Use the repository's
documented test environment and `markdownlint-cli2` for Markdown edits. Real Codex/Pi/TT ST is
required to claim native lifecycle behavior, but run it only when explicitly requested and its
host prerequisites are available. Otherwise report that behavior as unverified.

Check the installed build and exact interface before applying version-specific limitations from
[the capability guide](capabilities.md#establish-actual-support). Avoid encoding a model, device,
container path, or benchmark-specific verifier rule in the adapter guidance.
