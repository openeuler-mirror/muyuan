import tempfile
import unittest
from pathlib import Path

from harness_optimizer.surface import discover_surface
from harness_optimizer.schema import HarnessSpec


class HarnessSurfaceDiscoveryTests(unittest.TestCase):
    def test_keeps_workload_and_evaluator_out_of_mutable_surface(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".pi/prompts/system.md").parent.mkdir(parents=True)
            (root / ".pi/prompts/system.md").write_text("prompt", encoding="utf-8")
            (root / "tools/search.py").parent.mkdir(parents=True)
            (root / "tools/search.py").write_text("pass", encoding="utf-8")
            (root / "src/agent.py").parent.mkdir(parents=True)
            (root / "src/agent.py").write_text("pass", encoding="utf-8")
            (root / "evaluator.py").write_text("print('{}')", encoding="utf-8")
            (root / "models/kernel.py").parent.mkdir(parents=True)
            (root / "models/kernel.py").write_text("pass", encoding="utf-8")
            (root / "workload/input.json").parent.mkdir(parents=True)
            (root / "workload/input.json").write_text("{}", encoding="utf-8")

            result = discover_surface(
                root,
                task="optimize the agent harness",
                task_root=root / "workload",
                eval_command="python evaluator.py --input workload/input.json",
                validation_command="python -m compileall .",
            )
            paths = {item["path"] for item in result.manifest["components"]}
            self.assertEqual(paths, {".pi/prompts/system.md", "src/agent.py", "tools/search.py"})
            self.assertTrue(any(item["path"] == "evaluator.py" for item in result.excluded))
            self.assertTrue(any(item["path"] == "workload/input.json" for item in result.excluded))
            HarnessSpec.from_dict(result.manifest)

    def test_missing_boundaries_are_reported_without_broadening_surface(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "prompts/main.md").parent.mkdir(parents=True)
            (root / "prompts/main.md").write_text("prompt", encoding="utf-8")
            result = discover_surface(root)
            self.assertTrue(any("validation" in warning for warning in result.warnings))
            self.assertTrue(any("evaluator" in warning for warning in result.warnings))
            self.assertEqual(result.manifest["components"][0]["path"], "prompts/main.md")

    def test_covers_all_seven_ahe_component_classes(self):
        """The discovery must recognize every mount point of the AHE paper's
        seven editable component types (arXiv:2604.25850) as high-confidence
        mutable components, so generated manifests do not starve the
        optimizer of the classes the paper's ablation says carry the gain."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            seven = {
                "system_prompt": "assets/instructions.txt",
                "tool_description": "assets/tools/search.md",
                "tool_implementation": "src/tools/candidate_creation.py",
                "middleware": "assets/pi/extensions/main.ts",
                "skill": ".agents/skills/review/SKILL.md",
                "sub_agent_config": "assets/codex/agents/worker.toml",
                "long_term_memory": "memory/ledger.py",
            }
            for rel in seven.values():
                target = root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("content", encoding="utf-8")
            result = discover_surface(
                root,
                task="optimize the harness",
                validation_command="python -m compileall .",
            )
            paths = {item["path"] for item in result.manifest["components"]}
            for rel in seven.values():
                self.assertIn(rel, paths, f"missing AHE component mount point: {rel}")
            classes = result.manifest["discovery"]["component_classes"]
            self.assertEqual(len(classes), 7)
            self.assertEqual(classes[0]["class"], "system_prompt")


if __name__ == "__main__":
    unittest.main()
