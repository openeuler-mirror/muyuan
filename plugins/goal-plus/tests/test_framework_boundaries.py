"""Phased architecture gates for migrated layers, including nested imports."""

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


PACKAGE = Path(__file__).resolve().parents[1] / "src" / "goal_plus"
HOST_CANDIDATE_MODULES = (
    "host_services.py", "host_scope.py", "host_recovery.py", "goal_plus_stop_hook.py",
    "session_host.py",
)
PURE_MODULES = {
    "__future__", "base64", "collections", "contextlib", "dataclasses", "datetime", "enum", "hashlib", "json",
    "math", "pathlib", "pydantic", "re", "typing",
}
IO_MEMBERS = {
    "open", "read_text", "read_bytes", "write_text", "write_bytes", "mkdir",
    "exists", "is_file", "is_dir", "is_symlink", "resolve", "absolute", "stat",
    "lstat", "iterdir", "glob", "rglob", "unlink", "rmdir", "rename",
    "touch", "chmod", "cwd", "home", "expanduser", "getenv", "environ",
}


def test_no_platform_process_inspection():
    violations = []
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text())
        # Bubblewrap mounts a private procfs; the argv target is not a host lookup.
        proc_mount_targets = {
            id(target)
            for sequence in ast.walk(tree) if isinstance(sequence, (ast.List, ast.Tuple))
            for option, target in zip(sequence.elts, sequence.elts[1:])
            if isinstance(option, ast.Constant) and option.value == "--proc"
            and isinstance(target, ast.Constant) and target.value == "/proc"
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and (
                node.attr in {"killpg", "getpgid", "getpgrp"}
                or node.attr == "kill" and isinstance(node.value, ast.Name) and node.value.id == "os"
            ):
                violations.append(f"{path.relative_to(PACKAGE)}:{node.lineno}")
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and (
                node.value == "/proc" and id(node) not in proc_mount_targets
                or node.value.startswith("/proc/") or "libproc" in node.value or node.value == "sysctl"
            ):
                violations.append(f"{path.relative_to(PACKAGE)}:{node.lineno}")
    assert not violations, "platform process inspection is not supported: " + ", ".join(violations)
    for name in ("process_identity", "process_identity_darwin", "process_resources", "scope_guardian"):
        assert not (PACKAGE / "execution" / f"{name}.py").exists()


@pytest.mark.parametrize("module", HOST_CANDIDATE_MODULES)
def test_candidate_hosts_do_not_pierce_search_state(module):
    tree = ast.parse((PACKAGE / module).read_text(encoding="utf-8"))
    forbidden = {
        "_load_run", "_load_frozen_spec", "_load_candidate_record", "_load_candidate_records",
        "_load_agent_session_by_id", "_load_agent_sessions", "_candidate_worker_budget",
        "_normalize_worker_budget_override", "_candidate_inner_agent_binding",
        "_candidate_artifact_state", "_best_artifact_iteration_record",
        "_outer_deadline_value", "_outer_deadline_epoch",
    }
    assert not [
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in forbidden
    ]
    assert not [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value in (
            "candidate.json", "frozen_spec.json", "run.json", "*/agent_sessions/*.json",
        )
    ]


def boundary_errors(source: str, module: str, layer: str) -> list[str]:
    tree = ast.parse(source)
    imports: dict[str, str] = {}
    dependencies: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports[alias.asname or alias.name.split(".")[0]] = alias.name
                dependencies.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            origin = node.module or ""
            if node.level:
                origin = importlib.util.resolve_name(
                    "." * node.level + origin, module.rpartition(".")[0]
                )
            for alias in node.names:
                imports[alias.asname or alias.name] = origin + "." + alias.name
                dependencies.append(origin + "." + alias.name)

    allowed = ("goal_plus.domain.",)
    if layer in {"ports", "application", "execution", "projections"}:
        allowed += ("goal_plus.ports.",)
    if layer == "application":
        allowed += ("goal_plus.application.",)
    if layer == "execution":
        allowed += ("goal_plus.execution.", "goal_plus.application.contracts.")
    if layer == "projections":
        allowed += ("goal_plus.projections.", "goal_plus._version.",
                    "goal_plus.application.candidate_ranking.",
                    "goal_plus.application.artifact_availability.")
    libraries = PURE_MODULES | (sys.stdlib_module_names if layer == "execution" else set())
    if layer == "projections":
        libraries |= {"html"}
    errors = [
        f"forbidden dependency: {name}"
        for name in dependencies
        if not name.startswith(allowed) and name.split(".")[0] not in libraries
        and name not in {"os.sep", "fnmatch.fnmatch"}
    ]
    if layer == "execution":
        return errors
    call_targets = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = imports.get(node.func.id, node.func.id)
            if name in {"open", "exec", "eval", "__import__", "json.load", "json.dump"}:
                errors.append(f"forbidden call: {name}")
        if isinstance(node, ast.Attribute):
            # VerifierCommand.cwd is data; Path.cwd (including aliases) is I/O.
            cwd_method = (
                node.attr == "cwd"
                and (
                    id(node) in call_targets
                    or (
                        isinstance(node.value, ast.Name)
                        and imports.get(node.value.id) in {"pathlib.Path", "pathlib", "os"}
                    )
                )
            )
            score_resolution = (
                module == "goal_plus.domain.selection" and node.attr == "resolve"
            )
            if (
                node.attr in IO_MEMBERS - {"cwd"}
                and not score_resolution
            ) or cwd_method:
                errors.append(f"I/O member: {node.attr}")
            if isinstance(node.value, ast.Name):
                owner = imports.get(node.value.id, node.value.id)
                if owner == "json" and node.attr in {"load", "dump"}:
                    errors.append(f"I/O serialization: {node.attr}")
                if owner in {"datetime.datetime", "datetime.date"} and node.attr in {"now", "utcnow", "today"}:
                    errors.append(f"implicit clock: {owner}.{node.attr}")
    return errors


@pytest.mark.parametrize("layer", ["domain", "ports", "application", "execution", "projections"])
def test_migrated_layers_have_only_inward_dependencies(layer: str) -> None:
    errors = []
    for path in sorted((PACKAGE / layer).rglob("*.py")):
        module = "goal_plus." + ".".join(path.relative_to(PACKAGE).with_suffix("").parts)
        errors.extend(
            f"{path.relative_to(PACKAGE)}: {error}"
            for error in boundary_errors(path.read_text(encoding="utf-8"), module, layer)
        )
    assert not errors, "\n".join(errors)


@pytest.mark.parametrize("source", [
    "def nested():\n    import goal_plus.runtime as runtime\n",
    "from goal_plus import runtime as store\n",
    "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from goal_plus.runtime import FileSearchRuntime\n",
    "from ..infrastructure.state.files import load_json as load\n",
    "import os as environment\nvalue = environment.environ\n",
    "from time import time as clock\nvalue = clock()\n",
    "from pathlib import Path as P\nvalue = P('state').read_text()\n",
    "from json import load as read\nvalue = read(handle)\n",
    "import json as codec\nvalue = codec.load(handle)\n",
    "value = __import__('goal_plus.runtime')\n",
    "from datetime import datetime as D\nvalue = D.now()\n",
    "from pathlib import Path as P\nvalue = P.cwd()\n",
    "from pathlib import Path as P\nreader = P.cwd\nvalue = reader()\n",
    "import pathlib\nvalue = pathlib.Path.cwd()\n",
])
def test_boundary_check_detects_nested_and_aliased_bypasses(source: str) -> None:
    assert boundary_errors(source, "goal_plus.domain.search", "domain")


def test_boundary_check_allows_declared_verifier_cwd_field() -> None:
    assert not boundary_errors("value = command.cwd\n", "goal_plus.application.context", "application")


@pytest.mark.parametrize("source", [
    "from goal_plus.bootstrap import build_annotation_services\n",
    "def nested():\n    import goal_plus.evidence_annotator as role\n",
    "from goal_plus.infrastructure.state.files import write_json\n",
    "from goal_plus.application.annotation import AnnotationWorkflow\n",
])
def test_execution_dependencies_cannot_construct_business_or_storage_owners(source):
    assert boundary_errors(source, "goal_plus.execution.process_monitor", "execution")


def test_schemas_import_without_initializing_runtime_or_executors() -> None:
    script = """
import importlib
import sys
from pydantic import BaseModel
modules = [
    'goal_plus.models', 'goal_plus.domain.search', 'goal_plus.domain.evidence',
    'goal_plus.domain.goal', 'goal_plus.domain.annotation',
    'goal_plus.domain.adaptive.models', 'goal_plus.domain.adaptive.policy',
    'goal_plus.ports.state', 'goal_plus.ports.execution',
    'goal_plus.ports.annotation', 'goal_plus.application.annotation',
    'goal_plus.application.annotation_admission', 'goal_plus.application.search_context',
    'goal_plus.application.annotation_context', 'goal_plus.application.artifact_availability',
    'goal_plus.ports.annotation_context', 'goal_plus.ports.artifacts',
    'goal_plus.ports.verifier', 'goal_plus.ports.adaptive',
    'goal_plus.ports.clock', 'goal_plus.application.verifier',
    'goal_plus.application.adaptive_snapshot',
    'goal_plus.application.allocation', 'goal_plus.ports.allocation',
    'goal_plus.application.candidate_execution',
    'goal_plus.domain.goal_authorization', 'goal_plus.ports.goal_authorization',
    'goal_plus.application.goal_authorization',
    'goal_plus.domain.results_ledger',
    'goal_plus.application.allocation_apply', 'goal_plus.ports.allocation_apply',
    'goal_plus.ports.candidate_creation',
    'goal_plus.application.internal_agents', 'goal_plus.ports.internal_agents',
    'goal_plus.ports.agent_hosts', 'goal_plus.ports.session',
]
for name in modules:
    for value in vars(importlib.import_module(name)).values():
        if isinstance(value, type) and issubclass(value, BaseModel) and value is not BaseModel:
            assert value.__pydantic_complete__, value
            value.model_json_schema()
for name in sys.modules:
    assert not name.startswith((
        'goal_plus.runtime', 'goal_plus.adaptive', 'goal_plus.evidence_annotator',
        'goal_plus.agent_hosts', 'goal_plus.infrastructure', 'goal_plus.thinkthread',
    )), name
from goal_plus import FileSearchRuntime
assert FileSearchRuntime.__module__ == 'goal_plus.runtime'
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30,
        env={**os.environ, "PYTHONPATH": str(PACKAGE.parent)},
    )
    assert result.returncode == 0, result.stderr


def test_host_bindings_resolve_without_loading_execution_adapters() -> None:
    script = """
import sys
from goal_plus.infrastructure.host_bindings import execution_binding, resolve_inner_agent_binding
for host, provider in (('codex', 'direct'), ('pi', 'direct'), ('pi', 'thinkthread')):
    binding = execution_binding(host, provider)
    inner = resolve_inner_agent_binding(host, 'common', provider)
    assert inner.agent_harness == host
    assert inner.host_adapter_version == binding.adapter_version
    assert inner.continuation_mode == "native_session"
    assert inner.workspace_ledger_projection == 'none'
for name in sys.modules:
    assert not name.startswith((
        'goal_plus.agent_hosts', 'goal_plus.execution', 'goal_plus.runtime',
        'goal_plus.pi_pool', 'goal_plus.pi_worker', 'goal_plus.host_observability',
        'goal_plus.thinkthread_agent_posix',
    )), name
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30,
        env={**os.environ, "PYTHONPATH": str(PACKAGE.parent)},
    )
    assert result.returncode == 0, result.stderr


