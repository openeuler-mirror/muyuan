"""Regression checks for the standalone package and branch-aware CLI."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from bate.cli import main
from bate.runtime import MetaToolExecutor
from bate.spec import MetaToolSpec


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"


@pytest.mark.parametrize("source,extra", [
    ("raw", []),
    ("graph", ["--also-raw"]),
    ("graph", ["--also-raw", "--require-fully-bound"]),
])
def test_extraction_matches_pre_migration_catalog(tmp_path, source, extra):
    assert main([
        "extract", "--traces", str(FIXTURES / "branched_traces.json"),
        "--config", str(FIXTURES / "branched_config.json"),
        "--out", str(tmp_path), "--source", source, "--max-len", "4", *extra,
    ]) == 0
    actual = json.loads((tmp_path / "active_meta_tools.json").read_text())
    expected = json.loads((FIXTURES / "branched_catalog_before_migration.json").read_text())
    assert actual == expected
    summary = json.loads((tmp_path / "extraction_summary.json").read_text())
    assert summary["result"]["admitted_meta_tools"] == 2

    for raw_spec in actual["meta_tools"]:
        spec = MetaToolSpec.from_dict(raw_spec)
        calls = []

        def lookup_customer(email):
            calls.append(("lookup_customer", email))
            return {"customer_id": "new-customer"}

        def lookup_record(customer_id):
            assert customer_id == "new-customer"
            calls.append(("lookup_record", customer_id))
            return {"record_id": "new-record"}

        def read_record(record_id):
            assert record_id == "new-record"
            calls.append(("read_record", record_id))
            return {"status": "live-result"}

        result = MetaToolExecutor({
            "lookup_customer": lookup_customer,
            "lookup_order": lookup_record,
            "lookup_invoice": lookup_record,
            "read_record": read_record,
        }).execute(spec, {"email": "unseen@example.test"})
        assert result.output == {"status": "live-result"}
        assert len(calls) == 3


def test_short_names_preserve_contracts(tmp_path):
    original = FIXTURES / "branched_catalog_before_migration.json"
    assert main([
        "extract", "--traces", str(FIXTURES / "branched_traces.json"),
        "--config", str(FIXTURES / "branched_config.json"),
        "--out", str(tmp_path), "--also-raw", "--max-len", "4",
        "--naming", "stable-short",
    ]) == 0
    specs = json.loads((tmp_path / "active_meta_tools.json").read_text())["meta_tools"]
    before = json.loads(original.read_text())["meta_tools"]
    assert len(specs) == len(before) == 2
    for new, old in zip(specs, before):
        assert new["name"].startswith("mt_") and len(new["name"]) == 15
        for field in ("steps", "output", "input_schema"):
            assert new[field] == old[field]
    summary = json.loads((tmp_path / "extraction_summary.json").read_text())
    assert summary["result"]["emitted_meta_tools"] == 2


def test_graph_plus_raw_reports_every_assessment(tmp_path, monkeypatch):
    import bate.cli as cli
    observed = {}
    for name in ("assess_all", "assess_all_graph"):
        original = getattr(cli, name)
        def wrapped(*args, _name=name, _original=original, **kwargs):
            result = _original(*args, **kwargs)
            observed[_name] = len(result)
            return result
        monkeypatch.setattr(cli, name, wrapped)
    assert cli.main([
        "extract", "--traces", str(FIXTURES / "branched_traces.json"),
        "--config", str(FIXTURES / "branched_config.json"),
        "--out", str(tmp_path), "--source", "graph", "--also-raw", "--max-len", "4",
    ]) == 0
    summary = json.loads((tmp_path / "extraction_summary.json").read_text())
    assert observed["assess_all"] > 0
    assert summary["result"]["candidates_assessed"] == sum(observed.values())


def test_sources_and_integrations_have_no_external_algorithm_dependency():
    assert not (ROOT / "src/bate/awo_bridge.py").exists()
    for folder in (ROOT / "src", ROOT / "integrations"):
        for path in folder.rglob("*.py"):
            source = path.read_text()
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Import):
                    assert all(alias.name.split(".")[0] != "awo" for alias in node.names), path
                elif isinstance(node, ast.ImportFrom):
                    assert (node.module or "").split(".")[0] != "awo", path
            assert "AWO_ROOT" not in source, path
            assert "meta-tool-online-optimized" not in source, path
        for path in folder.rglob("*.sh"):
            assert "AWO_ROOT" not in path.read_text(), path
            assert "meta-tool-online-optimized" not in path.read_text(), path


@pytest.mark.parametrize("source,extra", [("raw", []), ("graph", []), ("graph", ["--also-raw"])])
@pytest.mark.parametrize("keep_nested", [False, True])
def test_uncompilable_extension_does_not_hide_usable_prefix(tmp_path, source, extra, keep_nested):
    traces = {"traces": [{"trace_id": f"t-{i}", "success": True, "calls": [
        {"name": "lookup", "arguments": {}, "result": {"id": f"item-{i}"}, "turn_id": 0},
        {"name": "read", "arguments": {"id": f"item-{i}"},
         "result": {"token": f"token-{i}"}, "turn_id": 1},
        {"name": "inspect", "arguments": {"token": f"token-{i}", "ref": "stale-ref"},
         "result": {}, "turn_id": 2},
    ]} for i in range(3)]}
    path = tmp_path / "traces.json"
    path.write_text(json.dumps(traces))
    out = tmp_path / "out"
    assert main([
        "extract", "--traces", str(path), "--out", str(out),
        "--config", str(FIXTURES / "branched_config.json"), "--source", source,
        "--max-meta-tools", "1", *extra, *(["--keep-nested"] if keep_nested else []),
    ]) == 0
    specs = json.loads((out / "meta_tools.json").read_text())["meta_tools"]
    assert [[step["tool"] for step in spec["steps"]] for spec in specs] == [["lookup", "read"]]
