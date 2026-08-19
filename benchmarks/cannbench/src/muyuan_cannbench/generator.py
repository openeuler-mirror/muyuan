from __future__ import annotations

import json
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from auto_pipeline.core import AGENT_FAILED, AGENT_SUCCESS, Artifact, GeneratorInput
from auto_pipeline.generator.akg.converter.to_cann import load_operator_schema
from goal_plus.pi_pool import (
    close_pi_search_pool,
    open_pi_search_pool,
    wait_any_pi_search_pool,
)
from goal_plus.runtime import FileSearchRuntime
from goal_plus.tools import SearchTools

from .paths import CANN_BENCH_ROOT, GOAL_PLUS_ROOT, RESOURCES_DIR, resolve_path


@dataclass(frozen=True)
class GoalPlusGeneratorConfig:
    driver: str = "pi-rpc"
    verifier: str = "cann"
    max_parallel: int = 1
    seed_submission: str | None = None
    pi_binary: str = "pi"
    model_pattern: str | None = None
    device_id: int = 0
    verify_timeout: int = 7200
    no_perf: bool = False
    extra_eval_args: tuple[str, ...] = ()
    root_hypotheses: tuple[str, ...] = ()
    worker_budget: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def max_candidates(self) -> int:
        """Backward-compatible name used by older benchmark configs."""

        return self.max_parallel


def _read_config(registry_config: Mapping[str, Any]) -> tuple[GoalPlusGeneratorConfig, Path | None]:
    config_path_value = registry_config.get("config_path")
    config_path = resolve_path(str(config_path_value)) if config_path_value else None
    payload: dict[str, Any] = {}
    if config_path is not None:
        if not config_path.is_file():
            raise FileNotFoundError(f"Goal Plus generator config not found: {config_path}")
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"Goal Plus generator config must be a mapping: {config_path}")
        nested = loaded.get("goal_plus", loaded)
        if not isinstance(nested, dict):
            raise ValueError(f"goal_plus config must be a mapping: {config_path}")
        payload.update(nested)

    for key in ("device_id", "verify_timeout"):
        if registry_config.get(key) is not None:
            payload[key] = registry_config[key]
    codegen_target = str(registry_config.get("codegen_target") or "triton_ascend")
    if codegen_target != "triton_ascend":
        raise ValueError(
            "goal-plus generator currently targets codegen_target=triton_ascend, "
            f"got {codegen_target}"
        )

    configured_parallel = payload.get("max_parallel")
    legacy_candidates = payload.get("max_candidates")
    if (
        configured_parallel is not None
        and legacy_candidates is not None
        and int(configured_parallel) != int(legacy_candidates)
    ):
        raise ValueError(
            "goal_plus.max_parallel and legacy max_candidates must match when both are set"
        )
    max_parallel = int(
        configured_parallel
        if configured_parallel is not None
        else legacy_candidates
        if legacy_candidates is not None
        else 1
    )
    if max_parallel < 1:
        raise ValueError("goal_plus.max_parallel must be positive")
    driver = str(payload.get("driver") or "pi-rpc")
    verifier = str(payload.get("verifier") or "cann")
    if driver not in {"pi-rpc", "scripted"}:
        raise ValueError(f"unsupported Goal Plus driver: {driver}")
    if verifier not in {"cann", "static"}:
        raise ValueError(f"unsupported Goal Plus verifier: {verifier}")
    if driver == "pi-rpc" and verifier != "cann":
        raise ValueError("pi-rpc production search requires verifier=cann")
    if driver == "scripted" and verifier != "static":
        raise ValueError("scripted smoke search requires verifier=static")
    pi_binary = str(payload.get("pi_binary") or "pi")
    if driver == "pi-rpc" and pi_binary != "pi":
        raise ValueError(
            "Goal Plus Pi pool resolves the 'pi' executable from PATH; "
            "custom goal_plus.pi_binary values are not supported"
        )

    config = GoalPlusGeneratorConfig(
        driver=driver,
        verifier=verifier,
        max_parallel=max_parallel,
        seed_submission=payload.get("seed_submission"),
        pi_binary=pi_binary,
        model_pattern=payload.get("model_pattern"),
        device_id=int(payload.get("device_id", 0)),
        verify_timeout=int(payload.get("verify_timeout", 7200)),
        no_perf=bool(payload.get("no_perf", False)),
        extra_eval_args=tuple(str(item) for item in payload.get("extra_eval_args", [])),
        root_hypotheses=tuple(str(item) for item in payload.get("root_hypotheses", [])),
        worker_budget=payload.get("worker_budget"),
        raw=dict(payload),
    )
    return config, config_path


