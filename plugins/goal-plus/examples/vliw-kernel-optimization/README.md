# VLIW Kernel Optimization

This example is a local, public-case VLIW optimization task that can be opened
directly through Pi's native `/goal-plus` command. It uses only the Python
standard library and does not require Docker or accelerator hardware.

## Run with Pi

Start Pi from the VLIW example directory with the main model selected:

```bash
cd examples/vliw-kernel-optimization
pi
```

Then paste the complete contents of
[`pi-goal-prompt.md`](pi-goal-prompt.md).

The prompt does not pin `main`, `annotator`, or `workers`; it uses the model
already selected in Pi and the installed Goal Plus defaults. This keeps the
example independent of any provider-specific local model registry. Explicit
role routing remains optional and can be added to the first line when that is
the behavior being demonstrated.

No SearchSpec or experiment launcher is required: Goal Plus discovers the
verifier, metric, edit surface, and local workspace from the task.

Run Goal Plus from `vliw-kernel-optimization/`, not from
`workspace-template/`. The template is the tracked, read-only public baseline.
Search candidates edit isolated worktrees under the ignored `.gp/` directory.
The selected promotion patch and canonical reports remain under `.gp/`; this
example does not apply the result back to `workspace-template/`. A normal run
therefore does not change tracked example files or appear in `git status`.

## Eight-minute contract

- The complete Goal Plus task must settle within eight minutes.
- Candidate work stops after six minutes.
- Each worker lease is capped at four minutes.
- The final two minutes are reserved for selection, promotion, reporting, and
  terminal settlement.

## Local baseline

Run the public verifier without Goal Plus. The verifier invokes `runner.py`, so
execute it with `workspace-template/` as its working directory:

```bash
cd examples/vliw-kernel-optimization
(cd workspace-template && python3 .goal-plus-verifiers/vliw_score.py)
```

The starter implementation is correct and reports `147734` cycles. Lower is
better. Only `solution.py` is an optimization surface; the simulator, runner,
verifier, test cases, and Goal Plus score adapter are read-only evaluation
assets.

## Scope

This is a runnable local example, not an official EdgeBench result. It includes
only public cases, so it demonstrates Goal Plus orchestration and model routing
rather than hidden-case benchmark comparability. See
[`PROVENANCE.md`](PROVENANCE.md) for the upstream source and license.
