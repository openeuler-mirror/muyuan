# Disposable Circle Packing Workspace

This directory is an independent Git repository prepared by the
`circle-packing-workspace-apply` example. Run Pi and Goal Plus from this root.

Only `initial_program.py` is editable during Search. The public verifier is:

```bash
python .goal-plus-verifiers/circle_packing_score.py
```

Goal Plus candidate lanes use Git worktrees. After selection and promotion,
`search_apply_promotion` applies one squash publication commit back to this
repository. Runtime state under `.gp/` is intentionally ignored.
