# Codex/Pi run analysis

`analyze_run.py` reads only durable `.gp` state and emits one compact JSON
object. Use it after selection and promotion for both ten-minute runs:

```bash
python examples/model-optimize/torch-cpu-target/analyze_run.py \
  --root .gp \
  --run-id run_... \
  --expected-host codex \
  --require-pass

python examples/model-optimize/torch-cpu-target/analyze_run.py \
  --root .gp \
  --run-id run_... \
  --expected-host pi-rpc \
  --require-pass
```

The analyzer checks the shared control-plane contract: one initial
`parallel_loops` plan, two candidates and durable sessions, same-candidate
session binding across continuations, a 600-second worker budget, verifier and
annotation evidence, source-tagged Evidence read audit, final selection,
promotion, model/host provenance, and the one-thread Torch rule.

Shared-dir activity is reported without being invented. When the frozen spec
disables `shared_dir`, that check is `not_applicable`. When it is enabled, the
analyzer verifies that every iteration has settlement fields and reports
staged, published, viewed, and consumed counts even when those counts are zero.
Supplemental Evidence detail reads are also reported as on-demand activity;
zero detail reads are not treated as a failure.

Codex and Pi are not expected to have identical native lifecycle fields.
Codex may show `resume_dispatches` on one host-resident worker, while Pi may
resume a native session across multiple RPC processes. The portable assertion
is that each candidate keeps one durable `agent_session_id` across all of its
verifier-backed iterations.
