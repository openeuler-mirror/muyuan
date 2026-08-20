# Workspace Backend Runtime E2E

This small host-free example exercises the `git_worktree` candidate workspace
backend through the public `SearchTools` facade. It is intentionally a numeric
fixture so the example isolates workspace engineering from model quality.

## Example contract

| Field | Value |
|---|---|
| Entry | deterministic `run_demo.py` script |
| Goal Plus task | no Goal Plus record or live host worker |
| Whole-goal limit | not applicable |
| SearchSpec worker budget | schema/adapter fixture only; unused by this host-free run |
| Output | candidate revisions, selection, and Search report |

The demo:

1. freezes `git_worktree_search_spec.json`;
2. creates `c001`, `c002`, and `c003` once from the same run baseline;
3. writes and verifies independent values on their distinct branches;
4. selects higher-scoring `c003` and writes the normal Search report.

Run it with a new runtime path:

```bash
python examples/workspace-backends/run_demo.py \
  --runtime-root .tmp/workspace-backend-demo
```

The final stdout line is JSON containing candidate scores, branch names,
parent and child revisions, selection, report path, and whether every candidate
resolved to the same Git common directory.

This is a runtime E2E. It does not launch a real Codex or Pi worker and
therefore is not evidence for host lifecycle or `/goal-plus` hook behavior.
