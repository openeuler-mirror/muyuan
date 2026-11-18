import json
import os
import tempfile
import unittest
from pathlib import Path

import ahe


def _report(root: Path, *, accepted, harness=None, has_keep=True,
            eval_task_project="", adapter=""):
    payload = {
        "mode": "component",
        "harness": str(harness or root / "harness"),
        "eval_task_project": eval_task_project,
        "adapter": adapter,
        "baseline": {"accuracy": 0.5, "tokens": 100},
        "final": {"accuracy": 0.6, "tokens": 70},
        "history": ([{"round": 1, "action": "keep", "candidate": accepted}] if has_keep else []),
        "accepted_candidate": accepted,
        "trace_dir": str(root / "trace"),
    }
    report_path = root / "ahe-component-report.json"
    report_path.write_text(json.dumps(payload), encoding="utf-8")
    return report_path


EXTENSION = '''pi.registerCommand("my-tool", { handler: (a) => {} });
pi.registerCommand('my-tool-info', { handler: (a) => {} });
const keep = "registerCommand(\\"not-a-call\\")";
'''


class RenameAndRegisterTests(unittest.TestCase):
    def test_rename_registered_commands(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "harness"
            ext = target / "assets" / "pi" / "extensions"
            ext.mkdir(parents=True)
            (ext / "my-tool.ts").write_text(EXTENSION, encoding="utf-8")
            renamed = ahe._rename_registered_commands(target, "v2")
            self.assertEqual(renamed, ["my-tool-info-v2", "my-tool-v2"])
            text = (ext / "my-tool.ts").read_text(encoding="utf-8")
            self.assertIn('pi.registerCommand("my-tool-v2"', text)
            self.assertIn("pi.registerCommand('my-tool-info-v2'", text)
            # strings that merely look like calls stay untouched
            self.assertIn('registerCommand(\\"not-a-call\\")', text)

    def test_rename_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "harness"
            ext = target / "assets" / "pi" / "extensions"
            ext.mkdir(parents=True)
            (ext / "my-tool.ts").write_text(EXTENSION, encoding="utf-8")
            ahe._rename_registered_commands(target, "v2")
            renamed_again = ahe._rename_registered_commands(target, "v2")
            self.assertEqual(renamed_again, [])

    def test_derive_task_suffix_follows_naming_rule(self):
        # 优先级：--eval-project 任务名 > adapter 名 > 空（保留原名）
        self.assertEqual(ahe._derive_task_suffix(
            {"eval_task_project": "pypto", "adapter": "bench"}), "pypto")
        self.assertEqual(ahe._derive_task_suffix(
            {"eval_task_project": "SWE-bench Lite", "adapter": ""}), "swe-bench-lite")
        self.assertEqual(ahe._derive_task_suffix(
            {"eval_task_project": "", "adapter": "bench"}), "bench")
        self.assertEqual(ahe._derive_task_suffix(
            {"eval_task_project": "", "adapter": ""}), "")

    def test_export_best_auto_registers_and_renames_by_default(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent_dir = root / "agent"
            agent_dir.mkdir()
            saved = os.environ.get("PI_CODING_AGENT_DIR")
            os.environ["PI_CODING_AGENT_DIR"] = str(agent_dir)
            try:
                candidate = root / "workspace" / "round-1-abc"
                ext = candidate / "assets" / "pi" / "extensions"
                ext.mkdir(parents=True)
                (ext / "my-tool.ts").write_text(EXTENSION, encoding="utf-8")
                report = _report(root, accepted=str(candidate), eval_task_project="pypto")
                target = root / "final"
                # 默认行为：不加任何 flag，按 <harness>-<任务名> 规则自动改名 + 自动注册
                result = ahe.export_best(report, target=str(target))
                self.assertEqual(result, target.resolve())
                settings = json.loads((agent_dir / "settings.json").read_text(encoding="utf-8"))
                self.assertIn(str(target.resolve()), settings["packages"])
                text = (target / "assets" / "pi" / "extensions" / "my-tool.ts").read_text(encoding="utf-8")
                self.assertIn('registerCommand("my-tool-pypto"', text)
                self.assertIn('registerCommand(\'my-tool-info-pypto\'', text)
            finally:
                if saved is None:
                    os.environ.pop("PI_CODING_AGENT_DIR", None)
                else:
                    os.environ["PI_CODING_AGENT_DIR"] = saved

    def test_export_best_register_writes_pi_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent_dir = root / "agent"
            agent_dir.mkdir()
            saved = os.environ.get("PI_CODING_AGENT_DIR")
            os.environ["PI_CODING_AGENT_DIR"] = str(agent_dir)
            try:
                candidate = root / "workspace" / "round-1-abc"
                ext = candidate / "assets" / "pi" / "extensions"
                ext.mkdir(parents=True)
                (ext / "my-tool.ts").write_text(EXTENSION, encoding="utf-8")
                report = _report(root, accepted=str(candidate))
                target = root / "final"
                result = ahe.export_best(report, target=str(target),
                                         register=True, suffix="v2")
                self.assertEqual(result, target.resolve())
                settings = json.loads((agent_dir / "settings.json").read_text(encoding="utf-8"))
                self.assertIn(str(target.resolve()), settings["packages"])
                text = (target / "assets" / "pi" / "extensions" / "my-tool.ts").read_text(encoding="utf-8")
                self.assertIn('registerCommand("my-tool-v2"', text)
            finally:
                if saved is None:
                    os.environ.pop("PI_CODING_AGENT_DIR", None)
                else:
                    os.environ["PI_CODING_AGENT_DIR"] = saved


class ExportBestTests(unittest.TestCase):
    def test_exports_final_accepted_candidate_with_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            candidate = root / "workspace" / "round-1-abc"
            (candidate / "prompts").mkdir(parents=True)
            (candidate / "prompts" / "system.md").write_text("v2", encoding="utf-8")
            report = _report(root, accepted=str(candidate), harness=str(root / "harness"))

            target = root / "final-harness"
            result = ahe.export_best(report, target=str(target),
                                      register=False, suffix=None)
            self.assertEqual(result, target.resolve())
            self.assertEqual((target / "prompts" / "system.md").read_text(encoding="utf-8"), "v2")
            self.assertFalse((target / "__pycache__").exists())

    def test_errors_without_accepted_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report = _report(root, accepted=None, has_keep=False)
            self.assertIsNone(ahe.export_best(report, target=str(root / "out")))
            self.assertFalse((root / "out").exists())

    def test_errors_when_target_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            candidate = root / "workspace" / "round-1-abc"
            candidate.mkdir(parents=True)
            report = _report(root, accepted=str(candidate))
            existing = root / "out"
            existing.mkdir()
            self.assertIsNone(ahe.export_best(report, target=str(existing)))


if __name__ == "__main__":
    unittest.main()


CODEX_PROMPT_TEMPLATE = """---
description: demo
argument-hint: <task>
---
<!-- ahe-plugin-root: __AHE_PLUGIN_ROOT__ -->

# Demo

- root: __AHE_PLUGIN_ROOT__
- engine: __AHE_SCRIPT__
"""


class CodexRegisterTests(unittest.TestCase):
    """--export-best 应把 assets/codex/prompts/*.md 渲染并安装为
    <CODEX_HOME>/prompts/<命令名>-<后缀>.md（与原命令共存）。"""

    def _setup_env(self, root):
        saved_pi = os.environ.get("PI_CODING_AGENT_DIR")
        saved_cx = os.environ.get("CODEX_HOME")
        os.environ["PI_CODING_AGENT_DIR"] = str(root / "agent")
        os.environ["CODEX_HOME"] = str(root / "codex-home")
        return saved_pi, saved_cx

    @staticmethod
    def _restore_env(saved_pi, saved_cx):
        for var, saved in (("PI_CODING_AGENT_DIR", saved_pi),
                           ("CODEX_HOME", saved_cx)):
            if saved is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = saved

    def _export_with_codex_prompt(self, root, **kwargs):
        candidate = root / "workspace" / "round-1-abc"
        prompts = candidate / "assets" / "codex" / "prompts"
        prompts.mkdir(parents=True)
        (prompts / "demo.md").write_text(CODEX_PROMPT_TEMPLATE, encoding="utf-8")
        (candidate / "ahe.py").write_text("# engine\n", encoding="utf-8")
        report = _report(root, accepted=str(candidate), eval_task_project="pypto")
        target = root / "final"
        result = ahe.export_best(report, target=str(target), **kwargs)
        self.assertIsNotNone(result)
        return result, Path(os.environ["CODEX_HOME"])

    def test_installs_suffix_prompt(self):
        with tempfile.TemporaryDirectory() as temp:
            saved_pi, saved_cx = self._setup_env(Path(temp))
            try:
                result, codex_home = self._export_with_codex_prompt(Path(temp))
                cmd = result / "commands" / "demo-pypto.md"
                self.assertTrue(cmd.is_file())
                text = cmd.read_text(encoding="utf-8")
                self.assertNotIn("__AHE_PLUGIN_ROOT__", text)
                self.assertNotIn("__AHE_SCRIPT__", text)
                self.assertIn(str(result), text)
                self.assertIn(str(result / "ahe.py"), text)
                manifest = json.loads(
                    (result / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
                self.assertIn("-pypto", manifest["name"])
                market = json.loads(
                    (result / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))
                self.assertEqual(market["plugins"][0]["name"], manifest["name"])
            finally:
                self._restore_env(saved_pi, saved_cx)

    def test_custom_suffix(self):
        with tempfile.TemporaryDirectory() as temp:
            saved_pi, saved_cx = self._setup_env(Path(temp))
            try:
                result, _ = self._export_with_codex_prompt(Path(temp), suffix="v3")
                self.assertTrue((result / "commands" / "demo-v3.md").is_file())
                self.assertFalse((result / "commands" / "demo-v2.md").is_file())
            finally:
                self._restore_env(saved_pi, saved_cx)

    def test_no_register_skips_codex(self):
        with tempfile.TemporaryDirectory() as temp:
            saved_pi, saved_cx = self._setup_env(Path(temp))
            try:
                result, _ = self._export_with_codex_prompt(Path(temp), register=False)
                self.assertFalse((result / "commands").is_dir())
            finally:
                self._restore_env(saved_pi, saved_cx)

    def test_no_templates_no_codex_install(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            saved_pi, saved_cx = self._setup_env(root)
            try:
                candidate = root / "workspace" / "round-1-abc"
                candidate.mkdir(parents=True)
                report = _report(root, accepted=str(candidate))
                result = ahe.export_best(report, target=str(root / "final"))
                self.assertIsNotNone(result)
                self.assertFalse((result / "commands").is_dir())
            finally:
                self._restore_env(saved_pi, saved_cx)
