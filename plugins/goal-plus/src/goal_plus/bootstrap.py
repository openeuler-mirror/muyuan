"""Construct concrete implementations without deciding domain transitions."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from goal_plus.ports.thinkthread_verification import ThinkThreadAttemptIO

if TYPE_CHECKING:
    from goal_plus.adaptive.steering import Steering

from goal_plus.application.allocation import AllocationDecisions
from goal_plus.application.allocation_apply import AllocationApply
from goal_plus.application.annotation import AnnotationWorkflow
from goal_plus.application.annotation_admission import AnnotationAdmission
from goal_plus.application.annotation_context import AnnotationContextBuilder
from goal_plus.application.goal_authorization import GoalAuthorization
from goal_plus.application.internal_agents import InternalAgentWork
from goal_plus.application.search_context import SearchContext
from goal_plus.execution.drainer import DetachedDrainer
from goal_plus.execution.process_monitor import ProcessMonitor
from goal_plus.execution.codex_scope import CodexScopeRegistry
from goal_plus.execution.scope_activation import ScopeActivation
from goal_plus.execution.scope_cleanup import ScopeCleanup
from goal_plus.execution.scope_processes import ScopeProcessRegistry
from goal_plus.execution.structured_inference import StructuredInference
from goal_plus.infrastructure.artifacts.reader import ProviderArtifactReader
from goal_plus.infrastructure.clock import SystemClock
from goal_plus.infrastructure.state.allocation import FileAllocationState
from goal_plus.infrastructure.state.annotation import FileAnnotationState
from goal_plus.infrastructure.state.drainer import FileDrainerState
from goal_plus.infrastructure.state.scope import FileScopeState
from goal_plus.infrastructure.state.goal import FileGoalQueries
from goal_plus.infrastructure.state.verifier import FileVerifierState
from goal_plus.infrastructure.state.search_context import FileSearchContextSource
from goal_plus.infrastructure.state.process_monitor import FileProcessMonitorSink
from goal_plus.ports.candidate_creation import (
    MaterializeCandidate,
    PrepareInitialSession,
)
from goal_plus.ports.process_monitor import ProcessMonitorIdentity
from goal_plus.ports.process import ProcessRegistration
from goal_plus.ports.scope import ScopeCleanupServices, ScopeState
from goal_plus.thinkthread_agent_posix import AgentPosixSdkClient


def build_scope_state(root_dir: Path | str) -> ScopeState:
    return FileScopeState(root_dir, SystemClock())


def build_codex_scope_registry(root_dir: Path | str) -> CodexScopeRegistry:
    return CodexScopeRegistry(state=build_scope_state(root_dir), clock=SystemClock())


def build_scope_process_registry(root_dir: Path | str) -> ScopeProcessRegistry:
    return ScopeProcessRegistry(state=build_scope_state(root_dir), clock=SystemClock())


def build_scope_cleanup(
    root_dir: Path | str, services: ScopeCleanupServices
) -> ScopeCleanup:
    state, clock = build_scope_state(root_dir), SystemClock()
    return ScopeCleanup(
        state=state,
        clock=clock,
        processes=ScopeProcessRegistry(state=state, clock=clock),
        codex=CodexScopeRegistry(state=state, clock=clock),
        services=services,
    )


def build_scope_activation(
    root_dir: Path | str
) -> ScopeActivation:
    return ScopeActivation(
        state=build_scope_state(root_dir),
        clock=SystemClock(),
    )


def build_detached_drainer(
    root_dir: Path | str,
    run_id: str,
    *,
    namespace: str,
    module: str,
    log_name: str,
    label: str,
) -> DetachedDrainer:
    root = Path(root_dir).expanduser().resolve()
    return DetachedDrainer(
        state=FileDrainerState(root / "runs" / run_id / namespace, log_name),
        root_dir=root,
        run_id=run_id,
        module=module,
        source_root=Path(__file__).resolve().parents[1],
        label=label,
        clock=SystemClock(),
    )


def build_structured_inference(
    *,
    popen: Callable[..., subprocess.Popen[str]] | None = None,
) -> StructuredInference:
    return StructuredInference(clock=SystemClock(), popen=popen)


def build_inference_transcript(root_dir, identity):
    from goal_plus.infrastructure.inference_transcript import FileInferenceTranscript, inference_capture_enabled

    if not root_dir or identity is None or not inference_capture_enabled():
        return None
    return FileInferenceTranscript(Path(root_dir).resolve(), identity)


def build_inference_registration(
    root_dir: Path | str | None,
    run_id: str | None,
    *,
    label: str,
) -> ProcessRegistration | None:
    from goal_plus.host_scope import (
        execution_scope_for_run,
        finish_execution_process,
        register_execution_process,
    )

    if not root_dir or not run_id:
        return None
    scope = execution_scope_for_run(root_dir=root_dir, run_id=run_id)
    if scope is None:
        return None
    scope_id = str(scope["scope_id"])
    return ProcessRegistration(
        register=lambda pid: register_execution_process(
            root_dir=root_dir,
            scope_id=scope_id,
            run_id=run_id,
            pid=pid,
            label=label,
            kind="internal",
        ),
        finish=lambda token: finish_execution_process(
            root_dir=root_dir, scope_id=scope_id, token=token
        ),
    )


def build_process_monitor(
    path: Path | None,
    identity: ProcessMonitorIdentity,
    *,
    include_event_timestamps: bool = False,
) -> ProcessMonitor:
    return ProcessMonitor(
        identity,
        sink=FileProcessMonitorSink(path) if path is not None else None,
        clock=SystemClock(),
        include_event_timestamps=include_event_timestamps,
    )


def build_internal_agent_work(root_dir: Path | str) -> InternalAgentWork:
    return InternalAgentWork(FileAnnotationState(root_dir))


def build_settlement_verifier(root_dir):
    from goal_plus.application.settlement_verifier import SettlementVerifier
    from goal_plus.infrastructure.state.candidate_queries import FileCandidateQueries
    return SettlementVerifier(workflow=VerifierWorkflow(FileVerifierState(root_dir), SystemClock()),
        artifacts=ProviderArtifactReader(AgentPosixSdkClient), source=FileCandidateQueries(root_dir))


def build_goal_authorization(
    root_dir: Path | str,
) -> tuple[GoalAuthorization, FileGoalQueries]:
    source = FileGoalQueries(root_dir)
    return GoalAuthorization(source), source


def build_candidate_sessions(root_dir: Path | str):
    from goal_plus.application.adaptive_admission import AdaptiveAdmission
    from goal_plus.application.candidate_sessions import CandidateSessions
    from goal_plus.infrastructure.artifacts.candidate_launch import HostCandidateLaunch
    from goal_plus.infrastructure.state.candidate_sessions import (
        FileCandidateSessionState,
    )

    state, clock = FileCandidateSessionState(root_dir), SystemClock()
    authorization, _ = build_goal_authorization(root_dir)
    return CandidateSessions(
        state=state,
        launch=HostCandidateLaunch(root_dir),
        admission=AdaptiveAdmission(state),
        authorization=authorization,
        context=SearchContext(FileSearchContextSource(root_dir)),
        clock=clock,
    )


def build_thinkthread_verifiers(root_dir: Path | str, local):
    from goal_plus.infrastructure.artifacts.thinkthread_mutations import (
        ThinkThreadMutations,
    )
    from goal_plus.infrastructure.verifiers.thinkthread import ThinkThreadVerifiers

    mutations = ThinkThreadMutations(root_dir)
    return mutations, ThinkThreadVerifiers(root_dir, mutations, local)


def build_search_research(root_dir: Path | str):
    from goal_plus.application.search_research import SearchResearch
    from goal_plus.infrastructure.state.candidate_queries import FileCandidateQueries

    return SearchResearch(FileCandidateQueries(root_dir))


def build_thinkthread_verification(*, settlement, client_factory, verifiers, mutations):
    from goal_plus.application.thinkthread_verification import ThinkThreadVerification
    from goal_plus.infrastructure.verifiers.thinkthread_attempt import (
        ProviderThinkThreadAttempt,
    )

    def io_factory(client):
        return ProviderThinkThreadAttempt(
            client or client_factory(), verifiers, mutations
        )

    return ThinkThreadVerification(
        state=settlement.state,
        clock=SystemClock(),
        authorization=settlement.authorization,
        candidate_sessions=settlement.candidate_sessions,
        candidate_ledger=settlement.candidate_ledger,
        shared_tools=settlement.shared_tools,
        settlement=settlement,
    ), io_factory


def build_candidate_verification(*, settlement, context, verifiers):
    from goal_plus.application.candidate_verification import CandidateVerification

    return CandidateVerification(
        state=settlement.state,
        clock=SystemClock(),
        authorization=settlement.authorization,
        context=context,
        candidate_sessions=settlement.candidate_sessions,
        artifacts=settlement.artifacts,
        candidate_ledger=settlement.candidate_ledger,
        shared_tools=settlement.shared_tools,
        settlement=settlement,
        verifiers=verifiers,
        persist_run=settlement.persist_run,
    )


def build_candidate_recovery(root_dir, *, settlement, sessions, context):
    from goal_plus.application.candidate_recovery import CandidateRecoveryWorkflow
    from goal_plus.infrastructure.artifacts.candidate_recovery import (
        ProviderCandidateRecovery,
    )

    return CandidateRecoveryWorkflow(
        state=settlement.state,
        clock=SystemClock(),
        workspace=ProviderCandidateRecovery(root_dir),
        sessions=sessions,
        authorization=settlement.authorization,
        context=context,
        candidate_ledger=settlement.candidate_ledger,
        shared_tools=settlement.shared_tools,
        checkpoints=settlement.checkpoints,
        persist_run=settlement.persist_run,
    )


def build_candidate_settlement(
    root_dir,
    *,
    authorization,
    sessions,
    artifacts,
    ledger,
    shared_tools,
    persist_run,
    trace,
):
    from goal_plus.application.candidate_checkpoints import CandidateCheckpoints
    from goal_plus.application.candidate_settlement import CandidateSettlement
    from goal_plus.infrastructure.state.candidate_settlement import (
        FileCandidateSettlementState,
    )

    state = FileCandidateSettlementState(root_dir)
    checkpoints = CandidateCheckpoints(state)
    settlement = CandidateSettlement(
        state=state,
        clock=SystemClock(),
        authorization=authorization,
        candidate_sessions=sessions,
        artifacts=artifacts,
        candidate_ledger=ledger,
        shared_tools=shared_tools,
        checkpoints=checkpoints,
        persist_run=persist_run,
        verifier_submission=build_settlement_verifier(root_dir),
        trace=trace,
    )
    return settlement, checkpoints


def build_candidate_artifacts(ledger):
    from goal_plus.application.candidate_artifacts import CandidateArtifacts
    from goal_plus.infrastructure.artifacts.candidate_artifacts import (
        FileCandidateArtifacts,
    )

    return CandidateArtifacts(FileCandidateArtifacts(), ledger)


def build_publication_notifications(root_dir, persist_run):
    from goal_plus.application.publication_notifications import PublicationNotifications
    from goal_plus.infrastructure.state.publication import FilePublicationState

    return PublicationNotifications(
        FilePublicationState(root_dir), SystemClock(), persist_run
    )


def build_selection(
    *,
    settlement,
    recovery,
    annotation,
    annotation_admission,
    verifier,
):
    from goal_plus.application.selection import Selection

    return Selection(
        state=settlement.state,
        recovery=recovery,
        artifacts=settlement.artifacts,
        candidate_ledger=settlement.candidate_ledger,
        checkpoints=settlement.checkpoints,
        annotation=annotation,
        annotation_admission=annotation_admission,
        authorization=settlement.authorization,
        verifier=verifier,
        persist_run=settlement.persist_run,
    )


def build_references(root_dir, sessions, trace):
    from goal_plus.application.references import ReferenceWorkspaces
    from goal_plus.infrastructure.state.references import FileReferenceState
    from goal_plus.infrastructure.artifacts.references import ProviderReferenceWorkspace

    return ReferenceWorkspaces(
        FileReferenceState(root_dir),
        ProviderReferenceWorkspace(),
        sessions,
        SystemClock(),
        trace,
    )


def build_shared_tools(root_dir: Path | str, annotation: AnnotationWorkflow, sessions):
    from goal_plus.application.shared_tools import SharedTools
    from goal_plus.infrastructure.artifacts.shared_tools import (
        FileToolPublicationWorkspace,
    )
    from goal_plus.infrastructure.state.candidate_settlement import (
        FileCandidateSettlementState,
    )

    return SharedTools(
        FileCandidateSettlementState(root_dir),
        FileToolPublicationWorkspace(root_dir),
        annotation,
        sessions,
        SystemClock(),
    )


def build_search_run(
    root_dir: Path | str, *, repository, bindings, client_factory, creation, sessions, research, verifiers, annotation
):
    from goal_plus.application.search_run import SearchRun
    from goal_plus.infrastructure.state.search_run import FileSearchRunState
    from goal_plus.infrastructure.state.device_leases import FileDeviceLeases
    from goal_plus.infrastructure.artifacts.thinkthread_baseline import ThinkThreadBaselinePreparation

    state = FileSearchRunState(root_dir)
    return SearchRun(
        state=state,
        source=repository,
        bindings=bindings,
        baseline=ThinkThreadBaselinePreparation(root_dir, client_factory),
        creation=creation,
        sessions=sessions,
        research=research,
        verifiers=verifiers,
        annotation=annotation,
        leases=FileDeviceLeases(root_dir, state),
        authorization=sessions.authorization,
        clock=SystemClock(),
    )


def build_allocation_services(
    root_dir: Path | str,
    *,
    materialize: MaterializeCandidate,
    start_session: PrepareInitialSession,
) -> tuple[AllocationDecisions, AllocationApply]:
    state = FileAllocationState(root_dir)
    authorization, _ = build_goal_authorization(root_dir)
    clock = SystemClock()
    return (
        AllocationDecisions(state=state, clock=clock),
        AllocationApply(
            state=state,
            authorization=authorization,
            materialize=materialize,
            start_session=start_session,
            clock=clock,
        ),
    )


def build_steering(root_dir: Path | str) -> Steering:
    """Wire the versioned Steering strategy path and its executor."""
    from goal_plus.adaptive.steering import Steering, SteeringExecutor
    from goal_plus.candidate_host import candidate_host_queries
    from goal_plus.infrastructure.state.steering import FileSteeringState
    from goal_plus.application.steering import SteeringDiagnostics

    authorization, _ = build_goal_authorization(root_dir)
    return Steering(
        diagnostics=SteeringDiagnostics(state=FileSteeringState(root_dir), clock=SystemClock()),
        executor=SteeringExecutor(state=FileSteeringState(root_dir), authorization=authorization,
                                  queries=candidate_host_queries(root_dir), clock=SystemClock()),
    )


def build_annotation_services(
    root_dir: Path | str,
) -> tuple[
    AnnotationWorkflow, AnnotationAdmission, SearchContext, AnnotationContextBuilder
]:
    state = FileAnnotationState(root_dir)
    context = SearchContext(FileSearchContextSource(root_dir))
    workflow = AnnotationWorkflow(
        tasks=state, evidence=state, tools=state, clock=SystemClock()
    )
    admission = AnnotationAdmission(workflow=workflow, source=state, context=context)
    builder = AnnotationContextBuilder(
        workflow=workflow,
        source=state,
        admission_source=state,
        context=context,
        artifacts=ProviderArtifactReader(AgentPosixSdkClient),
        root_dir=state.root_dir,
    )
    return workflow, admission, context, builder


def build_candidate_creation(root_dir: Path | str):
    from goal_plus.application.candidate_creation import CandidateCreation
    from goal_plus.application.candidate_ledger import CandidateLedger
    from goal_plus.infrastructure.artifacts.candidate_workspace import (
        FileCandidateWorkspaceCreation,
    )
    from goal_plus.infrastructure.artifacts.candidate_ledger import (
        FileCandidateLedgerWorkspace,
    )
    from goal_plus.infrastructure.state.candidate_creation import (
        FileCandidateCreationState,
    )

    state = FileCandidateCreationState(root_dir)
    ledger = CandidateLedger(state, FileCandidateLedgerWorkspace())
    return CandidateCreation(
        FileCandidateWorkspaceCreation(root_dir), state, ledger
    ), ledger


def build_search_bootstrap(root_dir: Path | str, verifiers):
    from goal_plus.application.search_bootstrap import SearchBootstrap
    from goal_plus.infrastructure.artifacts.search_source import FileFreezeSource
    from goal_plus.infrastructure.artifacts.source_repository import SourceRepository
    from goal_plus.infrastructure.search_configuration import SearchConfiguration, HostModelDiscovery
    from goal_plus.infrastructure.state.search_bootstrap import FileFrozenSpecState

    repository, models = SourceRepository(), HostModelDiscovery()
    configuration = SearchConfiguration(models)
    source = FileFreezeSource(root_dir, configuration, repository)
    authorization, _ = build_goal_authorization(root_dir)
    return SearchBootstrap(
        source, FileFrozenSpecState(root_dir), verifiers, SystemClock(), repository,
        authorization,
    ), repository, configuration, models


def build_publication(
    root_dir, *, client_factory, mutations, verifiers, settlement, recovery, notifications
):
    from goal_plus.application.promotion import Promotion
    from goal_plus.application.thinkthread_publication import ThinkThreadPublication
    from goal_plus.application.publication_apply import PublicationApply
    from goal_plus.infrastructure.artifacts.publication import LocalPublication
    from goal_plus.infrastructure.artifacts.thinkthread_publication import (
        ThinkThreadPublicationSnapshots, ThinkThreadApply, ThinkThreadPublicationVerifier,
    )
    from goal_plus.infrastructure.state.publication import FilePublicationState

    state, clock = FilePublicationState(root_dir), SystemClock()
    io = LocalPublication(root_dir)
    client = client_factory
    tt_apply = ThinkThreadApply(client, mutations)
    persist = settlement.persist_run
    thinkthread = ThinkThreadPublication(
        state,
        ThinkThreadPublicationSnapshots(client, mutations),
        tt_apply,
        ThinkThreadPublicationVerifier(client, verifiers),
        clock, persist,
    )
    return (
        Promotion(
            state=state,
            io=io,
            clock=clock,
            persist_run=persist,
            authorization=settlement.authorization,
            artifacts=settlement.artifacts,
            candidate_ledger=settlement.candidate_ledger,
            recovery=recovery,
            thinkthread=thinkthread,
        ),
        PublicationApply(
            state=state,
            io=io,
            thinkthread=tt_apply,
            clock=clock,
            persist_run=persist,
            authorization=settlement.authorization,
            notifications=notifications,
        ),
    )


def build_artifact_lifecycle(root_dir, client_factory, mutations, persist_run):
    from goal_plus.application.artifact_lifecycle import ArtifactLifecycle
    from goal_plus.infrastructure.state.artifact_lifecycle import (
        FileArtifactLifecycleState,
    )
    from goal_plus.infrastructure.artifacts.snapshot_removal import (
        ThinkThreadSnapshotRemoval,
    )

    return ArtifactLifecycle(
        FileArtifactLifecycleState(root_dir),
        ThinkThreadSnapshotRemoval(client_factory, mutations),
        SystemClock(),
        persist_run,
    )


def build_goal_mutations(root_dir, authorization):
    from goal_plus.infrastructure.state.goal_mutations import FileGoalState
    from goal_plus.application.goal_revisions import GoalRevisions
    from goal_plus.application.goal_control import GoalControl
    from goal_plus.application.goal_dispatch import GoalDispatch

    state, clock = FileGoalState(root_dir), SystemClock()
    return (
        state,
        GoalRevisions(state, clock, authorization),
        GoalControl(state, clock, authorization),
        GoalDispatch(state, clock, authorization),
    )


def build_recovery_queries(root_dir):
    from goal_plus.application.recovery_queries import CandidateRecoveryQueries
    from goal_plus.infrastructure.state.candidate_settlement import (
        FileCandidateSettlementState,
    )
    from goal_plus.infrastructure.state.candidate_sessions import (
        FileCandidateSessionState,
    )
    from goal_plus.infrastructure.artifacts.candidate_recovery import (
        ProviderCandidateRecovery,
    )

    return CandidateRecoveryQueries(
        FileCandidateSettlementState(root_dir),
        ProviderCandidateRecovery(root_dir),
        FileCandidateSessionState(root_dir),
    )


def build_goal_closeout(root_dir, state, authorization):
    from goal_plus.application.goal_search import GoalSearch
    from goal_plus.application.goal_completion import GoalCompletion
    from goal_plus.application.goal_gate import GoalGate
    from goal_plus.application.goal_reconciliation import GoalReconciliation
    from goal_plus.infrastructure.state.goal_search import FileGoalSearchArtifacts

    clock, artifacts = SystemClock(), FileGoalSearchArtifacts(root_dir)
    reconciliation = GoalReconciliation(state, clock, build_recovery_queries(root_dir))
    return (
        GoalSearch(state, clock, artifacts, authorization),
        GoalCompletion(state, clock, artifacts, reconciliation),
        GoalGate(state, clock, authorization, reconciliation),
        reconciliation,
    )


def build_goal_reconciliation(root_dir):
    from goal_plus.application.goal_reconciliation import GoalReconciliation

    return GoalReconciliation(
        FileGoalQueries(root_dir), SystemClock(), build_recovery_queries(root_dir)
    )


def build_report(root_dir, *, research, ledger, lifecycle):
    from goal_plus.application.report import Report
    from goal_plus.infrastructure.report_output import (
        FileReportQueries,
        FileReportOutput,
    )

    return Report(
        FileReportQueries(root_dir, research, ledger),
        FileReportOutput(root_dir),
        lifecycle,
    )


def build_goal_observation(state):
    from goal_plus.application.goal_observation import GoalObservation

    return GoalObservation(state, SystemClock())


def build_search_queries(
    root_dir,
    *,
    sessions,
    ledger,
    repository,
    bindings,
    verifiers,
    artifacts,
    references,
    shared_tools,
    research,
    annotation,
    admission,
    trace,
):
    from goal_plus.application.candidate_context import CandidateContext
    from goal_plus.application.evidence_reads import EvidenceReads
    from goal_plus.application.search_queries import SearchQueries
    from goal_plus.infrastructure.state.candidate_creation import (
        FileCandidateCreationState,
    )
    from goal_plus.infrastructure.state.candidate_sessions import (
        FileCandidateSessionState,
    )
    from goal_plus.infrastructure.search_inspection import HostSearchInspection
    from goal_plus.infrastructure.evidence_attachments import FileEvidenceAttachments
    from goal_plus.infrastructure.clock import SystemClock

    state = FileCandidateCreationState(root_dir)
    inspection = HostSearchInspection(repository, bindings, verifiers)
    return (
        CandidateContext(
            state=state,
            sessions=sessions,
            ledger=ledger,
            inspection=inspection,
            artifacts=artifacts,
            references=references,
            shared_tools=shared_tools,
        ),
        SearchQueries(
            state=state,
            sessions=sessions,
            research=research,
            inspection=inspection,
            artifacts=artifacts,
        ),
        EvidenceReads(
            state=FileCandidateSessionState(root_dir),
            sessions=sessions,
            annotation=annotation,
            admission=admission,
            attachments=FileEvidenceAttachments(root_dir),
            clock=SystemClock(),
            trace=trace,
        ),
    )


from goal_plus.infrastructure.verifiers.local import LocalVerifiers
from goal_plus.infrastructure.artifacts.thinkthread_mutations import (
    ThinkThreadMutations,
)
from goal_plus.infrastructure.verifiers.thinkthread import ThinkThreadVerifiers
from goal_plus.application.search_bootstrap import SearchBootstrap
from goal_plus.ports.search_configuration import ModelDiscovery
from goal_plus.application.candidate_creation import CandidateCreation
from goal_plus.application.candidate_ledger import CandidateLedger
from goal_plus.application.candidate_sessions import CandidateSessions
from goal_plus.application.search_research import SearchResearch
from goal_plus.application.search_run import SearchRun
from goal_plus.application.candidate_artifacts import CandidateArtifacts
from goal_plus.application.shared_tools import SharedTools
from goal_plus.application.references import ReferenceWorkspaces
from goal_plus.application.candidate_settlement import CandidateSettlement
from goal_plus.application.candidate_checkpoints import CandidateCheckpoints
from goal_plus.application.candidate_recovery import CandidateRecoveryWorkflow
from goal_plus.application.candidate_verification import CandidateVerification
from goal_plus.application.thinkthread_verification import ThinkThreadVerification
from goal_plus.application.selection import Selection
from goal_plus.application.publication_notifications import PublicationNotifications
from goal_plus.application.promotion import Promotion
from goal_plus.application.publication_apply import PublicationApply
from goal_plus.application.artifact_lifecycle import ArtifactLifecycle
from goal_plus.adaptive import AdaptiveSearch
from goal_plus.application.candidate_context import CandidateContext
from goal_plus.application.search_queries import SearchQueries
from goal_plus.application.evidence_reads import EvidenceReads
from goal_plus.application.candidate_trace import CandidateTrace
from goal_plus.application.report import Report
from goal_plus.application.verifier import VerifierWorkflow


# This assembly result is consumed only by the public facade, never by a use case.
@dataclass(frozen=True)
class SearchServices:
    thinkthread_attempt_io: Callable[[AgentPosixSdkClient | None], ThinkThreadAttemptIO]
    verifiers: LocalVerifiers
    candidate_sessions: CandidateSessions
    research: SearchResearch
    search_run: SearchRun
    artifacts: CandidateArtifacts
    shared_tools: SharedTools
    references: ReferenceWorkspaces
    recovery: CandidateRecoveryWorkflow
    verification: CandidateVerification
    thinkthread_verification: ThinkThreadVerification
    selection: Selection
    notifications: PublicationNotifications
    artifact_lifecycle: ArtifactLifecycle
    verifier: VerifierWorkflow
    adaptive_search: AdaptiveSearch
    reporter: Report
    goal_authorization: GoalAuthorization
    annotation: AnnotationWorkflow
    annotation_admission: AnnotationAdmission
    search_context: SearchContext
    annotation_context: AnnotationContextBuilder
    provider_mutations: ThinkThreadMutations
    thinkthread_verifiers: ThinkThreadVerifiers
    search_bootstrap: SearchBootstrap
    models: ModelDiscovery
    candidate_creation: CandidateCreation
    candidate_ledger: CandidateLedger
    settlement: CandidateSettlement
    checkpoints: CandidateCheckpoints
    promotion: Promotion
    publication_apply: PublicationApply
    context: CandidateContext
    queries: SearchQueries
    evidence_reads: EvidenceReads
    trace: CandidateTrace


def build_search_services(root_dir: Path, *, steering: Steering | None = None) -> SearchServices:
    from goal_plus.adaptive.allocation import AllocationExecutor

    goal_authorization, _ = build_goal_authorization(root_dir)
    (
        annotation,
        annotation_admission,
        search_context,
        annotation_context,
    ) = build_annotation_services(root_dir)
    verifiers = LocalVerifiers(root_dir)
    verifier = VerifierWorkflow(FileVerifierState(root_dir), SystemClock())
    provider_mutations, thinkthread_verifiers = build_thinkthread_verifiers(
        root_dir, verifiers
    )
    search_bootstrap, repository, bindings, models = build_search_bootstrap(root_dir, verifiers)
    client_factory = lambda: AgentPosixSdkClient()
    candidate_creation, candidate_ledger = build_candidate_creation(root_dir)
    candidate_sessions = build_candidate_sessions(root_dir)
    from goal_plus.infrastructure.state.candidate_trace import FileCandidateTraceStore
    trace = CandidateTrace(FileCandidateTraceStore(root_dir), SystemClock())
    research = build_search_research(root_dir)
    search_run = build_search_run(
        root_dir,
        repository=repository,
        bindings=bindings,
        client_factory=client_factory,
        creation=candidate_creation,
        sessions=candidate_sessions,
        research=research,
        verifiers=verifiers,
        annotation=annotation,
    )
    artifacts = build_candidate_artifacts(candidate_ledger.workspace)
    shared_tools = build_shared_tools(root_dir, annotation, candidate_sessions)
    references = build_references(root_dir, candidate_sessions, trace)
    settlement, checkpoints = build_candidate_settlement(
        root_dir,
        authorization=goal_authorization,
        sessions=candidate_sessions,
        artifacts=artifacts,
        ledger=candidate_ledger,
        shared_tools=shared_tools,
        persist_run=search_run.persist,
        trace=trace,
    )
    recovery = build_candidate_recovery(
        root_dir,
        settlement=settlement,
        sessions=candidate_sessions,
        context=search_context,
    )
    verification = build_candidate_verification(
        settlement=settlement,
        context=search_context,
        verifiers=verifiers,
    )
    thinkthread_verification, thinkthread_attempt_io = build_thinkthread_verification(
        settlement=settlement,
        client_factory=client_factory,
        verifiers=thinkthread_verifiers,
        mutations=provider_mutations,
    )
    selection = build_selection(
        settlement=settlement,
        recovery=recovery,
        annotation=annotation,
        annotation_admission=annotation_admission,
        verifier=verifier,
    )
    notifications = build_publication_notifications(root_dir, search_run.persist)
    promotion, publication_apply = build_publication(
        root_dir,
        client_factory=client_factory,
        mutations=provider_mutations,
        verifiers=thinkthread_verifiers,
        settlement=settlement,
        recovery=recovery,
        notifications=notifications,
    )
    artifact_lifecycle = build_artifact_lifecycle(
        root_dir,
        client_factory,
        provider_mutations,
        search_run.persist,
    )
    from goal_plus.application.adaptive_snapshot import AdaptiveSnapshotBuilder
    adaptive_search = AdaptiveSearch(
        steering=steering if steering is not None else build_steering(root_dir),
        snapshots=AdaptiveSnapshotBuilder(state=FileAllocationState(root_dir)),
        allocation=AllocationExecutor(
            *build_allocation_services(
                root_dir,
                materialize=candidate_creation.materialize,
                start_session=candidate_sessions.start_agent_session,
            )
        ),
    )
    context, queries, evidence_reads = build_search_queries(
        root_dir,
        sessions=candidate_sessions,
        ledger=candidate_ledger,
        repository=repository,
        bindings=bindings,
        verifiers=verifiers,
        artifacts=artifacts,
        references=references,
        shared_tools=shared_tools,
        research=research,
        annotation=annotation,
        admission=annotation_admission,
        trace=trace,
    )
    reporter = build_report(
        root_dir,
        research=research,
        ledger=candidate_ledger,
        lifecycle=artifact_lifecycle,
    )
    return SearchServices(
        verifiers=verifiers,
        candidate_sessions=candidate_sessions,
        research=research,
        search_run=search_run,
        artifacts=artifacts,
        shared_tools=shared_tools,
        references=references,
        recovery=recovery,
        verification=verification,
        thinkthread_verification=thinkthread_verification,
        thinkthread_attempt_io=thinkthread_attempt_io,
        selection=selection,
        notifications=notifications,
        artifact_lifecycle=artifact_lifecycle,
        verifier=verifier,
        adaptive_search=adaptive_search,
        reporter=reporter,
        goal_authorization=goal_authorization,
        annotation=annotation,
        annotation_admission=annotation_admission,
        search_context=search_context,
        annotation_context=annotation_context,
        provider_mutations=provider_mutations,
        thinkthread_verifiers=thinkthread_verifiers,
        search_bootstrap=search_bootstrap,
        models=models,
        candidate_creation=candidate_creation,
        candidate_ledger=candidate_ledger,
        settlement=settlement,
        checkpoints=checkpoints,
        promotion=promotion,
        publication_apply=publication_apply,
        context=context,
        queries=queries,
        evidence_reads=evidence_reads,
        trace=trace,
    )
