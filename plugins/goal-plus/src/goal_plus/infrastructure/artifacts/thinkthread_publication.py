"""Encode durable publication operations and decode ThinkThread observations."""

import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.ports.publication import (
    PublicationChange, PublicationApplyIntent, PublicationApplyObservation, PublicationRequest,
    PublicationSnapshotIntent, PublicationSnapshotResult, ThinkThreadPublicationPlan,
)
from goal_plus.thinkthread_agent_posix import AgentPosixBridgeError, AgentPosixSdkClient
from goal_plus.thinkthread_artifacts import ThinkThreadSnapshotReader
from goal_plus.infrastructure.artifacts.thinkthread_mutations import ThinkThreadMutations, provider_error


PUBLICATION_TIMEOUT_SECONDS = 120.0


def apply_contract(intent: PublicationApplyIntent) -> tuple[dict[str, Any], dict[str, Any]]:
    dependencies = [dict(item) for item in intent.dependencies]
    identity = {
        "publication_revision": intent.publication_revision,
        "base_snapshot_id": intent.base_artifact_ref.id,
        "target_snapshot_id": intent.target_artifact_ref.id,
        "dependencies": dependencies,
        "policy_id": "safe_content_v1",
    }
    params = {
        "baseSnapshotId": intent.base_artifact_ref.id,
        "targetSnapshotId": intent.target_artifact_ref.id,
        "dependencies": dependencies,
        "policyId": "safe_content_v1",
    }
    return identity, params


