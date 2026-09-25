from __future__ import annotations

import json
from pathlib import Path
import unittest


FIXTURE = Path(__file__).parent / "fixtures" / "ultra_smoke_codex"


class UltraSmokeFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.evidence = json.loads(
            (FIXTURE / "evidence.json").read_text(encoding="utf-8")
        )


    def test_committed_fixture_excludes_raw_runtime_and_transcript(self) -> None:
        provenance = self.evidence["provenance"]
        self.assertFalse(provenance["raw_runtime_state_committed"])
        self.assertFalse(provenance["raw_transcript_committed"])
        self.assertFalse((FIXTURE / ".gp").exists())
        self.assertEqual(list(FIXTURE.glob("*.jsonl")), [])


if __name__ == "__main__":
    unittest.main()
