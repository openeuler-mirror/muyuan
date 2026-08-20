# Codex/Pi Orchestration Monitor Comparison

This example records the same ordinary Goal Plus work item through representative
Codex and Pi host operations, then compares the `orchestration` feature plugin
output.

## Example contract

| Field | Value |
|---|---|
| Entry | deterministic `compare_hosts.py` script |
| Goal Plus task | no live Goal Plus model run |
| Whole-goal limit | not applicable |
| Output | two durable `.gp/` fixtures plus `comparison.json` |

```bash
python examples/orchestration-monitor/compare_hosts.py \
  .tmp/orchestration-monitor-comparison
```

The example is deterministic protocol coverage, not a live model benchmark. It
proves that both hosts expose the same task packet and semantic event flow while
retaining different native operations. The output directory contains each
host's durable `.gp/` state plus `comparison.json`; it must not already exist.