class ThinkThreadPublicationSnapshots:
    def __init__(
        self, client: Callable[[], AgentPosixSdkClient], mutations: ThinkThreadMutations,
    ) -> None:
        self.client, self.mutations = client, mutations

    def plan_thinkthread(
        self,
        *,
        base_artifact_ref: ArtifactRef,
        selected_artifact_ref: ArtifactRef,
        source_subdir: str,
        changed_files: list[str],
        dependencies: list[Mapping[str, str]],
    ) -> ThinkThreadPublicationPlan:
        client = self.client()
        self._require_snapshot(base_artifact_ref)
        self._require_snapshot(selected_artifact_ref)
        filtered = tuple(
            sorted({path for path in changed_files if _promotion_product_path(path)})
        )
        diff = self._snapshot_diff(client, base_artifact_ref.id, selected_artifact_ref.id)
        selected_paths = {
            _join_source_path(source_subdir, path) for path in filtered
        }
        selected_changes = {
            path: kind
            for path, kind in diff.items()
            if path in selected_paths
            or any(
                selected.startswith(f"{path.rstrip('/')}/")
                for selected in selected_paths
            )
        }
        changes: list[PublicationChange] = []
        removals = [
            path
            for path, kind in selected_changes.items()
            if kind in {"deleted", "type_changed"}
        ]
        for path in sorted(removals, key=lambda item: (-item.count("/"), item)):
            base_entry = self._snapshot_stat(client, base_artifact_ref.id, path)
            kind = base_entry.get("kind")
            if kind == "symlink":
                raise RuntimeError(
                    f"ThinkThread safe promotion rejects symlink path: {path}"
                )
            mutation_kind = (
                "remove_directory" if kind == "directory" else "remove_file"
            )
            changes.append(PublicationChange(mutation_kind, path))

        additions = [
            path for path, kind in selected_changes.items() if kind != "deleted"
        ]
        directories: list[tuple[str, int]] = []
        files: list[tuple[str, int, bytes]] = []
        for path in sorted(additions):
            entry = self._snapshot_stat(client, selected_artifact_ref.id, path)
            kind = entry.get("kind")
            mode = entry.get("mode")
            if not isinstance(mode, int) or not 0 <= mode <= 0o7777:
                raise RuntimeError(
                    f"ThinkThread promotion found invalid mode at {path}: {mode!r}"
                )
            if kind == "symlink" or selected_changes[path] == "symlink_target_changed":
                raise RuntimeError(
                    f"ThinkThread safe promotion rejects symlink path: {path}"
                )
            if kind == "directory":
                if selected_changes[path] == "mode_changed":
                    raise RuntimeError(
                        "ThinkThread safe promotion cannot change an existing "
                        f"directory mode: {path}"
                    )
                directories.append((path, mode))
                continue
            if kind != "file":
                raise RuntimeError(
                    f"ThinkThread promotion found unsupported entry at {path}: {kind}"
                )
            content = client.snapshot_read_file(
                selected_artifact_ref.id,
                path,
                max_bytes=256 * 1024 * 1024,
            )
            files.append((path, mode, content))

        for path, mode in sorted(
            directories,
            key=lambda item: (item[0].count("/"), item[0]),
        ):
            changes.append(PublicationChange("make_directory", path, mode))
        for path, mode, content in files:
            changes.append(PublicationChange("put_file", path, mode, content))
        normalized_dependencies = tuple(
            sorted(
                (
                    {
                        "path": _join_source_path(source_subdir, str(item["path"])),
                        "scope": str(item["scope"]),
                    }
                    for item in dependencies
                ),
                key=lambda item: (item["path"], item["scope"]),
            )
        )
        return ThinkThreadPublicationPlan(
            base_artifact_ref=base_artifact_ref,
            selected_artifact_ref=selected_artifact_ref,
            source_subdir=source_subdir,
            changed_files=filtered,
            changes=tuple(changes),
            dependencies=normalized_dependencies,
        )

    def write_manifest(
        self,
        manifest_path: Path,
        *,
        run_id: str,
        candidate_id: str,
        plan: ThinkThreadPublicationPlan,
        publication_request_id: str | None,
        state: str,
        publication_snapshot_id: str | None = None,
        apply_request_id: str | None = None,
    ) -> None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "candidate_id": candidate_id,
                    "provider": "thinkthread",
                    "strategy": "fs_apply",
                    "state": state,
                    "base_snapshot_id": plan.base_artifact_ref.id,
                    "selected_snapshot_id": plan.selected_artifact_ref.id,
                    "publication_snapshot_id": publication_snapshot_id,
                    "publication_request_id": publication_request_id,
                    "apply_request_id": apply_request_id,
                    "changed_files": list(plan.changed_files),
                    "dependencies": list(plan.dependencies),
                    "mutations": publication_change_manifest(plan.changes),
                },
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def canonical_digest(self, base: ArtifactRef, target: ArtifactRef) -> str:
        return ThinkThreadSnapshotReader(self.client()).canonical_digest(base, target)

    def _snapshot_contract(self, intent: PublicationSnapshotIntent) -> tuple[dict, dict]:
        plan = intent.plan
        identity = {
            "candidate_id": intent.candidate_id,
            "root_thinkthread_id": intent.root_thinkthread_id,
            "base_snapshot_id": plan.base_artifact_ref.id,
            "selected_snapshot_id": plan.selected_artifact_ref.id,
            "source_subdir": plan.source_subdir,
            "changed_files": list(plan.changed_files),
            "mutations": publication_change_manifest(plan.changes),
        }
        mutations = []
        for change in plan.changes:
            mutation = {"kind": change.kind, "path": change.path}
            if change.mode is not None:
                mutation["mode"] = change.mode
            if change.content is not None:
                mutation["dataBase64"] = base64.b64encode(change.content).decode("ascii")
            mutations.append(mutation)
        return identity, {"snapshotId": plan.base_artifact_ref.id, "mutations": mutations}

    def prepare_snapshot(self, intent: PublicationSnapshotIntent) -> PublicationRequest:
        identity, params = self._snapshot_contract(intent)
        mutation = self.mutations.prepare_provider_mutation(
            run_id=intent.run_id, operation="publication_snapshot", identity=identity,
            params=params,
        )
        return PublicationRequest(mutation.request_id, mutation.identity)

    def materialize_snapshot(self, intent: PublicationSnapshotIntent) -> PublicationSnapshotResult:
        identity, params = self._snapshot_contract(intent)
        result, request_id = self.mutations.invoke_provider_mutation(
            client=self.client(), run_id=intent.run_id,
            operation="publication_snapshot", identity=identity, params=params,
            method="workflow.fs.snapshot.patchBytes", status_method="fs.snapshot.patch",
            timeout_seconds=PUBLICATION_TIMEOUT_SECONDS,
        )
        snapshot_id = self.publication_snapshot_id(result)
        owner = result["snapshot"].get("ownerThinkthreadId")
        if not isinstance(owner, str) or not owner:
            raise RuntimeError("publication snapshot omitted its owner")
        return PublicationSnapshotResult(
            request_id, ArtifactRef(kind="fs_snapshot", provider="thinkthread", id=snapshot_id), owner,
        )

    @staticmethod
    def publication_snapshot_id(result: Mapping[str, object]) -> str:
        snapshot = result.get("snapshot")
        snapshot_id = snapshot.get("snapshotId") if isinstance(snapshot, dict) else None
        if not isinstance(snapshot_id, str) or not snapshot_id.startswith("fsnap-"):
            raise RuntimeError(
                f"ThinkThread snapshot patch returned no snapshot: {dict(result)}"
            )
        return snapshot_id

    @staticmethod
    def _snapshot_diff(
        client: AgentPosixSdkClient,
        base: str,
        target: str,
    ) -> dict[str, str]:
        changes: dict[str, str] = {}
        valid_kinds = {
            "added",
            "modified",
            "deleted",
            "type_changed",
            "mode_changed",
            "symlink_target_changed",
        }
        for item in client.snapshot_diff_all(base, target):
            raw_path = item.get("path")
            path = raw_path.get("utf8") if isinstance(raw_path, dict) else None
            kind = item.get("kind")
            if not isinstance(path, str) or not path:
                raise RuntimeError(
                    "ThinkThread promotion cannot address a non-UTF-8 path"
                )
            candidate = Path(path)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise RuntimeError("ThinkThread promotion path escapes the snapshot")
            if kind not in valid_kinds:
                raise RuntimeError(
                    f"ThinkThread promotion found invalid diff kind at {path}: {kind}"
                )
            if path in changes:
                raise RuntimeError(f"ThinkThread promotion found duplicate path: {path}")
            changes[path] = str(kind)
        return changes

    @staticmethod
    def _snapshot_stat(
        client: AgentPosixSdkClient,
        snapshot: str,
        path: str,
    ) -> dict[str, object]:
        return client.invoke(
            "fs.snapshot.stat",
            {"snapshotId": snapshot, "path": path},
        )

    @staticmethod
    def _require_snapshot(artifact_ref: ArtifactRef) -> None:
        if artifact_ref.kind != "fs_snapshot" or artifact_ref.provider != "thinkthread":
            raise ValueError("ThinkThread provider requires an fs_snapshot artifact")


