import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from artifact_bridge import ArtifactBridgeError, git_commit_artifacts, publish_run_artifacts


class ArtifactBridgeTests(unittest.TestCase):
    def _git_repo(self, root: Path) -> Path:
        root.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "ahe@example.com"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "ahe"], check=True)
        return root

    def _report(self, root: Path) -> Path:
        trace = root / "trace-source"
        trace.mkdir()
        (trace / "trace.jsonl").write_text('{"event":"evaluation"}\n', encoding="utf-8")
        report = {
            "mode": "component",
            "run_id": "demo-run",
            "timestamp": "2026-10-16T00:00:00+00:00",
            "harness": "/tmp/harness",
            "task": "shorten harness",
            "rounds": 1,
            "baseline": {"accuracy": 0.8, "f1": 0.7, "tokens": 100, "time_s": 10},
            "final": {"accuracy": 0.9, "f1": 0.8, "tokens": 80, "time_s": 7},
            "change_manifest": [{"round": 1, "action": "keep", "reason": "remove duplicate context"}],
            "trace_dir": str(trace),
        }
        report_path = root / "ahe-component-report.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        return report_path

    def test_publish_preserves_metrics_changes_and_trace(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            repo = self._git_repo(temp_path / "repo")
            report = self._report(temp_path)
            result = publish_run_artifacts(report, repo)
            destination = Path(result["destination"])
            self.assertEqual(result["run_id"], "demo-run")
            self.assertEqual(result["delta"]["tokens_saved"], 20)
            self.assertEqual(result["delta"]["time_saved_s"], 3)
            self.assertEqual(result["delta"]["f1_delta"], 0.1)
            self.assertEqual(result["change_count"], 1)
            self.assertTrue((destination / "report.json").is_file())
            self.assertTrue((destination / "summary.json").is_file())
            self.assertTrue((destination / "changes" / "round-001.json").is_file())
            self.assertTrue((destination / "trace" / "trace.jsonl").is_file())

    def test_output_root_must_stay_inside_repo(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            repo = self._git_repo(temp_path / "repo")
            report = self._report(temp_path)
            with self.assertRaises(ArtifactBridgeError):
                publish_run_artifacts(report, repo, output_root="../outside")

    def test_existing_run_is_not_overwritten_by_default(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            repo = self._git_repo(temp_path / "repo")
            report = self._report(temp_path)
            publish_run_artifacts(report, repo)
            with self.assertRaises(ArtifactBridgeError):
                publish_run_artifacts(report, repo)

    def _rewire_trace_dir(self, report: Path, trace_dir: Path) -> None:
        payload = json.loads(report.read_text(encoding="utf-8"))
        payload["trace_dir"] = str(trace_dir)
        report.write_text(json.dumps(payload), encoding="utf-8")

    def _isolated_report(self, temp_path: Path) -> Path:
        """把报告放进独立子目录，使 temp 根下的其他目录真正越界。"""
        report_root = temp_path / "report-root"
        report_root.mkdir()
        return self._report(report_root)

    def test_trace_dir_outside_report_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            repo = self._git_repo(temp_path / "repo")
            outside = temp_path / "elsewhere"
            outside.mkdir()
            (outside / "trace.jsonl").write_text("{}\n", encoding="utf-8")
            report = self._isolated_report(temp_path)
            self._rewire_trace_dir(report, outside)
            with self.assertRaises(ArtifactBridgeError):
                publish_run_artifacts(report, repo)
            # 默认根之外没有被复制进仓库。
            self.assertFalse((repo / "autoharness").exists())

    def test_explicit_source_root_allows_external_trace(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            repo = self._git_repo(temp_path / "repo")
            outside = temp_path / "elsewhere"
            outside.mkdir()
            (outside / "trace.jsonl").write_text("{}\n", encoding="utf-8")
            report = self._isolated_report(temp_path)
            self._rewire_trace_dir(report, outside)
            result = publish_run_artifacts(report, repo, allowed_source_roots=[outside])
            destination = Path(result["destination"])
            self.assertTrue((destination / "trace" / "trace.jsonl").is_file())

    def test_include_candidates_outside_default_roots_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            repo = self._git_repo(temp_path / "repo")
            candidates = temp_path / "candidates"
            (candidates / "round-1").mkdir(parents=True)
            (candidates / "round-1" / "prompt.txt").write_text("short", encoding="utf-8")
            report = self._isolated_report(temp_path)
            payload = json.loads(report.read_text(encoding="utf-8"))
            payload["workspace_dir"] = str(candidates)
            report.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ArtifactBridgeError):
                publish_run_artifacts(report, repo, include_candidates=True)
            result = publish_run_artifacts(
                report, repo, include_candidates=True, allowed_source_roots=[candidates])
            destination = Path(result["destination"])
            self.assertTrue((destination / "candidates" / "round-1" / "prompt.txt").is_file())

    def _committed_names(self, repo: Path) -> list[str]:
        output = subprocess.run(
            ["git", "-C", str(repo), "show", "--name-only", "--format=", "HEAD"],
            capture_output=True, text=True, check=True).stdout
        return [line.strip() for line in output.splitlines() if line.strip()]

    def test_commit_excludes_unrelated_staged_files(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            repo = self._git_repo(temp_path / "repo")
            report = self._report(temp_path)
            (repo / "unrelated.txt").write_text("in-progress work", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "--", "unrelated.txt"], check=True)
            result = publish_run_artifacts(report, repo)
            commit_result = git_commit_artifacts(repo, result["destination"])
            self.assertTrue(commit_result["committed"])
            names = self._committed_names(repo)
            self.assertTrue(names)
            self.assertTrue(all(name.startswith("autoharness/") for name in names))
            self.assertNotIn("unrelated.txt", names)
            # 调用方预先暂存的文件原样保留在暂存区，未被提交。
            staged = subprocess.run(
                ["git", "-C", str(repo), "diff", "--cached", "--name-only"],
                capture_output=True, text=True, check=True).stdout
            self.assertIn("unrelated.txt", staged)

    def test_push_does_not_publish_unrelated_staged_files(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            repo = self._git_repo(temp_path / "repo")
            remote = temp_path / "remote.git"
            subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True)
            report = self._report(temp_path)
            (repo / "secret.txt").write_text("in-progress work", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "--", "secret.txt"], check=True)
            result = publish_run_artifacts(report, repo)
            branch = subprocess.run(
                ["git", "-C", str(repo), "branch", "--show-current"],
                capture_output=True, text=True, check=True).stdout.strip()
            commit_result = git_commit_artifacts(
                repo, result["destination"], push=True, branch=branch)
            self.assertTrue(commit_result["pushed"])
            listed = subprocess.run(
                ["git", "-C", str(remote), "ls-tree", "-r", "--name-only", branch],
                capture_output=True, text=True, check=True).stdout.splitlines()
            self.assertTrue(any(name.endswith("/report.json") for name in listed))
            self.assertFalse(any("secret.txt" in name for name in listed))


if __name__ == "__main__":
    unittest.main()
