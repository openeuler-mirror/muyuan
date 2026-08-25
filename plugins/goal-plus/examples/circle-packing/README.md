# Circle Packing Quick Live Run

This is the recommended five-minute Goal Plus usability scenario. It starts a
real interactive Pi `/goal-plus` or Codex `$goal-plus` task, launches two native
candidate lanes, and uses a deterministic public verifier. The workspace
depends only on the Python standard library.

## Run with Codex

Start Codex from the Circle Packing example directory with the main model
selected:

```bash
cd examples/circle-packing
codex -m gpt-5.6-terra -c model_reasoning_effort=max
```

Then paste the complete contents of
[`codex-goal-prompt.md`](codex-goal-prompt.md). The prompt uses Codex-native
model ids for one Terra and one Luna candidate lane.

## Run with Pi

Start the installed Pi package from the same example directory:

```bash
cd examples/circle-packing
pi --model openai-codex/gpt-5.6-terra --thinking max
```

Then paste the complete contents of
[`pi-goal-prompt.md`](pi-goal-prompt.md).

In both hosts the main model is selected before Goal Plus starts; neither prompt
includes `main=`. Goal Plus discovers the verifier, metric, edit surface, Search
strategy, and host-specific worker launch from the current target directory.
The checked-in prompts use Terra for the annotator role and assign one Terra and
one Luna candidate lane. For Pi, if those provider-qualified model ids are not
installed, inspect the local registry with `pi --list-models gpt-5.6` and
replace only the role ids on the first prompt line.

Run Goal Plus from `circle-packing/`, not from `workspace-template/`. The
template is a tracked, read-only baseline. Search candidates edit isolated
worktrees under the ignored `.gp/` directory. The final promotion patch and
canonical reports remain under `.gp/`; this demonstration deliberately does
not apply the selected patch back to `workspace-template/`. Therefore a normal
Pi or Codex run does not change tracked example files or appear in `git status`.

## Five-minute contract

- The complete Goal Plus task must settle within five minutes.
- Candidate work stops after four minutes.
- Each worker lease is capped at three minutes.
- The final minute is reserved for selection, promotion, reporting, and
  terminal settlement.

The short run is intended to expose the real flow and measured result. It may
finish without improving the baseline.

On a cold Codex session with maximum reasoning, skill loading and Goal Plus
intake can consume most of the candidate window. Treat the strict five-minute
prompt as a deadline/closeout UX exercise; it may reach a terminal deadline
status even after preserving and promoting a valid result.

## Local baseline

```bash
cd examples/circle-packing
python3 workspace-template/.goal-plus-verifiers/circle_packing_score.py
```

The starter is valid and currently reports a `sum_radii` of about `1.79835`.
Higher is better.

Only the candidate copy of `initial_program.py` is editable. A valid result
must contain exactly 26 non-overlapping circles inside the unit square. The
verifier is public and read-only so this example demonstrates optimization with
feedback, not a hidden-answer benchmark.
