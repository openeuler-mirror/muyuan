"""Probe an isolated installed Pi project without provider requests.

Run after install.py --pi --project /path/to/disposable/project.
The project must use fresh default batching; do not point at a working session.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--pi", default=shutil.which("pi"))
    args = parser.parse_args()
    if not args.pi:
        parser.error("Pi executable not found")
    project = args.project.resolve(strict=True)
    runtime = project / ".meta-tool"
    package = runtime / "integrations/pi-meta-tool"
    extension = package / "extensions/meta-tool.ts"
    probe = ROOT / "integrations/pi-meta-tool/tests/fixtures/read-batch-probe.ts"
    with tempfile.TemporaryDirectory(prefix="bate-install-probe-") as temporary:
        test_root = Path(temporary)
        env = {k: v for k, v in os.environ.items() if not k.startswith("META_TOOL_") and k != "PYTHONPATH"}
        env.update(PI_CODING_AGENT_DIR=str(test_root / "agent-config"), PI_OFFLINE="1", PI_TELEMETRY="0",
                   BATE_READ_BATCH_PROBE_EXTENSION=str(extension), BATE_READ_BATCH_PROBE_DISABLED="0")
        for worker in (False, True):
            cwd = test_root / "worker" if worker else project
            cwd.mkdir(exist_ok=True)
            command = [str(package / "bin/pi") if worker else args.pi]
            if worker:
                env.update(META_TOOL_PI_REAL_BIN=args.pi, META_TOOL_PI_EXTRA_EXTENSIONS=str(extension),
                           GOAL_PLUS_PI_ROLE="worker")
                command.append("--no-extensions")
            # Main loads only through the actual generated project package setting.
            command += ["--offline", "--approve", "--no-session", "--no-skills", "--no-prompt-templates",
                        "--no-themes", "--no-context-files", "--provider", "openai", "--model", "gpt-4o-mini",
                        "--thinking", "off", "--mode", "json", "--extension", str(probe), "--print", "probe"]
            result = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, timeout=60)
            records = []
            for line in (result.stdout + "\n" + result.stderr).splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("read_batch_probe"):
                    records.append(record)
            assert result.returncode == 0 and len(records) == 1 and records[0]["passed"], {
                "worker": worker, "returncode": result.returncode, "records": records,
                "stderr": result.stderr[-3000:], "stdout": result.stdout[-3000:]}
            print(json.dumps({"case": "installed-worker" if worker else "installed-main", **records[0]}), flush=True)
        config = json.loads((ROOT / "tests/fixtures/branched_config.json").read_text())
        config["extraction"].update(max_steps=4, require_fully_bound=True)
        names = ["lookup_customer", "lookup_order", "lookup_invoice", "read_record"]
        config["admission"] = {"read_only_tools": names}
        payload = {"operation": "discover", "config": config,
                   "tracePath": str(ROOT / "tests/fixtures/branched_traces.json"),
                   "outputDir": str(test_root / "discovered"), "adapter": {"supportedTools": names}}
        result = subprocess.run([str(runtime / ".venv/bin/python"), str(package / "python/bridge.py")],
                                input=json.dumps(payload) + "\n", cwd=test_root, env=env,
                                text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr + result.stdout
        response = json.loads(result.stdout.splitlines()[-1])
        assert response["type"] == "complete" and response["result"]["activeMetaTools"] == 2, response
        print(json.dumps({"case": "installed-python-bridge", "passed": True,
                          "learned_tools": 2, "model_calls": 0}), flush=True)


if __name__ == "__main__":
    main()
