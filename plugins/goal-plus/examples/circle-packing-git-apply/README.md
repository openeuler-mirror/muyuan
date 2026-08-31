# Circle Packing Git Workspace Apply

This example runs the same optimization task, verifier, two candidate lanes,
and five-minute operating contract as
[Circle Packing](../circle-packing/README.md). Its distinct purpose is to show
the complete writable Git path: create real candidate worktrees, select the
best verifier-backed commit, and apply it back to the original source
repository.

The checked-in Circle template remains untouched. A preparation script copies
that template into an ignored `workspace/` directory and initializes it as a
standalone Git repository. Goal Plus can then exercise `promotion_mode=apply`
without changing tracked example files.

## Prepare the writable source

Run from this example directory:

```bash
cd examples/circle-packing-git-apply
python3 prepare_workspace.py
```

The command creates this clean source repository:

```text
workspace/
|-- .git/
|-- .goal-plus-verifiers/circle_packing_score.py
|-- README.md
`-- initial_program.py
```

It refuses to overwrite an existing directory. To discard a previously
generated example workspace and start again, use:

```bash
python3 prepare_workspace.py --reset
```

`--reset` removes only a workspace carrying this example's marker inside its
`.git/` directory.

## Run with Codex

Start Codex from the example directory:

```bash
cd examples/circle-packing-git-apply
codex -m gpt-5.6-terra -c model_reasoning_effort=max
```

Then paste the complete contents of
[`codex-goal-prompt.md`](codex-goal-prompt.md). For the same workflow without a
top-level time condition, use
[`codex-goal-prompt-untimed.md`](codex-goal-prompt-untimed.md).

## Run with Pi

Start Pi from the same directory:

```bash
cd examples/circle-packing-git-apply
pi
```

Then paste the complete contents of [`pi-goal-prompt.md`](pi-goal-prompt.md).
For the untimed variant, use
[`pi-goal-prompt-untimed.md`](pi-goal-prompt-untimed.md).

The prompts use the active Host model and installed Goal Plus defaults. The
original Circle Packing Codex prompt remains the dedicated multi-model routing
example; this scenario isolates workspace and apply behavior.

## What this example demonstrates

```text
writable workspace/ Git repository
              |
              | new
              v
      run-local Git mirror
         /           \
  candidate c001   candidate c002
   worktree/branch  worktree/branch
         \           /
          verifier-backed selection
                   |
                   v
       squash publication commit
                   |
                   | search_apply_promotion
                   v
      fetch + cherry-pick into workspace/
```

Candidate creation uses Git worktree checkouts. Final write-back does not copy
the candidate directory or manually checkout its branch into source. The Git
Provider constructs a single publication commit from the selected artifact's
allowed product files, fetches it into `workspace/`, and cherry-picks it. Full
candidate history remains in the run-local repository under `.gp/`; source
receives one auditable product commit.

## Inspect the applied result

After Goal Plus completes, inspect the source directly:

```bash
git -C workspace status --short
git -C workspace log --oneline --decorate -2
git -C workspace show --stat --oneline HEAD
python3 workspace/.goal-plus-verifiers/circle_packing_score.py
```

The source must be clean after apply. When the selected artifact changed
`initial_program.py`, `HEAD` advances by one publication commit. If the selected
artifact is identical to the baseline, the Provider records a Git no-op and
leaves `HEAD` unchanged.

Only `initial_program.py` is editable. The public verifier still requires
exactly 26 non-overlapping circles inside the unit square and maximizes
`sum_radii`; no task or scoring behavior differs from the original Circle case.