class ThinkThreadApply:
    def __init__(
        self, client: Callable[[], AgentPosixSdkClient], mutations: ThinkThreadMutations,
    ) -> None:
        self.client, self.mutations = client, mutations

    def describe_apply(self, intent: PublicationApplyIntent) -> PublicationRequest:
        identity, _ = apply_contract(intent)
        return PublicationRequest(
            self.mutations.thinkthread_request_id("apply", intent.run_id, identity), identity,
        )

    def prepare_apply(self, intent: PublicationApplyIntent) -> PublicationRequest:
        identity, params = apply_contract(intent)
        mutation = self.mutations.prepare_provider_mutation(
            run_id=intent.run_id, operation="apply", identity=identity, params=params,
        )
        return PublicationRequest(mutation.request_id, mutation.identity)

    def apply(self, intent: PublicationApplyIntent) -> PublicationApplyObservation:
        identity, params = apply_contract(intent)
        request = self.describe_apply(intent)
        try:
            result, request_id = self.mutations.invoke_provider_mutation(
                client=self.client(), run_id=intent.run_id, method="fs.apply",
                operation="apply", identity=identity, params=params,
                timeout_seconds=PUBLICATION_TIMEOUT_SECONDS,
            )
            if result.get("status") != "applied":
                raise RuntimeError(f"ThinkThread fs.apply returned a mismatched success result: {result}")
            base, target = result.get("baseSnapshotId"), result.get("targetSnapshotId")
            if not isinstance(base, str) or not isinstance(target, str):
                raise RuntimeError("ThinkThread fs.apply omitted snapshot identity")
            return PublicationApplyObservation(
                "applied", request_id,
                ArtifactRef(kind="fs_snapshot", provider="thinkthread", id=base),
                ArtifactRef(kind="fs_snapshot", provider="thinkthread", id=target),
                details=result,
            )
        except Exception as exc:
            code = exc.code if isinstance(exc, AgentPosixBridgeError) else None
            state = {
                "FsApplyConflict": "conflict", "FsApplyPolicyRejected": "policy_rejected",
                "FsApplyFailed": "failed",
            }.get(code, "needs_recovery")
            return PublicationApplyObservation(state, request.request_id, error=provider_error(exc))


class ThinkThreadPublicationVerifier:
    def __init__(self, client: Callable[[], AgentPosixSdkClient], verifiers) -> None:
        self.client, self.verifiers = client, verifiers

    def verify_publication(self, run, frozen, candidate_id, artifact_ref, iteration):
        return self.verifiers.run_thinkthread_commands(
            client=self.client(), run=run, frozen=frozen, candidate_id=candidate_id,
            artifact_ref=artifact_ref,
            commands=frozen.spec.promotion_verifiers or frozen.spec.process_verifiers,
            phase="promotion", iteration=iteration,
        )


def publication_change_manifest(changes: tuple[PublicationChange, ...]) -> list[dict]:
    result = []
    for change in changes:
        item = {"kind": change.kind, "path": change.path}
        if change.mode is not None:
            item["mode"] = change.mode
        if change.content is not None:
            item.update(bytes=len(change.content), sha256=hashlib.sha256(change.content).hexdigest())
        result.append(item)
    return result


def _join_source_path(source_subdir: str, relative_path: str) -> str:
    if source_subdir == ".":
        return relative_path
    return f"{source_subdir.rstrip('/')}/{relative_path}"


def _promotion_product_path(relative_path: str) -> bool:
    candidate = Path(relative_path)
    parts = candidate.parts
    return bool(
        relative_path
        and not candidate.is_absolute()
        and ".." not in parts
        and candidate.name != "results.tsv"
        and not any(part in {".git", ".gp", ".search", ".tmp"} for part in parts)
    )
