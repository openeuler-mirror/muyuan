import json
import tempfile
import unittest
from pathlib import Path

import ahe


class DetectInstallCommandTests(unittest.TestCase):
    def test_manifest_installation_command_wins(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "install.sh").write_text("#!/bin/sh\ntrue\n", encoding="utf-8")
            manifest = root / "m.json"
            manifest.write_text(json.dumps(
                {"installation": {"command": ["make", "setup"]}}), encoding="utf-8")
            self.assertEqual(
                ahe._detect_install_command(root, json.loads(manifest.read_text())),
                ["make", "setup"])

    def test_install_py_detected_without_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "install.py").write_text("pass\n", encoding="utf-8")
            self.assertEqual(
                ahe._detect_install_command(root, {}),
                ["bash", "install.sh"] if (root / "install.sh").exists()
                else [__import__("sys").executable, "install.py"])

    def test_package_json_falls_back_to_npm(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "package.json").write_text("{}", encoding="utf-8")
            self.assertEqual(ahe._detect_install_command(root, {}), ["npm", "install"])

    def test_no_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(ahe._detect_install_command(Path(temp), {}))


class InstallHarnessTests(unittest.TestCase):
    def test_runs_install_py_and_succeeds(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = root / "installed.marker"
            (root / "install.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('ok')\n",
                encoding="utf-8")
            self.assertTrue(ahe.install_harness(str(root)))
            self.assertTrue(marker.is_file())

    def test_explicit_command_overrides(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = root / "explicit.marker"
            self.assertTrue(ahe.install_harness(
                str(root),
                command=[__import__("sys").executable, "-c",
                         f"from pathlib import Path; Path({str(marker)!r}).touch()"]))
            self.assertTrue(marker.is_file())

    def test_fails_without_any_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertFalse(ahe.install_harness(str(temp)))


class PreflightTests(unittest.TestCase):
    def test_not_ready_when_node_modules_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "package.json").write_text("{}", encoding="utf-8")
            self.assertFalse(ahe.preflight(str(root)))

    def test_ready_when_probe_passes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "harness.txt").write_text("prompt", encoding="utf-8")
            manifest = root / "harness.manifest.json"
            manifest.write_text(json.dumps({
                "adapter": "command",
                "validation": {"command": ["python", "-c", "print('ok')"]},
            }), encoding="utf-8")
            self.assertTrue(ahe.preflight(str(root), str(manifest)))

    def test_not_ready_when_probe_reports_missing_dependency(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "harness.txt").write_text("prompt", encoding="utf-8")
            manifest = root / "harness.manifest.json"
            manifest.write_text(json.dumps({
                "adapter": "command",
                "validation": {"command": ["python", "-c",
                                           "raise ModuleNotFoundError('No module named x')"]},
            }), encoding="utf-8")
            self.assertFalse(ahe.preflight(str(root), str(manifest)))


if __name__ == "__main__":
    unittest.main()
