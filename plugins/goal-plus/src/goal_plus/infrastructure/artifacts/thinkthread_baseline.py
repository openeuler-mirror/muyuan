"""Prepare an exact runtime-owned ThinkThread baseline and its verifier input."""

from pathlib import Path, PurePosixPath
from typing import Callable
from goal_plus.domain.search import SearchSpec
from goal_plus.infrastructure.verifiers.local import LocalVerifiers
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.evidence import ScoreReport
from goal_plus.domain.search import FrozenSpec
from goal_plus.ports.state import RunRecord
from goal_plus.thinkthread_agent_posix import (
    AgentPosixSdkClient,
    validate_thinkthread_root,
)

from goal_plus.infrastructure.artifacts.thinkthread_mutations import (
    ThinkThreadMutations,
)
from goal_plus.infrastructure.verifiers.thinkthread import ThinkThreadVerifiers


class ThinkThreadBaseline:
    def __init__(
        self,
        root_dir: Path | str,
        client: AgentPosixSdkClient,
        source_relative_path: str,
    ) -> None:
        if Path(root_dir).resolve().is_relative_to(Path.cwd().resolve()):
            raise ValueError(
                "ThinkThread runtime root must be outside the attached Root tree; "
                "use the managed Profile GOAL_PLUS_ROOT session directory"
            )
        root = validate_thinkthread_root(client)
        self.provider_handle = {
            "provider": "thinkthread",
            "root_thinkthread_id": root.get("thinkthreadId"),
            "source_relative_path": source_relative_path,
        }
        self.client = client
        self.provider_mutations = ThinkThreadMutations(root_dir)
        self.verifiers = ThinkThreadVerifiers(
            root_dir, self.provider_mutations, LocalVerifiers(root_dir)
        )

    def verify(
        self, run: RunRecord, frozen: FrozenSpec, artifact: ArtifactRef
    ) -> ScoreReport:
        return self.verifiers.run_thinkthread_commands(
            client=self.client,
            run=run,
            frozen=frozen,
            candidate_id=None,
            artifact_ref=artifact,
            commands=frozen.spec.process_verifiers,
            phase="origin",
            iteration=0,
        )

    def capture(
        self,
        run: RunRecord,
    ) -> ArtifactRef:
        result, _request_id = self.provider_mutations.invoke_provider_mutation(
            client=self.client,
            run_id=run.run_id,
            method="fs.snapshot.create",
            operation="baseline_snapshot",
            identity={
                "root_thinkthread_id": run.source_provider_handle.get(
                    "root_thinkthread_id"
                ),
                "source_relative_path": run.source_provider_handle.get(
                    "source_relative_path"
                ),
            },
            params={},
            timeout_seconds=120.0,
        )
        snapshot_id = result.get("snapshotId")
        if not isinstance(snapshot_id, str) or not snapshot_id.startswith("fsnap-"):
            raise RuntimeError("fs.snapshot.create omitted an exact snapshot identity")
        if result.get("ownerThinkthreadId") != run.source_provider_handle.get(
            "root_thinkthread_id"
        ):
            raise RuntimeError("fs.snapshot.create returned the wrong snapshot owner")
        return ArtifactRef(
            kind="fs_snapshot",
            id=snapshot_id,
            provider="thinkthread",
        )


def thinkthread_source_relative_path(source: Path) -> str:
    source = source.resolve()
    root = Path.cwd().resolve()
    try:
        relative = source.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "ThinkThread source_path must be the attached Root cwd or one of "
            "its descendants"
        ) from exc
    value = relative.as_posix() or "."
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("ThinkThread source_path escapes the attached Root")
    return value


class ThinkThreadBaselinePreparation:
    def __init__(self, root_dir: Path | str, client: Callable[[], AgentPosixSdkClient]) -> None:
        self.root_dir, self.client = root_dir, client

    def prepare_baseline(self, spec: SearchSpec) -> ThinkThreadBaseline:
        return ThinkThreadBaseline(
            self.root_dir, self.client(), thinkthread_source_relative_path(Path(spec.source_path)),
        )
