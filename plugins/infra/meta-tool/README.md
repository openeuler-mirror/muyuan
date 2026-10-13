# Meta-tool Online Core

This package provides the stable Python foundation for Meta-tool Online:

- JSON-compatible trace and tool-call data contracts;
- portable workflow specifications with `$input` and `$step` bindings;
- deterministic synchronous and asynchronous execution of primitive tools.

Discovery, optimization, the RPC bridge, and Pi integration are layered on top
of this foundation in subsequent changes.

Run the core tests with:

```bash
python3 -m pytest -q tests/test_models_io.py tests/test_compiler_runtime.py
```
