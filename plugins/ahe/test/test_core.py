import json
import sys
import tempfile
import unittest
from pathlib import Path

from harness_optimizer import (
    HarnessSpec,
    apply_changes,
    create_candidate,
    find_manifest,
    iter_jsonl,
    parse_metrics,
    run_command,
    write_evidence,
    write_trace,
)


class GenericOptimizerTests(unittest.TestCase):
    def test_manifest_discovery_ignores_plugin_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "manifest.json").write_text(json.dumps({
                "name": "pi-extension", "type": "pi-extension"
            }), encoding="utf-8")
            harness_manifest = root / "harness.manifest.json"
            harness_manifest.write_text(json.dumps({
                "name": "demo",
                "components": [{"id": "prompt", "kind": "prompt", "path": "prompt.txt"}],
            }), encoding="utf-8")
            self.assertEqual(find_manifest(root), harness_manifest)

    def test_manifest_and_candidate_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "harness"
            source.mkdir()
            (source / "prompt.txt").write_text("before", encoding="utf-8")
            spec = HarnessSpec.from_dict({
                "name": "demo",
                "components": [{"id": "prompt", "kind": "file", "path": "prompt.txt", "mutable": True}],
                "validation": {"command": ["echo", '{"accuracy": 1}']},
                "adapter": "command",
            }, root=source)
            candidate = create_candidate(source, root / "candidates", "c1", spec=spec)
            apply_changes(candidate, {"prompt.txt": "after"})
            self.assertEqual((candidate.path / "prompt.txt").read_text(encoding="utf-8"), "after")
            self.assertEqual((source / "prompt.txt").read_text(encoding="utf-8"), "before")
            candidate.rollback()
            self.assertEqual((candidate.path / "prompt.txt").read_text(encoding="utf-8"), "before")

    def test_safe_path_and_immutable_component(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "harness"
            source.mkdir()
            (source / "fixed.txt").write_text("fixed", encoding="utf-8")
            spec = HarnessSpec.from_dict({
                "name": "demo",
                "components": [{"id": "fixed", "kind": "file", "path": "fixed.txt", "mutable": False}],
            })
            candidate = create_candidate(source, root / "candidates", "c1", spec=spec)
            with self.assertRaises(PermissionError):
                apply_changes(candidate, {"fixed.txt": "changed"})
            with self.assertRaises(ValueError):
                apply_changes(candidate, {"../escape": "bad"})

    def test_run_parse_and_jsonl(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = run_command(
                [sys.executable, "-c", "print('{\"tokens\": 3, \"accuracy\": 0.5}')"],
                root)
            self.assertTrue(result.ok)
            self.assertEqual(parse_metrics(result.stdout)["tokens"], 3)
            path = root / "trace.jsonl"
            write_trace(path, "run_finished", candidate_id="c1", payload={"ok": True})
            write_evidence(path, "metrics", candidate_id="c1", metrics={"accuracy": 0.5})
            rows = list(iter_jsonl(path))
            self.assertEqual([row["type"] for row in rows], ["trace", "evidence"])


if __name__ == "__main__":
    unittest.main()
