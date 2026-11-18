import json
import os
import tempfile
import unittest
from pathlib import Path

from patch_report import cumulative_diff, export_patch_report


def _write(root: Path, rel: str, content: str):
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="\n")


def _report(root: Path, *, baseline, keeps, harness="harness"):
    history = [{"round": 0, "action": "baseline", "candidate": str(baseline)}]
    for index, entry in enumerate(keeps, start=1):
        history.append({
            "round": entry.get("round", index),
            "action": "keep",
            "candidate": str(entry["dir"]),
            "proposal": entry.get("proposal"),
            "metrics": entry.get("metrics"),
        })
    payload = {
        "mode": "component",
        "manifest": str(root / "manifest.json"),
        "adapter": "generic",
        "harness": str(root / harness),
        "task": "test task",
        "rounds": len(keeps),
        "baseline": {"accuracy": 50, "tokens": 1000, "time_s": 10},
        "baseline_holdout": None,
        "final": keeps[-1].get("metrics") if keeps else {},
        "history": history,
        "change_manifest": [],
        "write_back": False,
        "baseline_frozen": True,
        "accepted_candidate": str(keeps[-1]["dir"]) if keeps else None,
        "baseline_candidate": str(baseline),
        "trace_dir": str(root),
        "workspace_dir": str(root / "candidates"),
    }
    report_path = root / "ahe-component-report.json"
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    return report_path, payload


