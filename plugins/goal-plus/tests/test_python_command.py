"""Internal workers preserve isolation without changing user tool environments."""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_isolated_host_preserves_python_flags_and_external_environment(tmp_path):
    source = Path(__file__).parents[1] / "src"
    poison = tmp_path / "poison"
    poison.mkdir()
    (poison / "sitecustomize.py").write_text("raise RuntimeError('wrong sitecustomize')")
    code = f'''
import json, os, subprocess, sys
sys.path.insert(0, {str(source)!r})
from goal_plus.execution.python_command import module_arguments
args = module_arguments("goal_plus.installation", [])
assert args[:3] == ["-I", "-m", "goal_plus.installation"]
child = subprocess.run([sys.executable, args[0], "-c", "import sys; print(sys.flags.isolated)"], capture_output=True, text=True, check=True)
print(json.dumps({{"isolated": child.stdout.strip(), "pythonpath": os.environ["PYTHONPATH"]}}))
'''
    env = {**os.environ, "PYTHONPATH": str(poison), "PYTHONHOME": "/wrong/home"}
    result = subprocess.run([sys.executable, "-I", "-c", code], cwd=poison, env=env, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"isolated": "1", "pythonpath": str(poison)}
