"""Run real Pi main/worker-wrapper probes without any model requests.

Usage: python3 tests/smoke_read_batches.py [--pi /path/to/pi]
All configuration and runtime state belong to a fresh temporary directory.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pi", default=shutil.which("pi"))
    args = parser.parse_args()
    if not args.pi:
        parser.error("Pi executable not found; supply --pi")
    package = Path(__file__).resolve().parents[1]
    extension = package / "extensions/meta-tool.ts"
    probe = package / "tests/fixtures/read-batch-probe.ts"
    cases = [
        ("fresh-default", None, False, False),
        ("static-main", False, False, False),
        ("online-main", True, False, False),
        ("static-opt-out", False, True, False),
        ("online-opt-out", True, True, False),
        ("static-worker-wrapper", False, False, True),
        ("online-worker-wrapper", True, False, True),
    ]
    with tempfile.TemporaryDirectory(prefix="bate-read-batch-smoke-") as temporary:
        root = Path(temporary)
        for name, online, disabled, worker in cases:
            cwd = root / name
            cwd.mkdir()
            config = {"routing": {"includeUserCatalog": False},
                      "catalog": {"userCatalogDir": str(cwd / "missing-catalog")}}
            if online is not None:
                config["online"] = {"enabled": online}
            if disabled:
                config["admission"] = {"policy": {"allow_caller_bound_read_batches": False}}
            if name != "fresh-default":
                directory = cwd / ".pi/meta-tool"
                directory.mkdir(parents=True)
                (directory / "config.json").write_text(json.dumps(config))
            env = dict(os.environ, PI_CODING_AGENT_DIR=str(cwd / "agent-config"),
                       PI_OFFLINE="1", PI_TELEMETRY="0",
                       BATE_READ_BATCH_PROBE_DISABLED="1" if disabled else "0")
            env.pop("META_TOOL_PI_DISABLE_WORKER_PROPAGATION", None)
            env.pop("META_TOOL_PI_EXTRA_EXTENSIONS", None)
            env.pop("META_TOOL_PI_REAL_BIN", None)
            command = [args.pi]
            if worker:
                command = [str(package / "bin/pi")]
                env.update(META_TOOL_PI_REAL_BIN=args.pi,
                           META_TOOL_PI_EXTRA_EXTENSIONS=str(extension), GOAL_PLUS_PI_ROLE="worker")
            command += ["--offline", "--approve", "--no-session", "--no-extensions", "--no-skills",
                        "--no-prompt-templates", "--no-themes", "--no-context-files",
                        "--provider", "openai", "--model", "gpt-4o-mini", "--thinking", "off", "--mode", "json"]
            if not worker:
                command += ["--extension", str(extension)]
            command += ["--extension", str(probe), "--print", "read-batch wiring probe"]
            result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
            records = []
            # Pi routes extension console output to stderr in JSON mode.
            for line in (result.stdout + "\n" + result.stderr).splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("read_batch_probe"):
                    records.append(record)
            assert result.returncode == 0 and len(records) == 1 and records[0]["passed"], {
                "case": name, "returncode": result.returncode,
                "probe": records, "stderr": result.stderr[-3000:], "stdout": result.stdout[-3000:]}
            print(json.dumps({"case": name, **records[0]}), flush=True)
    print(json.dumps({"passed": True, "cases": len(cases), "model_calls": 0}), flush=True)


if __name__ == "__main__":
    main()