def test_production_callers_use_canonical_helpers_and_models() -> None:
    moved_helpers = {
        "utc_timestamp", "utc_timestamp_from_epoch", "parse_utc_timestamp",
        "load_json", "write_json", "write_text", "exclusive_file_lock",
        "sha256_file", "sha256_text", "canonical_json",
    }
    retired_annotation_members = {
        "_load_evidence_annotation_task", "_write_evidence_annotation_task",
        "_load_evidence_annotation_queue", "_write_evidence_annotation_queue",
        "_evidence_annotation_tasks", "_close_evidence_annotation_queue",
        "_eligible_evidence_annotations", "_pending_evidence_annotations",
        "_ensure_evidence_annotation_tasks", "_reconcile_evidence_annotation_admission",
        "_create_evidence_annotation_task", "_create_evidence_annotation_task_locked",
        "_evidence_annotation_context", "_open_evidence_artifact_views",
        "_evidence_comparison_peers",
        "_claim_schedule_task", "_finish_schedule_task", "_ScheduleTaskStore",
        "_validate_task_output", "kick_schedule_tasks",
        "_tool_ownership_error", "_tool_input_run_id", "_search_run_owner",
        "_record_for_host_invocation", "_nonterminal_records", "_require_control_session",
        "_read_results_tsv", "_candidate_results_ledger", "_source_ledger_at_revision",
        "_assert_run_not_invalidated",
        "_materialize_candidate_record", "_AllocationRuntimePort",
        "_kick_evidence_annotator", "_drain_evidence_annotations",
        "on_evidence_annotation_terminal", "_close_run_annotations",
        "_message_payload", "_response_payload", "_response_hash", "_interrupt_child", "_turn_terminal",
    }
    for path in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute):
                assert node.attr not in retired_annotation_members, path
            elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                assert node.name not in retired_annotation_members, path
            if isinstance(node, ast.ImportFrom):
                assert node.module != "goal_plus.models", path
                assert node.module != "goal_plus.agent_pool", path
                if node.module == "goal_plus.agent_hosts":
                    assert not {
                        "AgentExecutionBinding", "AgentExecutionCapabilities", "HostModelCatalog",
                        "CandidateLaunchAdapter", "UnsupportedHostCapability",
                        "host_binding", "resolve_inner_agent_binding",
                        "candidate_inner_agent_binding", "portable_strategy_mode",
                    }.intersection(a.name for a in node.names), path
                if path.name == "thinkthread_annotation.py":
                    assert node.module != "goal_plus.thinkthread_pool", path
                if node.module == "goal_plus.runtime":
                    assert not moved_helpers.intersection(a.name for a in node.names), path


