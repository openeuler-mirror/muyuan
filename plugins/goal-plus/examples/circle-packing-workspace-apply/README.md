# Circle Packing Workspace Apply

This example demonstrates the full Git-backed workspace lifecycle without
changing the tracked Circle Packing template or the outer Muyuan repository.
It prepares an ignored disposable directory, initializes that directory as an
independent Git repository, launches Pi from there, and applies the selected
Goal Plus result back into that disposable repository as one squash commit.

Use the existing [Circle Packing example](../circle-packing/README.md) when the
promotion patch and report should remain artifact-only. Use this example when
you specifically want to observe `search_apply_promotion` and its timing.

## Prepare the workspace

From this example directory, run:

```bash
python3 prepare_workspace.py
cd workspace
```

The preparation command:

1. copies the public Circle Packing program and verifier;
2. copies this example's `search-spec.json` into the new root;
3. initializes `workspace/` as an independent Git repository; and
4. creates one clean baseline commit.

The spec intentionally omits `workspace.promotion_mode`: the runtime default is
`apply`. Read-only templates and test fixtures opt out explicitly with
`artifact_only`.

The outer repository ignores `workspace/`. The generated repository also
ignores `.gp/`, `.tmp/`, Python caches, and bytecode. The command refuses to
overwrite an existing destination. Move an old workspace aside or to the trash
before preparing a fresh one.

An alternate destination is useful for isolated experiments or tests:

```bash
python3 prepare_workspace.py --destination /absolute/path/to/circle-workspace
```

## Run with Pi

Start Pi while the current directory is the generated workspace:

```bash
pi
```

Paste the contents of
[pi-goal-prompt.md](pi-goal-prompt.md). The prompt freezes the checked-in
contract copied to `search-spec.json`, creates two Git worktree candidate lanes,
selects and promotes the best valid result, and then calls
`search_apply_promotion`.

After a successful run:

- `initial_program.py` in the generated workspace contains the selected result;
- the generated repository has exactly one Goal Plus publication commit on top
  of its baseline, regardless of candidate running-commit count;
- the generated repository is clean;
- `.gp/` contains the durable run state and canonical report but remains ignored;
  and
- the final statistics expose candidate workspace creation time and promotion
  apply time.

The tracked source under `../circle-packing/workspace-template/` remains
unchanged.