class CumulativeDiffTests(unittest.TestCase):
    def test_reports_only_changed_files_with_git_headers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "baseline"
            cand = root / "round-1-abc"
            for d in (base, cand):
                (d / "src").mkdir(parents=True)
                _write(d, "src/a.py", "x = 1\n")
                _write(d, "src/b.py", "y = 2\n")
            _write(cand, "src/a.py", "x = 2\n")
            _write(cand, "src/new.py", "n = 0\n")
            (cand / "src").joinpath("b.py").unlink()
            (base / "__pycache__").mkdir()
            _write(base, "__pycache__/junk.pyc", "binary-ish")
            diff, changed = cumulative_diff(base, cand)
            self.assertEqual(changed, ["src/a.py", "src/b.py", "src/new.py"])
            self.assertIn("diff --git a/src/a.py b/src/a.py", diff)
            self.assertIn("-x = 1", diff)
            self.assertIn("+x = 2", diff)
            self.assertIn("diff --git a/src/new.py b/src/new.py", diff)
            self.assertIn("new file mode 100644", diff)
            self.assertIn("diff --git a/src/b.py b/src/b.py", diff)
            self.assertIn("deleted file mode 100644", diff)
            self.assertIn("+++ /dev/null", diff)

    def test_identical_trees_produce_empty_diff(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "baseline"
            cand = root / "round-1-abc"
            for d in (base, cand):
                (d / "assets").mkdir(parents=True)
                _write(d, "assets/prompt.md", "same\n")
            diff, changed = cumulative_diff(base, cand)
            self.assertEqual(diff, "")
            self.assertEqual(changed, [])


class ExportPatchReportTests(unittest.TestCase):
    def test_builds_readme_and_per_round_patch_and_md(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "baseline"
            base.mkdir()
            _write(base, "runtime.py", "def run():\n    return 1\n")
            _write(base, "prompt.md", "base prompt\n")

            r1 = root / "round-1-3a2e2365"
            r1.mkdir()
            _write(r1, "runtime.py", "def run():\n    return 2\n")
            _write(r1, "prompt.md", "base prompt\n")
            _write(r1, "memory/ledger.md", "entry\n")

            r2 = root / "round-2-9f6c484d"
            r2.mkdir()
            _write(r2, "runtime.py", "def run():\n    return 2\n")
            _write(r2, "prompt.md", "better prompt\n")

            keeps = [
                {"dir": r1, "round": 1, "metrics": {"accuracy": 60, "tokens": 900},
                 "proposal": {"reason": "cache dispatch metadata", "patches": [
                     {"component_id": "runtime", "reason": "cache dispatch metadata"}]}},
                {"dir": r2, "round": 2, "metrics": {"accuracy": 61, "tokens": 800},
                 "proposal": {"reason": "compact context", "patches": [
                     {"component_id": "prompt", "reason": "compact context"}]}},
            ]
            report_path, _ = _report(root, baseline=base, keeps=keeps)
            target = root / "patch-report"

            result = export_patch_report(report_path, str(target))
            self.assertEqual(result, target)
            names = sorted(p.name for p in target.iterdir())
            self.assertIn("README.md", names)
            self.assertIn("01-round-1-3a2e2365.patch", names)
            self.assertIn("01-round-1-3a2e2365.md", names)
            self.assertIn("02-round-2-9f6c484d.patch", names)
            self.assertIn("02-round-2-9f6c484d.md", names)

            readme = (target / "README.md").read_text(encoding="utf-8")
            self.assertIn("每个 patch 都是从同一冻结基线到该轮候选快照的累计 diff", readme)
            self.assertIn("[Round 1](01-round-1-3a2e2365.md)", readme)
            self.assertIn("[Round 2](02-round-2-9f6c484d.md)", readme)
            self.assertIn("accuracy=50", readme)

            patch1 = (target / "01-round-1-3a2e2365.patch").read_text(encoding="utf-8")
            self.assertIn("diff --git a/runtime.py b/runtime.py", patch1)
            self.assertIn("+    return 2", patch1)
            self.assertIn("diff --git a/memory/ledger.md b/memory/ledger.md", patch1)

            md1 = (target / "01-round-1-3a2e2365.md").read_text(encoding="utf-8")
            self.assertIn("# Round 1", md1)
            self.assertIn("cache dispatch metadata", md1)
            self.assertIn("`runtime.py`", md1)
            self.assertIn("accuracy=60", md1)

            patch2 = (target / "02-round-2-9f6c484d.patch").read_text(encoding="utf-8")
            # Round 2 inherits round 1's runtime change, so its cumulative patch
            # contains both the kept runtime diff and this round's prompt diff.
            # The ledger added in round 1 and deleted by round 2 is absent from
            # both baseline and round 2, so the net diff correctly omits it.
            self.assertIn("diff --git a/prompt.md b/prompt.md", patch2)
            self.assertIn("+better prompt", patch2)
            self.assertIn("diff --git a/runtime.py b/runtime.py", patch2)
            self.assertNotIn("memory/ledger.md", patch2)

    def test_no_kept_rounds_writes_empty_readme(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "baseline"
            base.mkdir()
            _write(base, "prompt.md", "base\n")
            report_path, _ = _report(root, baseline=base, keeps=[])
            target = root / "patch-report"
            result = export_patch_report(report_path, str(target))
            self.assertEqual(result, target)
            readme = (target / "README.md").read_text(encoding="utf-8")
            self.assertIn("没有 keep 任何候选", readme)
            self.assertEqual([p.name for p in target.iterdir()], ["README.md"])

    def test_refuses_nonempty_target_and_missing_baseline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "baseline"
            base.mkdir()
            _write(base, "prompt.md", "base\n")
            report_path, _ = _report(root, baseline=base, keeps=[])
            occupied = root / "occupied"
            occupied.mkdir()
            _write(occupied, "keep.txt", "x")
            self.assertIsNone(export_patch_report(report_path, str(occupied)))
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            payload["baseline_candidate"] = str(root / "missing")
            report_path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIsNone(export_patch_report(report_path, str(root / "out2")))

    def test_rejects_non_component_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report_path = root / "report.json"
            report_path.write_text(json.dumps({"mode": "prompt"}), encoding="utf-8")
            self.assertIsNone(export_patch_report(report_path, str(root / "out3")))


if __name__ == "__main__":
    unittest.main()