def test_verifier_execution_does_not_import_adaptive_implementations() -> None:
    tree = ast.parse(
        (PACKAGE / "llm_verifier.py").read_text(encoding="utf-8")
    )
    adaptive_implementations = {
        "goal_plus.adaptive",
        "goal_plus.application.adaptive_snapshot",
        "goal_plus.application.allocation",
        "goal_plus.domain.adaptive",
        "goal_plus.infrastructure.state.allocation",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not any(
                node.module == dependency
                or node.module.startswith(dependency + ".")
                for dependency in adaptive_implementations
            )
        if isinstance(node, ast.Import):
            assert not any(
                alias.name == dependency
                or alias.name.startswith(dependency + ".")
                for dependency in adaptive_implementations
                for alias in node.names
            )


def test_candidate_settlement_does_not_drive_async_evaluators() -> None:
    tree = ast.parse(
        (PACKAGE / "application/candidate_settlement.py").read_text(
            encoding="utf-8"
        )
    )
    forbidden_modules = {
        "goal_plus.application.annotation",
        "goal_plus.application.annotation_admission",
        "goal_plus.application.verifier",
    }
    assert not [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module in forbidden_modules
    ]
    assert not [
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr in {"ensure_assessment", "reconcile_tool_heads"}
    ]


def test_common_models_do_not_reexport_adaptive_search_contracts() -> None:
    tree = ast.parse((PACKAGE / "models.py").read_text(encoding="utf-8"))
    assert not [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("goal_plus.domain.adaptive")
    ]


def test_verifier_record_identifies_scores_by_key() -> None:
    from goal_plus.domain.search import FrozenSpec
    from goal_plus.domain.verifier import VerifierRecord, VerifierTask

    assert "verifier_score_contract" not in FrozenSpec.model_fields
    assert "score_contract" in VerifierTask.model_fields
    assert "policy_id" not in VerifierRecord.model_fields
    assert "score_versions" not in VerifierRecord.model_fields
    assert "scores" in VerifierRecord.model_fields
    assert "score_contract_hash" in VerifierRecord.model_fields


def test_policy_module_does_not_own_verifier_score_semantics() -> None:
    violations: list[str] = []
    path = PACKAGE / "domain/adaptive/policy.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "goal_plus.domain.verifier":
            violations.append(path.relative_to(PACKAGE).as_posix())
        if isinstance(node, ast.FunctionDef) and "verifier" in node.name:
            violations.append(path.relative_to(PACKAGE).as_posix())

    assert violations == []


def test_search_execution_authorization_does_not_construct_goal_runtime() -> None:
    tree = ast.parse((PACKAGE / "runtime.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "goal_plus.goal_plus"
        if isinstance(node, ast.Name):
            assert node.id != "FileGoalPlusRuntime"


def test_allocation_facade_does_not_rebuild_preflight_from_private_runtime_queries() -> None:
    tree = ast.parse(
        (PACKAGE / "adaptive/allocation.py").read_text(encoding="utf-8")
    )
    retired = {
        "_run_transaction", "_load_run", "_load_frozen_spec", "_load_plan",
        "_load_candidate_records", "_load_agent_sessions", "_assert_worker_iteration_allowed",
        "_load_candidate_record", "_write_candidate_record", "_write_run",
    }
    assert not [node.attr for node in ast.walk(tree)
                if isinstance(node, ast.Attribute) and node.attr in retired]


def test_runtime_cannot_import_internal_agent_execution() -> None:
    dependencies = {
        "goal_plus.evidence_annotator", "goal_plus.thinkthread_annotation",
        "goal_plus.internal_agents", "goal_plus.llm_verifier",
    }
    tree = ast.parse((PACKAGE / "runtime.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module not in dependencies
        if isinstance(node, ast.Import):
            assert not dependencies.intersection(alias.name for alias in node.names)


@pytest.mark.parametrize("module", [
    "internal_agents.py", "evidence_annotator.py", "llm_verifier.py",
    "annotation_host.py", "thinkthread_annotation.py",
])
def test_internal_agent_host_paths_do_not_construct_search_runtime(module) -> None:
    tree = ast.parse((PACKAGE / module).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "goal_plus.runtime"
            assert node.module != "goal_plus.host_services"
        if isinstance(node, ast.Name):
            assert node.id != "FileSearchRuntime"
