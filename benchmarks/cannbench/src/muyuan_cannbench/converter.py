from __future__ import annotations

import json
import shutil
from pathlib import Path

from auto_pipeline.converter.base import BaseConverter
from auto_pipeline.converter.submission import validate_submission
from auto_pipeline.core import Artifact, CannBenchCase, Submission


class GoalPlusToCannConverter(BaseConverter):
    """Validate and copy a selected standard Goal Plus CANN submission."""

    name = "goal-plus-to-cann"
    source_generator = "goal-plus"
    target_benchmark = "cann"

    def __init__(self, *, timeout_sec: int = 7200) -> None:
        self.timeout_sec = timeout_sec
        self.env: dict[str, str] = {}

    def build_submission(
        self,
        bench_name: str,
        case: CannBenchCase,
        artifact: Artifact,
        *,
        output_dir: Path,
    ) -> Submission:
        if bench_name != self.target_benchmark:
            raise ValueError(f"{self.name} targets benchmark cann, got {bench_name}")
        source_dir = Path(artifact.files.get("submission", artifact.workdir)).expanduser().resolve()
        output_dir = Path(output_dir).expanduser().resolve()
        if source_dir == output_dir:
            raise ValueError("Goal Plus artifact and converter output directory must differ")
        validate_submission(self.target_benchmark, case, source_dir, label="goal-plus")

        output_dir.mkdir(parents=True, exist_ok=True)
        package_output = output_dir / "cann_bench"
        if package_output.exists():
            shutil.rmtree(package_output)
        shutil.copytree(source_dir / "cann_bench", package_output)
        for name in ("build.sh", "setup.py"):
            shutil.copy2(source_dir / name, output_dir / name)
        (output_dir / "build.sh").chmod(0o755)

        metadata = {
            "converter": self.name,
            "source_generator": self.source_generator,
            "target_benchmark": self.target_benchmark,
            "operator": case.operator,
            "goal_plus": dict(artifact.metadata),
        }
        (output_dir / "benchmark_submission.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        validate_submission(self.target_benchmark, case, output_dir, label="goal-plus copied")
        return Submission(self.target_benchmark, case.operator, output_dir, metadata)
