---
name: preserve-score-semantics
description: Preserve score_batch semantics while implementing, optimizing, or auditing scoring.py. Use for this repository's scoring work, especially when a faster result might rely on input identity, stale output caches, fixed inputs, or reduced numeric work.
---

# Preserve Score Semantics

Treat these as hard acceptance requirements:

- Compute every returned element from the input's current contents in the same call.
- Preserve input order, output length, and full Python floating-point evaluation.
- Do not cache complete outputs or input-dependent activations across independent calls.
- Do not key behavior on object identity, fixed seeds, fixed batches, or benchmark-only values.
- Keep `.agents/`, `.goal-plus-verifiers/`, and `search-spec.json` read-only. Only `scoring.py` may change.

Before accepting a delivery, run this repository-owned check from the workspace root:

```bash
python3 .agents/skills/preserve-score-semantics/scripts/check_score_semantics.py
```

Treat a nonzero exit status or any reported violation as a rejection. A faster ranking score never overrides these requirements.
