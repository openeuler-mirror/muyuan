"""This first layer must not depend on unpublished algorithms or adapters."""
from pathlib import Path
import subprocess
import sys
import textwrap
import tomllib

from awo import MetaToolValidationError
from awo.io import load_specs, load_traces, write_json, write_jsonl
from awo.models import ToolCall, Trace
from awo.spec import MetaToolSpec, StepSpec, input_binding, step_binding


ROOT = Path(__file__).resolve().parents[1]


def test_trace_and_spec_io_round_trip(tmp_path):
    trace = Trace(trace_id="round-trip", calls=(ToolCall("first", {"value": 1}),))
    trace_path = tmp_path / "traces.jsonl"
    write_jsonl(trace_path, [trace.to_dict()])
    assert load_traces(trace_path) == [trace]
    spec = MetaToolSpec(
        name="round_trip", description="Round-trip fixture",
        input_schema={"type": "object", "properties": {"value": {}}},
        steps=(StepSpec("first", "first", {"value": input_binding("value")}),
               StepSpec("second", "second", {"value": step_binding("first")})),
        output=step_binding("second"),
    )
    spec_path = tmp_path / "catalog.json"
    write_json(spec_path, {"meta_tools": [spec.to_dict()]})
    assert load_specs(spec_path) == [spec]


def test_core_imports_without_future_layers_or_validation_dependencies(tmp_path):
    script = textwrap.dedent("""
        import importlib.abc
        import sys
        class BlockFutureLayers(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, *args):
                blocked = ('bate', 'jsonschema', 'referencing', 'awo.normalization',
                           'awo.extraction', 'awo.pipeline', 'awo.dispatch')
                if any(fullname == x or fullname.startswith(x + '.') for x in blocked):
                    raise ModuleNotFoundError(fullname)
        sys.meta_path.insert(0, BlockFutureLayers())
        sys.path.insert(0, sys.argv[1])
        import awo
        import awo.io
        assert issubclass(awo.MetaToolValidationError, ValueError)
        assert 'jsonschema' not in sys.modules
        assert 'referencing' not in sys.modules
    """)
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(ROOT / "src")],
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_readme_example_executes():
    source = (ROOT / "README.md").read_text()
    snippet = source.split("```python\n", 1)[1].split("```", 1)[0]
    exec(compile(snippet, "README.md", "exec"), {})


def test_package_identity_and_entry_point(tmp_path):
    import awo
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert config["version"] == awo.__version__
    assert issubclass(MetaToolValidationError, ValueError)
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c",
         "import sys,runpy;sys.path.insert(0,sys.argv[1]);runpy.run_module('awo',run_name='__main__')",
         str(ROOT / "src")],
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{config['name']} {config['version']}"
