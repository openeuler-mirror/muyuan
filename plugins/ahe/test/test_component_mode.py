import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from component_mode import run_component_mode, _acceptable
from harness_optimizer.schema import HarnessSpec


class KeepGateTests(unittest.TestCase):
    def _spec(self):
        return HarnessSpec.from_dict({
            "name": "gates",
            "adapter": {"name": "generic"},
            "components": [{"id": "p", "kind": "prompt", "path": "p.txt",
                            "mutable": True}],
            "validation": {"command": "true"},
        })

    def _check(self, cand, best, **env):
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update({k: str(v) for k, v in env.items()})
        try:
            return _acceptable(cand, best, None, None, self._spec())
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_multi_objective_keeps_any_improvement_above_floor(self):
        base = {"runner_ok": True, "accuracy": 26.0, "f1": 66, "tokens": 121000000}
        better_iters = {"runner_ok": True, "accuracy": 26.0, "f1": 70, "tokens": 121000000}
        fewer_tokens = {"runner_ok": True, "accuracy": 26.0, "f1": 66, "tokens": 110000000}
        better_acc = {"runner_ok": True, "accuracy": 27.0, "f1": 60, "tokens": 130000000}
        worse_all = {"runner_ok": True, "accuracy": 25.0, "f1": 60, "tokens": 130000000}
        for cand in (better_iters, fewer_tokens, better_acc):
            self.assertTrue(self._check(cand, base, AHE_KEEP_MULTI_OBJECTIVE=1))
        self.assertFalse(self._check(worse_all, base, AHE_KEEP_MULTI_OBJECTIVE=1))

    def test_multi_objective_enforces_accuracy_floor(self):
        base = {"runner_ok": True, "accuracy": 26.0, "f1": 66, "tokens": 121000000}
        low_acc_more_iters = {"runner_ok": True, "accuracy": 15.0, "f1": 90,
                              "tokens": 50000000}
        self.assertFalse(
            self._check(low_acc_more_iters, base, AHE_KEEP_MULTI_OBJECTIVE=1))
        self.assertFalse(
            self._check(low_acc_more_iters, base,
                        AHE_KEEP_MULTI_OBJECTIVE=1, AHE_MULTI_OBJ_IMPR_FLOOR=20))
        above_lower_floor = {"runner_ok": True, "accuracy": 12.0, "f1": 90,
                             "tokens": 50000000}
        self.assertTrue(
            self._check(above_lower_floor, base,
                        AHE_KEEP_MULTI_OBJECTIVE=1, AHE_MULTI_OBJ_IMPR_FLOOR=10))

    def test_multi_objective_rejects_runner_not_ok(self):
        base = {"runner_ok": True, "accuracy": 26.0, "f1": 66, "tokens": 121000000}
        broken = {"runner_ok": False, "accuracy": 40.0, "f1": 100, "tokens": 1000}
        self.assertFalse(self._check(broken, base, AHE_KEEP_MULTI_OBJECTIVE=1))

    def test_default_gate_without_env(self):
        base = {"runner_ok": True, "accuracy": 26.0, "f1": 66, "tokens": 121000000}
        better = {"runner_ok": True, "accuracy": 27.0, "f1": 66, "tokens": 121000000}
        cleared = {key: "" for key in ("AHE_KEEP_MULTI_OBJECTIVE", "AHE_KEEP_ACCURACY_ONLY",
                                       "AHE_KEEP_TRI_METRIC", "AHE_KEEP_EFFICIENCY_STRICT",
                                       "AHE_LENIENT_KEEP", "AHE_KEEP_SPARSE_METRICS")}
        self.assertTrue(self._check(better, base, **cleared))


