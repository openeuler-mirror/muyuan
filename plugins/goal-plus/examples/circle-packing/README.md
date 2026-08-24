# Circle Packing Quick Live Run

This is the recommended five-minute Goal Plus usability scenario. It starts a
real interactive Pi `/goal-plus` task, launches two native candidate lanes, and
uses a deterministic public verifier. The workspace depends only on the Python
standard library.

## Run with Pi

Start the installed Pi package from the target directory:

```bash
cd examples/circle-packing/workspace
pi --model openai-codex/gpt-5.6-terra --thinking max
```

Then paste the complete contents of
[`pi-goal-prompt.md`](pi-goal-prompt.md).

The main model is selected before Goal Plus starts; the prompt does not include
`main=`. Goal Plus discovers the verifier, metric, edit surface, Search strategy,
and Pi worker launch from the current target directory. The checked-in prompt
uses Terra for the annotator role and assigns one Terra and one Luna candidate
lane. If those model ids are not installed, inspect the local registry with
`pi --list-models gpt-5.6` and replace only the role ids on the first prompt
line.

## Five-minute contract

- The complete Goal Plus task must settle within five minutes.
- Candidate work stops after four minutes.
- Each worker lease is capped at three minutes.
- The final minute is reserved for selection, promotion, reporting, and
  terminal settlement.

The short run is intended to expose the real flow and measured result. It may
finish without improving the baseline.

## Local baseline

```bash
cd examples/circle-packing/workspace
python3 .goal-plus-verifiers/circle_packing_score.py
```

The starter is valid and currently reports a `sum_radii` of about `1.79835`.
Higher is better.

Only `initial_program.py` is editable. A valid result must contain exactly 26
non-overlapping circles inside the unit square. The verifier is public and
read-only so this example demonstrates optimization with feedback, not a
hidden-answer benchmark.
