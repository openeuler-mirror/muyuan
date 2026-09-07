"""Host observations and workspace inspection, without task reconciliation."""

from pathlib import Path

from goal_plus.agent_hosts import candidate_inner_agent_binding, collect_host_observability
from goal_plus.infrastructure.artifacts.git import git_changed_files
from goal_plus.ports.search_configuration import WorkerPolicyBinding
from goal_plus.infrastructure.artifacts.source_repository import SourceRepository
from goal_plus.infrastructure.verifiers.local import LocalVerifiers


class HostSearchInspection:
    def __init__(self, repository: SourceRepository, bindings: WorkerPolicyBinding, verifiers: LocalVerifiers) -> None:
        self.repository, self.bindings = repository, bindings
        self.verifiers = verifiers

    def git_head(self, workspace: Path) -> str:
        return self.verifiers.git_head(workspace)

    def git_status(self, workspace: Path, *, ignore_runtime_noise: bool = False):
        return self.repository.git_status(
            workspace, ignore_runtime_noise=ignore_runtime_noise
        )

    def changed_files(self, workspace: Path, base: str, head: str):
        return git_changed_files(workspace, base, head)

    def inner_agent(self, frozen, task):
        return candidate_inner_agent_binding(frozen, task).inner_agent_id

    def normalize_worker_policy(self, strategy):
        return self.bindings.normalize_worker_policy(strategy)

    def observability(self, session):
        return collect_host_observability(session)