class GoalPlusGenerator:
    """Run one Goal Plus search lifecycle for one CANN Bench operator."""

    type = "goal-plus"

    def __init__(self, registry_config: Mapping[str, Any]) -> None:
        self.config, self.config_path = _read_config(registry_config)
        self.registry_config = dict(registry_config)

    def generate(self, generator_input: GeneratorInput) -> Artifact:
        try:
            return self._generate(generator_input)
        except Exception as exc:
            failure_dir = Path(generator_input.workdir).expanduser().resolve()
            failure_dir.mkdir(parents=True, exist_ok=True)
            log_file = failure_dir / "goal_plus_generator_error.log"
            log_file.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
            return Artifact(
                status=AGENT_FAILED,
                workdir=failure_dir,
                message=f"Goal Plus search failed: {type(exc).__name__}: {exc}",
                log_file=log_file,
            )

    def _generate(self, generator_input: GeneratorInput) -> Artifact:
        if generator_input.case is None:
            raise ValueError("GoalPlusGenerator requires a CANN Bench case")
        case = generator_input.case
        workdir = Path(generator_input.workdir).expanduser().resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        attempt_root = workdir / "goal-plus-attempts" / uuid.uuid4().hex[:12]
        source_dir = attempt_root / "source"
        self._prepare_source(generator_input, source_dir)

        state_root = attempt_root / "state"
        tools = SearchTools(FileSearchRuntime(state_root))
        spec, verifier_artifacts = self._build_search_spec(generator_input, source_dir)
        frozen = tools.search_freeze_spec(spec, [str(path) for path in verifier_artifacts])
        run_id = tools.search_create(frozen["frozen_spec_id"])["run_id"]
        plan = tools.search_plan_next(run_id, requested_k=self.config.max_parallel)
        tasks = tools.search_start_batch(run_id, plan["plan_id"])
        if not tasks:
            raise RuntimeError("Goal Plus did not create any candidate")

        pool_summary: dict[str, Any] | None = None
        if self.config.driver == "scripted":
            self._drive_scripted(tools, run_id, tasks, function_name=load_operator_schema(case).function_name)
        else:
            pool_summary = self._drive_pi(run_id, tasks, state_root)

        selection = tools.search_select(run_id)
        report = tools.search_report(run_id)
        selected_id = str(selection["selected_candidate_id"])
        selected = next((task for task in tasks if task["candidate_id"] == selected_id), None)
        if selected is None:
            raise RuntimeError(f"Goal Plus selected unknown candidate: {selected_id}")
        selected_workspace = Path(selected["workspace"]).expanduser().resolve()
        metadata = {
            "generator": self.type,
            "driver": self.config.driver,
            "verifier": self.config.verifier,
            "codegen_target": "triton_ascend",
            "run_id": run_id,
            "selected_candidate_id": selected_id,
            "selected_score": selection.get("selected_score"),
            "goal_plus_report": report,
            "candidate_count": len(tasks),
            "max_parallel": self.config.max_parallel,
            "pi_pool": pool_summary,
            "npu_executed": self.config.verifier == "cann",
            "attempt_root": str(attempt_root),
        }
        metadata_path = attempt_root / "goal_plus_selection.json"
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return Artifact(
            status=AGENT_SUCCESS,
            workdir=selected_workspace,
            message=f"Goal Plus selected {selected_id}",
            files={
                "submission": selected_workspace,
                "build_script": selected_workspace / "build.sh",
                "setup_py": selected_workspace / "setup.py",
                "package": selected_workspace / "cann_bench",
                "goal_plus_report": Path(report["report_path"]),
                "goal_plus_selection": metadata_path,
            },
            metadata=metadata,
        )

    def _prepare_source(self, generator_input: GeneratorInput, source_dir: Path) -> None:
        case = generator_input.case
        assert case is not None
        seed_value = self.config.seed_submission
        if seed_value:
            seed = resolve_path(seed_value, config_path=self.config_path)
            if not seed.is_dir():
                raise FileNotFoundError(f"seed_submission not found: {seed}")
            shutil.copytree(seed, source_dir, ignore=shutil.ignore_patterns("dist", "*.egg-info", "__pycache__"))
        else:
            example = CANN_BENCH_ROOT / "examples" / "triton_ascend_cann_example"
            source_dir.mkdir(parents=True)
            shutil.copy2(example / "build.sh", source_dir / "build.sh")
            shutil.copy2(example / "setup.py", source_dir / "setup.py")
            package_dir = source_dir / "cann_bench"
            package_dir.mkdir()
            function_name = load_operator_schema(case).function_name
            package_dir.joinpath("__init__.py").write_text(
                f"from .{function_name} import {function_name}\n\n__all__ = [{function_name!r}]\n",
                encoding="utf-8",
            )
            package_dir.joinpath(f"{function_name}.py").write_text(
                "from __future__ import annotations\n\n"
                f"def {function_name}(*args, **kwargs):\n"
                "    raise NotImplementedError('Goal Plus candidate must implement this Triton-Ascend operator')\n",
                encoding="utf-8",
            )
        (source_dir / "build.sh").chmod(0o755)

        context_dir = source_dir / "_goal_plus"
        task_dir = context_dir / "task"
        task_dir.mkdir(parents=True, exist_ok=True)
        for case_file in generator_input.material.task_files:
            if case_file.source_path.is_file():
                shutil.copy2(case_file.source_path, task_dir / case_file.target_name)
        context_dir.joinpath("TASK.md").write_text(
            "\n".join(
                [
                    f"# CANN Bench operator: {case.operator}",
                    "",
                    "Implement and optimize the standard cann_bench package with Triton-Ascend.",
                    "Only cann_bench/ is editable. build.sh, setup.py, task inputs, and verifier are frozen.",
                    "",
                    generator_input.material.require_text,
                ]
            ),
            encoding="utf-8",
        )
        context_dir.joinpath("input_metadata.json").write_text(
            json.dumps(generator_input.metadata, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        if self.config.verifier == "static":
            shutil.copy2(RESOURCES_DIR / "static_verifier.py", context_dir / "verifier.py")
        else:
            verifier_source = (
                GOAL_PLUS_ROOT
                / "examples"
                / "cannbench-tilelang-ascend"
                / "verifier"
                / "cannbench_eval.py"
            )
            shutil.copy2(verifier_source, context_dir / "verifier.py")

    def _build_search_spec(
        self,
        generator_input: GeneratorInput,
        source_dir: Path,
    ) -> tuple[dict[str, Any], list[Path]]:
        case = generator_input.case
        assert case is not None
        function_name = load_operator_schema(case).function_name
        verifier_path = source_dir / "_goal_plus" / "verifier.py"
        if self.config.verifier == "static":
            metric_name = "static_score"
            command = [sys.executable, "_goal_plus/verifier.py", "--function", function_name]
            resource_lock = None
        else:
            metric_name = "overall_score"
            task_selector = str(case.metadata.get("task_selector") or case.rel_path)
            command = [
                sys.executable,
                "_goal_plus/verifier.py",
                "--cann-bench-root",
                str(CANN_BENCH_ROOT),
                "--task-dir",
                task_selector,
                "--operator",
                case.operator,
                "--device-id",
                str(self.config.device_id),
            ]
            if self.config.no_perf:
                command.append("--no-perf")
            for item in self.config.extra_eval_args:
                command.append(f"--extra-arg={item}")
            resource_lock = f"npu:{self.config.device_id}"
        hypotheses = list(self.config.root_hypotheses) or [
            "produce a correct baseline Triton-Ascend implementation",
            "improve launch geometry and memory access while preserving correctness",
        ]
        strategy: dict[str, Any] = {
            "name": "agent_guided" if self.config.driver == "pi-rpc" else "random",
            "orchestration_mode": "parallel_loops",
            "worker_host": "pi-rpc" if self.config.driver == "pi-rpc" else "codex",
        }
        if self.config.worker_budget:
            strategy["worker_budget"] = self.config.worker_budget
        if self.config.model_pattern:
            strategy["worker_launch"] = {"model": self.config.model_pattern}
        spec = {
            "objective": f"optimize CANN Bench {case.operator} with Triton-Ascend",
            "metric_name": metric_name,
            "metric_direction": "maximize",
            "source_path": str(source_dir),
            "edit_surface": {
                "allow": ["cann_bench/"],
                "deny": ["_goal_plus/", "build.sh", "setup.py"],
            },
            "process_verifiers": [
                {
                    "name": "cannbench_npu" if self.config.verifier == "cann" else "triton_ascend_static",
                    "role": "ranking_signal",
                    "command": command,
                    "timeout_seconds": min(self.config.verify_timeout, generator_input.timeout_sec),
                    **({"resource_lock": resource_lock} if resource_lock else {}),
                }
            ],
            "promotion_verifiers": [
                {
                    "name": "frozen_hash_gate",
                    "role": "anti_cheat_gate",
                    "command": ["goal-plus-internal", "check-frozen-hashes"],
                }
            ],
            "budget": {"max_parallel": self.config.max_parallel},
            "root_hypotheses": hypotheses,
            "strategy": strategy,
        }
        artifacts = [verifier_path]
        artifacts.extend(path for path in (source_dir / "_goal_plus" / "task").iterdir() if path.is_file())
        return spec, artifacts

    def _drive_scripted(
        self,
        tools: SearchTools,
        run_id: str,
        tasks: list[dict[str, Any]],
        *,
        function_name: str,
    ) -> None:
        for index, task in enumerate(tasks, start=1):
            candidate_id = str(task["candidate_id"])
            session = tools.search_start_agent_session(
                run_id,
                candidate_id,
                {"goal": f"non-NPU control-plane candidate {index}"},
            )
            workspace = Path(task["workspace"])
            if index > 1:
                target = workspace / "cann_bench" / f"{function_name}.py"
                if not target.is_file():
                    target = next((workspace / "cann_bench").glob("*.py"))
                with target.open("a", encoding="utf-8") as stream:
                    stream.write(
                        f"\n# scripted control-plane candidate {index}\n"
                        f"GOAL_PLUS_STATIC_SCORE = {float(index):.1f}\n"
                    )
            tools.search_run_verifier(
                run_id,
                candidate_id,
                agent_session_id=session["agent_session_id"],
                hypothesis=f"scripted static candidate {index}",
            )
            tools.search_run_verifier(run_id, candidate_id)

    def _drive_pi(
        self,
        run_id: str,
        tasks: list[dict[str, Any]],
        state_root: Path,
    ) -> dict[str, Any]:
        candidate_ids = [str(task["candidate_id"]) for task in tasks]
        expected = set(candidate_ids)
        worker_budgets = (
            {
                candidate_id: dict(self.config.worker_budget)
                for candidate_id in candidate_ids
            }
            if self.config.worker_budget
            else None
        )
        opened = open_pi_search_pool(
            root_dir=state_root,
            run_id=run_id,
            candidate_ids=candidate_ids,
            worker_budgets=worker_budgets,
            final_verify=True,
            max_parallel=self.config.max_parallel,
        )
        pool_id = str(opened["pool_id"])
        events: dict[str, dict[str, Any]] = {}

        try:
            while len(events) < len(expected):
                waited = wait_any_pi_search_pool(
                    root_dir=state_root,
                    pool_id=pool_id,
                    timeout_seconds=30,
                )
                for raw_event in waited.get("events", []):
                    event = dict(raw_event)
                    candidate_id = str(event.get("candidate_id") or "")
                    if candidate_id not in expected:
                        raise RuntimeError(
                            f"Pi pool {pool_id} returned unknown candidate: {candidate_id!r}"
                        )
                    if candidate_id in events:
                        raise RuntimeError(
                            f"Pi pool {pool_id} returned duplicate terminal event for {candidate_id}"
                        )
                    events[candidate_id] = event
                if len(events) < len(expected) and int(waited.get("active_count", 0)) == 0:
                    missing = sorted(expected - set(events))
                    raise RuntimeError(
                        f"Pi pool {pool_id} stopped before candidates completed: {missing}"
                    )

            closed = close_pi_search_pool(
                root_dir=state_root,
                pool_id=pool_id,
                mode="drain",
                timeout_seconds=30,
            )
        except Exception:
            try:
                close_pi_search_pool(
                    root_dir=state_root,
                    pool_id=pool_id,
                    mode="interrupt",
                    timeout_seconds=30,
                )
            except Exception:
                pass
            raise

        if closed.get("state") != "closed" or int(closed.get("active_count", 0)) != 0:
            raise RuntimeError(f"Pi pool {pool_id} did not close cleanly: {closed}")

        failures = {
            candidate_id: event
            for candidate_id, event in events.items()
            if event.get("kind") != "candidate_ready"
        }
        if failures:
            details = "; ".join(
                f"{candidate_id}={event.get('kind')}: "
                f"{event.get('error') or event.get('message') or 'no error details'}"
                for candidate_id, event in sorted(failures.items())
            )
            raise RuntimeError(f"Pi pool {pool_id} candidate failure: {details}")

        return {
            "pool_id": pool_id,
            "state": str(closed["state"]),
            "max_parallel": self.config.max_parallel,
            "candidate_ready": sorted(events),
        }


def create_goal_plus_generator(registry_config: Mapping[str, Any]) -> GoalPlusGenerator:
    return GoalPlusGenerator(registry_config)
