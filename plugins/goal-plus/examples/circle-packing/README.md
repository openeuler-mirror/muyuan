# Circle Packing Quick Live Run

This is the recommended compact Goal Plus usability scenario. It starts a real
interactive Pi `/goal-plus` or Codex `$goal-plus` task, launches two native
candidate lanes, and uses a deterministic public verifier. The workspace
depends only on the Python standard library. Each host has a five-minute preset
variant and an untimed variant so completion semantics can be checked
independently from efficiency measurement.

## Run with Codex

Start Codex from the Circle Packing example directory with the main model
selected:

```bash
cd examples/circle-packing
codex -m gpt-5.6-terra -c model_reasoning_effort=max
```

Then paste the complete contents of
[`codex-goal-prompt.md`](codex-goal-prompt.md).

For the variant with no top-level time condition, use
[`codex-goal-prompt-untimed.md`](codex-goal-prompt-untimed.md).

## Run with Pi

Start the installed Pi package from the same example directory:

```bash
cd examples/circle-packing
pi
```

Then paste the complete contents of
[`pi-goal-prompt.md`](pi-goal-prompt.md).

For the variant with no top-level time condition, use
[`pi-goal-prompt-untimed.md`](pi-goal-prompt-untimed.md).

The Pi prompt does not pin `main`, `annotator`, or `workers`; it uses the model
already selected in Pi and the installed Goal Plus defaults. This keeps the
example runnable with any Pi model that supports the workflow. The Codex prompt
retains explicit Codex-native role ids as the sole host-specific model-routing
example: one worker lane uses Terra, one uses Luna, and Terra performs evidence
annotation.
Goal Plus discovers the verifier, metric, edit surface, Search strategy, and
host-specific worker launch from the current target directory.

Run Goal Plus from `circle-packing/`, not from `workspace-template/`. The
template is a tracked, read-only baseline. Search candidates edit isolated
worktrees under the ignored `.gp/` directory. The final promotion patch and
canonical reports remain under `.gp/`; this demonstration deliberately does
not apply the selected patch back to `workspace-template/`. Therefore a normal
Pi or Codex run does not change tracked example files or appear in `git status`.

## Five-minute preset variant

- The preset task duration is five minutes.
- Candidate work stops after four minutes.
- Each worker lease is capped at three minutes.
- The final minute is reserved for selection, promotion, reporting, and
  terminal settlement.

The short run is intended to expose the real flow and measured result. It may
finish without improving the baseline. The preset is an efficiency observation,
not a result-status gate: after valid artifacts and required audits are settled,
the Goal remains complete even when final settlement finishes after the preset.
The completion card reports the preset duration and actual closeout completion
time as separate facts.

On a cold Codex session with maximum reasoning, skill loading and Goal Plus
intake can consume most of the candidate window. Use the measured preset versus
actual closeout values to diagnose that overhead without changing the valid
result status.

## Untimed variant

The untimed prompt text does not define a top-level time condition. It starts
the same two fixed lanes, requires at least one settled verifier-backed result
from each, then closes the pool without continuation work and performs the same
selection, artifact-only promotion, source-integrity check, and reporting flow.

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
