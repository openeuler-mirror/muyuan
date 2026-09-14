# Signal Processing Fixture

This deterministic fixture tests Search contracts on causal filtering of synthetic,
non-stationary signals. It is a test input, not a public benchmark result.

- `initial_program.py` implements `process_signal(input_signal, window_size)` and is the editable target.
- `evaluator.py` and `config.yaml` define the frozen public scoring inputs.
- `search-spec.json` describes one-model fixed lanes; `multi-model-search-spec.json`
  supplies an explicit worker-model variant. Tests resolve the source path and host inputs.

Run pure evaluation from this directory without a model:

```bash
python -c "from evaluator import evaluate; import json; print(json.dumps(evaluate('initial_program.py'), sort_keys=True))"
```

The result includes `overall_score`; higher is better. Frozen evaluator integrity,
candidate isolation and promotion are verified by runtime tests, not by altering
this evaluator to accept a candidate. See [Tests](../../README.md) for test layers.