class ComponentModeTests(unittest.TestCase):
    def _manifest(self, root: Path) -> Path:
        manifest = root / "harness.manifest.json"
        manifest.write_text(json.dumps({
            "name": "demo",
            "version": 1,
            "adapter": "command",
            "components": [{
                "id": "prompt",
                "kind": "prompt",
                "path": "prompt.txt",
                "mutable": True,
            }],
            "validation": {},
        }), encoding="utf-8")
        return manifest

    def _evaluator(self):
        code = (
            "import json, os; "
            "from pathlib import Path; "
            "p=Path(os.environ['AHE_HARNESS_ROOT'])/'prompt.txt'; "
            "print(json.dumps({'accuracy': 1.0, 'tokens': len(p.read_text()), 'time_s': 0.1}))"
        )
        return [sys.executable, "-c", code]

    def test_baseline_does_not_need_api(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "harness"
            root.mkdir()
            (root / "prompt.txt").write_text("unchanged", encoding="utf-8")
            report = run_component_mode(
                harness_path_str=str(root),
                manifest_path=str(self._manifest(root)),
                task_prompt="smoke",
                targets={},
                rounds=0,
                eval_command=self._evaluator(),
                holdout_eval_command=None,
                baseline_only=True,
                api={},
                call_llm=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
                trace_dir=str(Path(temp) / "traces"),
            )
            self.assertEqual(report["final"]["accuracy"], 1.0)
            self.assertEqual((root / "prompt.txt").read_text(), "unchanged")

    def test_baseline_reuse_keeps_single_candidate_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "harness"
            root.mkdir()
            (root / "prompt.txt").write_text("unchanged", encoding="utf-8")
            reuse = Path(temp) / "previous-baseline.json"
            reuse.write_text(json.dumps({
                "metrics": {"runner_ok": True, "accuracy": 0.9, "tokens": 5, "time_s": 0.1},
            }), encoding="utf-8")
            previous = os.environ.get("AHE_REUSE_BASELINE")
            os.environ["AHE_REUSE_BASELINE"] = str(reuse)
            try:
                report = run_component_mode(
                    harness_path_str=str(root),
                    manifest_path=str(self._manifest(root)),
                    task_prompt="smoke",
                    targets={},
                    rounds=0,
                    eval_command=self._evaluator(),
                    holdout_eval_command=None,
                    baseline_only=True,
                    api={},
                    call_llm=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
                    workspace_dir=str(Path(temp) / "candidates"),
                    trace_dir=str(Path(temp) / "traces"),
                )
            finally:
                if previous is None:
                    os.environ.pop("AHE_REUSE_BASELINE", None)
                else:
                    os.environ["AHE_REUSE_BASELINE"] = previous
            self.assertEqual(report["baseline"]["accuracy"], 0.9)
            checkouts = sorted(item.name for item in (Path(temp) / "candidates").iterdir())
            # 复用指标绝不额外滞留第二份完整 checkout。
            self.assertEqual(len(checkouts), 1)
            self.assertTrue(checkouts[0].startswith("baseline-"))
            self.assertEqual(Path(report["baseline_candidate"]).name, checkouts[0])
            self.assertTrue(Path(report["baseline_candidate"]).is_dir())

    def test_candidate_change_is_evaluated_and_staged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "harness"
            root.mkdir()
            (root / "prompt.txt").write_text("a very long prompt", encoding="utf-8")
            manifest = self._manifest(root)

            def fake_llm(*_args, **_kwargs):
                payload = {
                    "reason": "remove redundant wording",
                    "evidence_ids": ["e1"],
                    "changes": [{"component_id": "prompt", "operation": "modify", "content": "short"}],
                }
                return json.dumps(payload), 1, 0.0

            report = run_component_mode(
                harness_path_str=str(root),
                manifest_path=str(manifest),
                task_prompt="smoke",
                targets={},
                rounds=1,
                eval_command=self._evaluator(),
                holdout_eval_command=None,
                baseline_only=False,
                api={"url": "mock", "key": "mock", "evolve_model": "mock"},
                call_llm=fake_llm,
                workspace_dir=str(Path(temp) / "candidates"),
                trace_dir=str(Path(temp) / "traces"),
                write_back=False,
            )
            self.assertEqual(report["history"][-1]["action"], "keep")
            self.assertLess(report["final"]["tokens"], report["baseline"]["tokens"])
            self.assertEqual((root / "prompt.txt").read_text(), "a very long prompt")

    def test_structured_patch_replaces_one_exact_range(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "harness"
            root.mkdir()
            baseline_text = "prefix\nold branch\nsuffix\n"
            (root / "prompt.txt").write_text(baseline_text, encoding="utf-8")
            manifest = self._manifest(root)
            manifest.write_text(json.dumps({
                "name": "structured-demo",
                "patch_mode": "structured",
                "adapter": "command",
                "components": [{
                    "id": "prompt", "kind": "prompt", "path": "prompt.txt", "mutable": True,
                }],
                "validation": {},
            }), encoding="utf-8")
            evaluator = [
                sys.executable, "-c",
                "import json,os; from pathlib import Path; "
                "text=(Path(os.environ['AHE_HARNESS_ROOT'])/'prompt.txt').read_text(); "
                "print(json.dumps({'accuracy': 1, 'tokens': len(text), 'time_s': 0.1}))",
            ]

            def fake_llm(*_args, **_kwargs):
                expected = hashlib.sha256(baseline_text.encode()).hexdigest()
                return json.dumps({"changes": [
                    {
                        "component_id": "prompt",
                        "operation": "replace_text",
                        "old": "old branch",
                        "new": "new",
                        "expected_file_sha256": expected,
                    },
                    {
                        "component_id": "prompt",
                        "operation": "replace_text",
                        "old": "suffix",
                        "new": "tail",
                        "expected_file_sha256": expected,
                    },
                ]}), 1, 0.0

            report = run_component_mode(
                harness_path_str=str(root), manifest_path=str(manifest), task_prompt="structured smoke",
                targets={}, rounds=1, eval_command=evaluator, holdout_eval_command=None,
                baseline_only=False,
                api={"url": "mock", "key": "mock", "evolve_model": "mock"},
                call_llm=fake_llm,
                workspace_dir=str(Path(temp) / "candidates"),
                trace_dir=str(Path(temp) / "traces"), write_back=True,
            )
            self.assertEqual(report["history"][-1]["action"], "keep")
            self.assertTrue(report["write_back"])
            self.assertEqual((root / "prompt.txt").read_text(), "prefix\nnew\ntail\n")

    def test_accepted_candidate_can_be_promoted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "harness"
            root.mkdir()
            (root / "prompt.txt").write_text("a very long prompt", encoding="utf-8")
            manifest = self._manifest(root)

            def fake_llm(*_args, **_kwargs):
                return json.dumps({
                    "reason": "shorten",
                    "changes": [{"component_id": "prompt", "operation": "modify", "content": "short"}],
                }), 1, 0.0

            report = run_component_mode(
                harness_path_str=str(root),
                manifest_path=str(manifest),
                task_prompt="smoke",
                targets={},
                rounds=1,
                eval_command=self._evaluator(),
                holdout_eval_command=None,
                baseline_only=False,
                api={"url": "mock", "key": "mock", "evolve_model": "mock"},
                call_llm=fake_llm,
                workspace_dir=str(Path(temp) / "candidates"),
                trace_dir=str(Path(temp) / "traces"),
            )
            self.assertTrue(report["write_back"])
            self.assertEqual((root / "prompt.txt").read_text(), "short")
            self.assertTrue(Path(report["backup"]).is_dir())

    def test_references_and_failure_debugger_feed_next_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "harness"
            root.mkdir()
            (root / "runtime.txt").write_text("broken", encoding="utf-8")
            reference = Path(temp) / "sol-pi-reference.md"
            reference.write_text("action fusion and evidence-preserving observation packing", encoding="utf-8")
            manifest = root / "harness.manifest.json"
            manifest.write_text(json.dumps({
                "name": "debuggable-harness",
                "adapter": "command",
                "baseline_frozen": True,
                "components": [{"id": "runtime", "kind": "runtime", "path": "runtime.txt", "mutable": True}],
                "objective": {"metric": "accuracy", "direction": "maximize"},
                "references": [{"id": "sol-pi", "path": str(reference)}],
                "debugger": {"command": [sys.executable, "-c", "import json; print(json.dumps({'root_cause':'missing recovery'}))"]},
            }), encoding="utf-8")
            evaluator = [
                sys.executable, "-c",
                "import json,os,sys; from pathlib import Path; "
                "text=(Path(os.environ['AHE_HARNESS_ROOT'])/'runtime.txt').read_text(); "
                "print(json.dumps({'accuracy': 1 if text == 'fixed' else 0})); "
                "sys.exit(0 if text == 'fixed' else 1)",
            ]
            prompts = []

            def fake_llm(_url, _key, _model, messages, **_kwargs):
                prompts.append(messages[-1]["content"])
                return json.dumps({"changes": [{"component_id": "runtime", "operation": "modify", "content": "fixed"}]}), 1, 0.0

            report = run_component_mode(
                harness_path_str=str(root), manifest_path=str(manifest), task_prompt="debug smoke",
                targets={}, rounds=1, eval_command=evaluator, holdout_eval_command=None,
                baseline_only=False, api={"url": "mock", "key": "mock", "evolve_model": "mock"},
                call_llm=fake_llm, workspace_dir=str(Path(temp) / "candidates"),
                trace_dir=str(Path(temp) / "traces"), write_back=True,
            )
            self.assertIn("action fusion and evidence-preserving observation packing", prompts[0])
            self.assertTrue(report["history"][0]["debugger"][0]["ok"])
            self.assertEqual(report["history"][0]["debugger"][0]["analysis"]["root_cause"], "missing recovery")
            diagnosis = report["history"][0]["debugger"][0]["diagnosis"]
            self.assertEqual(diagnosis["type"], "ahe.diagnosis")
            self.assertEqual(diagnosis["schema_version"], 1)
            self.assertEqual(diagnosis["root_cause"], "missing recovery")
            self.assertEqual(
                diagnosis["candidate_id"],
                report["history"][0]["debugger"][0]["candidate_id"],
            )
            diagnosis_files = list((Path(temp) / "traces" / "debugger").glob("*.diagnosis.json"))
            self.assertEqual(len(diagnosis_files), 1)
            self.assertTrue((Path(temp) / "traces" / "diagnosis.jsonl").is_file())
            self.assertTrue(report["history"][1]["debugger"] is None)
            self.assertEqual((root / "runtime.txt").read_text(), "broken")

    def test_failed_run_uses_builtin_llm_debugger_when_no_command_is_declared(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "harness"
            root.mkdir()
            (root / "runtime.txt").write_text("broken", encoding="utf-8")
            manifest = root / "harness.manifest.json"
            manifest.write_text(json.dumps({
                "name": "builtin-debugger",
                "adapter": "command",
                "components": [{"id": "runtime", "kind": "runtime", "path": "runtime.txt", "mutable": True}],
                "objective": {"metric": "accuracy", "direction": "maximize"},
            }), encoding="utf-8")
            evaluator = [
                sys.executable, "-c",
                "import json,os,sys; from pathlib import Path; "
                "text=(Path(os.environ['AHE_HARNESS_ROOT'])/'runtime.txt').read_text(); "
                "print(json.dumps({'accuracy': 1 if text == 'fixed' else 0})); "
                "sys.exit(0 if text == 'fixed' else 1)",
            ]
            calls = []

            def fake_llm(_url, _key, model, messages, **_kwargs):
                calls.append(messages)
                if "diagnosis" in messages[0]["content"]:
                    return json.dumps({"root_cause": "broken runtime", "evidence_ids": ["e1"]}), 4, 0.01
                return json.dumps({"changes": [{"component_id": "runtime", "operation": "modify", "content": "fixed"}]}), 5, 0.01

            report = run_component_mode(
                harness_path_str=str(root), manifest_path=str(manifest), task_prompt="builtin debug smoke",
                targets={}, rounds=1, eval_command=evaluator, holdout_eval_command=None,
                baseline_only=False, api={"url": "mock", "key": "mock", "evolve_model": "mock"},
                call_llm=fake_llm, workspace_dir=str(Path(temp) / "candidates"),
                trace_dir=str(Path(temp) / "traces"), write_back=False,
            )
            self.assertEqual(report["history"][0]["debugger"][0]["kind"], "llm")
            self.assertEqual(report["history"][0]["debugger"][0]["analysis"]["root_cause"], "broken runtime")
            self.assertEqual(
                report["history"][0]["debugger"][0]["diagnosis"]["root_cause"],
                "broken runtime",
            )
            self.assertEqual(report["final"]["accuracy"], 1)
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
