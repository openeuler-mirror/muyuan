import json
import os
import tempfile
import unittest
from pathlib import Path

import ahe


VALID_SOURCE = '''# AHE-OBJECTIVE: {"metric": "accuracy", "direction": "maximize", "description": "pass rate"}
import json
import os
import time


def main() -> int:
    candidate = os.environ["AHE_HARNESS_ROOT"]
    t0 = time.time()
    marker = os.path.join(candidate, "prompts", "system.md")
    accuracy = 1.0 if os.path.isfile(marker) else 0.0
    print(json.dumps({"accuracy": accuracy, "tokens": 0, "time_s": time.time() - t0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


class GenerateEvaluatorTests(unittest.TestCase):
    def _setup(self, temp: str):
        harness = Path(temp) / "harness"
        (harness / "prompts").mkdir(parents=True)
        (harness / "prompts" / "system.md").write_text("be brief", encoding="utf-8")
        manifest = harness / "harness.manifest.json"
        manifest.write_text(json.dumps({
            "name": "demo",
            "adapter": "command",
            "components": [{"id": "prompts", "kind": "prompt", "path": "prompts", "mutable": True}],
            "validation": {"command": ["python", "-m", "compileall", "-q", "src"]},
        }), encoding="utf-8")
        return harness, manifest

    def _api(self):
        return {"url": "mock", "key": "mock", "model": "mock", "evolve_model": "mock"}

    def test_generates_evaluator_and_updates_manifest_objective(self):
        with tempfile.TemporaryDirectory() as temp:
            harness, manifest = self._setup(temp)
            target = Path(temp) / "ahe_evaluator.py"
            result = ahe.generate_evaluator(
                harness=str(harness), manifest_path=str(manifest),
                task="keep answers accurate while cutting tokens",
                api=self._api(), target=str(target),
                call_llm_fn=lambda *a, **k: (VALID_SOURCE, 10, 0.1),
            )
            self.assertEqual(result, target)
            source = target.read_text(encoding="utf-8")
            compile(source, str(target), "exec")
            self.assertIn("AHE_HARNESS_ROOT", source)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(payload["objective"]["metric"], "accuracy")
            self.assertEqual(payload["objective"]["direction"], "maximize")
            backup = manifest.with_suffix(manifest.suffix + ".ahe-backup")
            self.assertTrue(backup.is_file())

    def test_rejects_source_without_objective_header(self):
        with tempfile.TemporaryDirectory() as temp:
            harness, manifest = self._setup(temp)
            target = Path(temp) / "ahe_evaluator.py"
            bad = "import os\nprint('no header, no contract')\n"
            result = ahe.generate_evaluator(
                harness=str(harness), manifest_path=str(manifest),
                task="task",
                api=self._api(), target=str(target),
                call_llm_fn=lambda *a, **k: (bad, 10, 0.1),
            )
            self.assertIsNone(result)
            self.assertFalse(target.exists())
            rejected = target.with_suffix(target.suffix + ".rejected")
            self.assertTrue(rejected.is_file())
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertNotIn("objective", payload)

    def test_rejects_source_that_does_not_compile(self):
        with tempfile.TemporaryDirectory() as temp:
            harness, manifest = self._setup(temp)
            target = Path(temp) / "ahe_evaluator.py"
            broken = VALID_SOURCE + "\nthis is not python(\n"
            result = ahe.generate_evaluator(
                harness=str(harness), manifest_path=str(manifest),
                task="task",
                api=self._api(), target=str(target),
                call_llm_fn=lambda *a, **k: (broken, 10, 0.1),
            )
            self.assertIsNone(result)
            self.assertFalse(target.exists())


class LoadApiConfigTests(unittest.TestCase):
    ENV_KEYS = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "EVOLVE_LLM_MODEL",
                "AHE_ENV_FILE", "PI_PROVIDER", "PI_MODEL", "PI_CODING_AGENT_DIR")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for k in self.ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _agent_dir(self, temp: str) -> Path:
        agent = Path(temp) / "agent"
        agent.mkdir()
        (agent / "models.json").write_text(json.dumps({
            "providers": {
                "csi-provider": {
                    "baseUrl": "http://gateway.example/v1",
                    "apiKey": "sk-test",
                    "models": [{"id": "GLM-5.3"}],
                }
            }
        }), encoding="utf-8")
        return agent

    def test_inherits_pi_session_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            agent = self._agent_dir(temp)
            os.environ["PI_PROVIDER"] = "csi-provider"
            os.environ["PI_MODEL"] = "GLM-5.3"
            os.environ["PI_CODING_AGENT_DIR"] = str(agent)
            api = ahe.load_api_config()
            self.assertEqual(api["url"], "http://gateway.example/v1")
            self.assertEqual(api["key"], "sk-test")
            self.assertEqual(api["model"], "GLM-5.3")

    def test_explicit_env_file_beats_pi_inheritance(self):
        with tempfile.TemporaryDirectory() as temp:
            agent = self._agent_dir(temp)
            env_file = Path(temp) / "custom.env"
            env_file.write_text("LLM_BASE_URL=http://file.example/v1\n"
                                "LLM_API_KEY=sk-file\n", encoding="utf-8")
            os.environ["PI_PROVIDER"] = "csi-provider"
            os.environ["PI_MODEL"] = "GLM-5.3"
            os.environ["PI_CODING_AGENT_DIR"] = str(agent)
            os.environ["AHE_ENV_FILE"] = str(env_file)
            api = ahe.load_api_config()
            self.assertEqual(api["url"], "http://file.example/v1")
            self.assertEqual(api["key"], "sk-file")

    def test_auth_json_key_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            agent = self._agent_dir(temp)
            models = agent / "models.json"
            payload = json.loads(models.read_text(encoding="utf-8"))
            payload["providers"]["csi-provider"].pop("apiKey")
            models.write_text(json.dumps(payload), encoding="utf-8")
            (agent / "auth.json").write_text(
                json.dumps({"csi-provider": {"apiKey": "sk-auth"}}), encoding="utf-8")
            os.environ["PI_PROVIDER"] = "csi-provider"
            os.environ["PI_MODEL"] = "GLM-5.3"
            os.environ["PI_CODING_AGENT_DIR"] = str(agent)
            api = ahe.load_api_config()
            self.assertEqual(api["key"], "sk-auth")


if __name__ == "__main__":
    unittest.main()
