"""Host integration must preserve BATE's algorithm and fail closed."""
import importlib.util
import asyncio
import json
from pathlib import Path
import subprocess
import sys

import pytest

from bate.cli import main

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "integrations/pi-meta-tool/python"
FIXTURES = ROOT / "tests/fixtures"
definition = importlib.util.spec_from_file_location("bate_pi_discovery", ADAPTER / "discovery.py")
discovery = importlib.util.module_from_spec(definition)
definition.loader.exec_module(discovery)


def request(tmp_path):
    config = json.loads((FIXTURES / "branched_config.json").read_text())
    config["extraction"].update(max_steps=4, require_fully_bound=True)
    names = ["lookup_customer", "lookup_order", "lookup_invoice", "read_record"]
    config["admission"] = {"read_only_tools": names}
    return {
        "operation": "discover", "config": config,
        "tracePath": str(FIXTURES / "branched_traces.json"),
        "outputDir": str(tmp_path / "online"),
        "adapter": {"supportedTools": names},
    }


def test_online_uses_branch_aware_cli_without_baseline(tmp_path):
    assert not (ROOT / "src/bate/pipeline.py").exists()
    payload = request(tmp_path)
    result = discovery.discover_online(payload)
    assert result["extractor"] == "bate-branch-aware"
    assert result["activeMetaTools"] == 2
    main(["extract", "--traces", payload["tracePath"], "--out", str(tmp_path / "cli"),
          "--config", str(FIXTURES / "branched_config.json"),
          "--also-raw", "--max-len", "4", "--require-fully-bound",
          "--naming", "stable-short"])
    for filename in ("bate_only_catalog.json", "extraction_summary.json"):
        assert json.loads((tmp_path / "online" / filename).read_text()) == json.loads(
            (tmp_path / "cli" / filename).read_text())
    for spec in json.loads((tmp_path / "online/active_meta_tools.json").read_text())["meta_tools"]:
        assert spec["metadata"]["extractor"] == "bate-branch-aware"
        assert spec["metadata"]["execution_guard"] == "none"


def test_unavailable_tools_are_not_admitted(tmp_path):
    payload = request(tmp_path)
    payload["adapter"]["supportedTools"] = ["lookup_customer"]
    result = discovery.discover_online(payload)
    assert result["discoveredMetaTools"] == 2
    assert result["activeMetaTools"] == 0


def test_original_optimization_loop_cannot_be_enabled(tmp_path):
    payload = request(tmp_path)
    payload["config"]["optimization"] = {"enabled": True}
    with pytest.raises(ValueError, match="optimization loop"):
        discovery.discover_online(payload)


def test_pi_jsonl_discovery_transport(tmp_path):
    payload = request(tmp_path)
    process = subprocess.run([sys.executable, str(ADAPTER / "bridge.py")],
                             input=json.dumps(payload) + "\n", text=True,
                             capture_output=True, timeout=30)
    assert process.returncode == 0, process.stderr + process.stdout
    messages = [json.loads(line) for line in process.stdout.splitlines()]
    assert messages[-1]["type"] == "complete"
    assert messages[-1]["result"]["activeMetaTools"] == 2


@pytest.mark.parametrize("turn", [None, 0])
def test_no_claimed_savings_without_distinct_observed_turns(tmp_path, turn):
    payload = request(tmp_path)
    traces = json.loads(Path(payload.pop("tracePath")).read_text())
    for trace in traces["traces"]:
        for call in trace["calls"]:
            call["turn_id"] = turn
    payload["trace"] = traces
    result = discovery.discover_online(payload)
    assert result["discoveredMetaTools"] == 2
    assert result["activeMetaTools"] == 0


def test_admitted_workflow_runs_through_local_dispatch(tmp_path, monkeypatch):
    payload = request(tmp_path)
    result = discovery.discover_online(payload)
    monkeypatch.syspath_prepend(str(ADAPTER))
    definition = importlib.util.spec_from_file_location("bate_pi_transport", ADAPTER / "bridge.py")
    transport = importlib.util.module_from_spec(definition)
    definition.loader.exec_module(transport)
    calls = []

    class Peer:
        async def request(self, kind, payload):
            calls.append(payload)
            name, args = payload["name"], payload["input"]
            if name == "lookup_customer":
                assert args == {"email": "new@example.test"}
                return {"result": {"customer_id": "new-customer"}}
            if name in {"lookup_invoice", "lookup_order"}:
                assert args == {"customer_id": "new-customer"}
                return {"result": {"record_id": "new-record"}}
            assert name == "read_record" and args == {"record_id": "new-record"}
            return {"result": {"status": "live"}}

    value = asyncio.run(transport._run({
        "catalogFile": str(tmp_path / "online/active_meta_tools.json"),
        "metaTool": result["names"][0], "inputs": {"email": "new@example.test"},
        "supportedTools": payload["adapter"]["supportedTools"],
    }, Peer()))
    assert value["output"] == {"status": "live"}
    assert len(calls) == 3
