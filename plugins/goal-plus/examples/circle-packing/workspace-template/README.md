# Circle Packing Workspace Template

This directory is the tracked, read-only baseline for the live example. Start
Pi or Codex from its parent `circle-packing/` directory; do not run Goal Plus
here and do not apply a promotion patch back into this template.

Candidate workspaces may edit their copy of `initial_program.py` to return
exactly 26 circle centers and radii inside a unit square. Circles must not
overlap. The optimization metric is the sum of all radii, and higher is better.

Run the public verifier with:

```bash
python3 .goal-plus-verifiers/circle_packing_score.py
```

The `.goal-plus-verifiers/` directory is read-only task infrastructure.
