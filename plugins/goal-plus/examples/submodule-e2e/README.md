# Recursive Submodule E2E

This fixture is offline and creates all repositories under a temporary directory.
It exercises candidate materialization, dirty-child rejection, stale-gitlink rejection,
child-first commits, parent checkpointing, recursive topology validation, and the exact
combined verifier result.

Failures use typed names and include the repository-relative path, stage, reason, and
expected/actual values where applicable. A dirty child is rejected before verifier execution;
the parent-only path remains valid when no `.gitmodules` or gitlink is present.

Run it from the plugin root:

```bash
uv run python examples/submodule-e2e/run_e2e.py
```

The successful result must contain `"combined_score": 22`.
