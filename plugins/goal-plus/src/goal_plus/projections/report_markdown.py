from pathlib import Path
from goal_plus.ports.report import MarkdownReportInputs
from goal_plus.ports.state import CandidateRecord, RunRecord
from goal_plus.domain.evidence import ResultLedgerEntry
from goal_plus.domain.artifacts import ArtifactRef
from goal_plus.domain.results_ledger import RESULTS_TSV_RELATIVE_PATH
from goal_plus.domain.agents import WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV
from goal_plus.domain.serialization import canonical_json
from goal_plus.application.candidate_ranking import best_iteration_record
from goal_plus.application.artifact_availability import artifact_availability
from goal_plus.projections.publication import publication_summary
from goal_plus.projections.fork import dispatch_group_summary


def markdown_cell(value: str) -> str:
    return value.replace("\n", " ").replace("|", "\\|")


def render_markdown_report(inputs: MarkdownReportInputs) -> str:
    run, frozen = inputs.run, inputs.frozen
    run_id, report_path = run.run_id, inputs.path
    records, plans = inputs.candidates, inputs.plans
    publication = publication_summary(run)
    outcome = publication["publication_outcome"] or {}
    reference = outcome.get("publication_artifact_ref") or {}
    artifact = reference.get("id") or outcome.get("artifact_path") or ""
    lines = [
        f"# Search Report: {run_id}",
        "",
        "- HTML report: [report.html](report.html)",
        f"- Frozen spec: `{frozen.frozen_spec_id}`",
        f"- State: `{run.state}`",
        f"- Search result: `{publication['search_result']}`",
        f"- Publication result: `{publication['publication_result']}`",
        f"- Publication artifact: `{artifact}`",
        f"- Publication apply request: `{outcome.get('apply_request_id') or ''}`",
        f"- Source run: `{run.source_run_id}`",
        f"- Replacement run: `{run.replacement_run_id}`",
        f"- Spec hash: `{frozen.spec_hash}`",
        f"- Objective: {frozen.spec.objective}",
        f"- Metric: `{frozen.spec.metric_name}` ({frozen.spec.metric_direction})",
        f"- Initial strategy: `{frozen.spec.strategy.name}`",
        f"- Best candidate: `{run.best_candidate_id}`",
        f"- Best score: `{run.best_score}`",
        f"- Selected score: `{run.selected_score}`",
        f"- Selected frozen spec: `{run.selected_frozen_spec_id}`",
        f"- Selected iteration: `{run.selected_iteration}`",
        f"- Selected git head: `{run.selected_git_head}`",
        f"- Invalidated at: `{run.invalidated_at}`",
        f"- Invalidation reason: `{run.invalidation_reason}`",
        f"- Invalidation summary: {run.invalidation_summary or ''}",
        (
            "- Invalidation evidence: "
            f"{markdown_cell(canonical_json(run.invalidation_evidence))}"
        ),
        "",
        "## Bootstrap Verification",
        "",
        "Bootstrap checks are control-plane gates. They are not candidate "
        "iterations and do not participate in ranking.",
        "",
        "| Phase | Backend | Candidate | Passed | Score | Source Artifact | Verifiers |",
        "|---|---|---|---|---:|---|---|",
    ]
    for verification in [
        frozen.origin_verification,
        run.workspace_bootstrap_verification,
    ]:
        if verification is None:
            continue
        artifact = verification.source_artifact_ref
        verifier_summary = ", ".join(
            f"{result.name}={'pass' if result.passed else 'fail'}"
            for result in verification.verifier_results
        )
        lines.append(
            f"| `{verification.phase}` | `{verification.workspace_provider}` | "
            f"`{verification.candidate_id or ''}` | {verification.passed} | "
            f"{'' if verification.aggregate_score is None else verification.aggregate_score} | "
            f"{markdown_cell(artifact.id if artifact is not None else '')} | "
            f"{markdown_cell(verifier_summary)} |"
        )
    fork = dispatch_group_summary(run.dispatch_group)
    if fork is not None:
        lines.extend([
            "", "## Fork", "",
            f"- Group: `{fork['dispatch_group_id']}` ({fork['status']})",
            f"- Runtime provider: `{fork['runtime_provider']}`",
            f"- Main session: `{fork['main_session_id']}`",
            f"- Parent native session: `{fork['parent_native_session_id']}`",
            f"- Checkpoint: `{fork['checkpoint_id']}` ({fork['checkpoint_kind']})",
            f"- Frozen spec: `{fork['frozen_spec_id']}`",
            "", "| Branch | Candidate | Session | Native session | Launch | Closed at | Audit | Audit record | Attachment | Receipt |",
            "|---:|---|---|---|---|---|---|---|---|---|",
        ])
        for branch in fork["targets"]:
            binding = branch["binding"]
            lines.append(
                f"| {branch['dispatch_target_index']} | `{branch['candidate_id']}` | `{branch['agent_session_id']}` | "
                f"`{branch['provider_session_id'] or ''}` | {branch['launch_state']} | "
                f"{branch['closed_at'] or ''} | {branch['audit_state']} | `{branch['audit_record_id'] or ''}` | "
                f"{markdown_cell(canonical_json(binding['workspace_attachment']))} | "
                f"{markdown_cell(canonical_json(binding['launch_receipt']))} |"
            )
    lines.extend(
        [
            "",
            "## Strategy Plans",
            "",
            "| Plan | Status | Strategy | Initial Context | Adaptive | Requested | Planned | Started Candidates | Trace |",
            "|---|---|---|---|---|---:|---:|---|---|",
        ]
    )
    for plan in plans:
        trace = (
            plan.strategy_trace.get("reason")
            or plan.strategy_trace.get("selection_rule")
            or ""
        )
        lines.append(
            f"| `{plan.plan_id}` | {plan.status} | `{plan.strategy.name}` | "
            f"`{plan.strategy.initial_context}` | "
            f"{'yes' if plan.strategy.adaptive_search_enabled else 'no'} | "
            f"{plan.requested_k} | {plan.planned_k} | "
            f"{markdown_cell(', '.join(plan.started_candidate_ids))} | "
            f"{markdown_cell(str(trace))} |"
        )
    lines.extend(
        [
            "",
            "## Candidates",
            "",
            "Scores use each candidate's active evaluation contract. Scores from different frozen specs are not compared.",
            "",
            "| Candidate | Active Frozen Spec | Metric | Plan | Agent Sessions | Parent/Base | Status | Score | Git Head | Best Iteration | Best Score | Best Git Head | Process | Summary | Key Metrics | Changed Files | Results Ledger |",
            "|---|---|---|---|---|---|---|---:|---|---:|---:|---|---|---|---|---|---|",
        ]
    )
    ledger_summaries: list[
        tuple[CandidateRecord, Path | None, list[ResultLedgerEntry]]
    ] = []
    for record in records:
        active = inputs.candidate_specs[record.candidate_id]
        score = ""
        passed = ""
        latest_iteration = record.iterations[-1] if record.iterations else None
        git_head = latest_iteration.git_head if latest_iteration else ""
        payload = inputs.history[record.candidate_id]
        key_metrics = ", ".join(
            f"{key}={value}" for key, value in payload["key_metrics"].items()
        )
        changed = ", ".join(record.detected_changed_files)
        agent_sessions = ", ".join(
            session["agent_session_id"] for session in payload["agent_sessions"]
        )
        best_iteration = best_iteration_record(record, active.spec.metric_direction)
        if best_iteration is not None:
            score = "" if best_iteration.score is None else str(best_iteration.score)
            passed = "True"
        elif record.score_report and record.score_report.process_passed and (
            record.score_report.frozen_spec_id or run.frozen_spec_id
        ) == active.frozen_spec_id:
            score = (
                ""
                if record.score_report.aggregate_score is None
                else str(record.score_report.aggregate_score)
            )
            passed = "True"
        best_iteration_value = (
            "" if best_iteration is None else str(best_iteration.iteration)
        )
        best_score_value = (
            ""
            if best_iteration is None or best_iteration.score is None
            else str(best_iteration.score)
        )
        best_git_head = "" if best_iteration is None else best_iteration.git_head or ""
        parent_base = ", ".join(
            part
            for part in [
                f"parent={record.task.parent_id}" if record.task.parent_id else "",
                f"base={record.task.base_candidate_id}"
                if record.task.base_candidate_id
                else "",
            ]
            if part
        )
        workspace_ledger_projection = inputs.ledger_projections[record.candidate_id]
        results_path = (
            (record.task.workspace / RESULTS_TSV_RELATIVE_PATH)
            if workspace_ledger_projection == WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV
            and record.task.workspace_provider != "thinkthread"
            else None
        )
        results_entries = inputs.ledgers[record.candidate_id]
        ledger_summaries.append((record, results_path, results_entries))
        if results_path is None:
            private_projection = (
                "; ThinkThread Child-private results.tsv projection"
                if workspace_ledger_projection
                == WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV
                and record.task.workspace_provider == "thinkthread"
                else ""
            )
            ledger_display = (
                f".gp canonical{private_projection} ({len(results_entries)} rows)"
            )
        else:
            try:
                ledger_link = results_path.relative_to(report_path.parent).as_posix()
            except ValueError:
                ledger_link = results_path.as_posix()
            ledger_display = (
                f"[results.tsv]({ledger_link}) ({len(results_entries)} rows)"
                if (results_path in inputs.existing_ledgers)
                else "missing results.tsv projection"
            )
        lines.append(
            f"| `{record.candidate_id}` | `{active.frozen_spec_id}` | "
            f"{markdown_cell(active.spec.metric_name)} ({active.spec.metric_direction}) | `{record.task.plan_id or ''}` | "
            f"{markdown_cell(agent_sessions)} | "
            f"{markdown_cell(parent_base)} | {record.status} | {score} | "
            f"{markdown_cell(git_head or '')} | "
            f"{best_iteration_value} | {best_score_value} | "
            f"{markdown_cell(best_git_head)} | {passed} | "
            f"{markdown_cell(payload.get('summary', ''))} | "
            f"{markdown_cell(key_metrics)} | {markdown_cell(changed)} | "
            f"{markdown_cell(ledger_display)} |"
        )
    if any(record.spec_revisions for record in records):
        lines.extend([
            "", "## Evaluation Contract Versions", "",
            "| Candidate | Iteration | Frozen Spec | Spec Hash |",
            "|---|---:|---|---|",
        ])
        for record in records:
            for iteration in record.iterations:
                lines.append(
                    f"| `{record.candidate_id}` | {iteration.iteration} | "
                    f"`{iteration.frozen_spec_id or run.frozen_spec_id}` | "
                    f"`{iteration.spec_hash or frozen.spec_hash}` |"
                )
    lines.extend(
        [
            "",
            "## Results Ledgers",
            "",
            "The .gp control plane owns the canonical verifier ledger. Some "
            "inner-agent adapters also receive a workspace projection.",
            "",
            "| Candidate | Ledger | Rows | Latest Commit | Latest Score | Latest Status | Latest Hypothesis |",
            "|---|---|---:|---|---:|---|---|",
        ]
    )
    for record, results_path, results_entries in ledger_summaries:
        latest = results_entries[-1] if results_entries else None
        if results_path is None:
            workspace_ledger_projection = inputs.ledger_projections[record.candidate_id]
            ledger_display = (
                ".gp canonical; ThinkThread Child-private results.tsv projection"
                if workspace_ledger_projection
                == WORKSPACE_LEDGER_PROJECTION_RESULTS_TSV
                and record.task.workspace_provider == "thinkthread"
                else ".gp canonical"
            )
        else:
            try:
                ledger_link = results_path.relative_to(report_path.parent).as_posix()
            except ValueError:
                ledger_link = results_path.as_posix()
            ledger_display = (
                f"[results.tsv]({ledger_link})"
                if (results_path in inputs.existing_ledgers)
                else "missing results.tsv projection"
            )
        lines.append(
            f"| `{record.candidate_id}` | {markdown_cell(ledger_display)} | "
            f"{len(results_entries)} | "
            f"{markdown_cell((latest.git_head or '') if latest else '')} | "
            f"{'' if latest is None or latest.score is None else latest.score} | "
            f"{markdown_cell(latest.status if latest else '')} | "
            f"{markdown_cell(latest.hypothesis if latest else '')} |"
        )
    lines.extend(
        [
            "",
            "## Toolization Reviews",
            "",
            "These worker decisions and runtime advisories are observational only; staging inventory and publication settlement remain authoritative.",
            "",
            "| Candidate | Iteration | Outcome | Signals | Exclusion | Tool Names | Rationale | Advisories | Staged Entries | Publish Status |",
            "|---|---:|---|---|---|---|---|---|---|---|",
        ]
    )
    for record in records:
        for iteration in record.iterations:
            decision = iteration.toolization_decision
            lines.append(
                f"| `{record.candidate_id}` | {iteration.iteration} | "
                f"{markdown_cell(decision.outcome if decision else '')} | "
                f"{markdown_cell(', '.join(decision.signals) if decision else '')} | "
                f"{markdown_cell(decision.exclusion or '' if decision else '')} | "
                f"{markdown_cell(', '.join(decision.tool_names) if decision else '')} | "
                f"{markdown_cell(decision.rationale if decision else '')} | "
                f"{markdown_cell(', '.join(iteration.toolization_advisories))} | "
                f"{markdown_cell(', '.join(iteration.shared_tool_staged_entries))} | "
                f"{markdown_cell(iteration.shared_tool_publish_status)} |"
            )
    agent_sessions = inputs.sessions
    if agent_sessions:
        session_rows = [
            (session, inputs.handles[session.agent_session_id])
            for session in agent_sessions
        ]
        include_handle = any(
            handle and handle != session.agent_session_id
            for session, handle in session_rows
        )
        lines.extend(
            [
                "",
                "## Agent Sessions",
                "",
            ]
        )
        if include_handle:
            lines.extend(
                [
                    "| Session | Harness | Runtime Provider | Handle | Candidate | Verifier Runs | Created | Updated |",
                    "|---|---|---|---|---|---:|---|---|",
                ]
            )
        else:
            lines.extend(
                [
                    "| Session | Harness | Runtime Provider | Candidate | Verifier Runs | Created | Updated |",
                    "|---|---|---|---|---:|---|---|",
                ]
            )
        for session, handle in session_rows:
            common = f"| `{session.agent_session_id}` | `{session.agent_harness}` | `{session.runtime_provider}` | "
            if include_handle:
                display_handle = handle if handle != session.agent_session_id else ""
                common += f"{markdown_cell(display_handle)} | "
            lines.append(
                common + f"`{session.candidate_id or ''}` | "
                f"{session.counters.get('verifier_runs', 0)} | "
                f"{session.created_at} | {session.updated_at} |"
            )
    lines.append("")

    return "\n".join(lines)


def render_artifact_lifecycle(final_run: RunRecord) -> str:
    lines = []
    lines.extend(
        [
            "## Provider Artifact Lifecycle",
            "",
            "Artifact identity and saved Evidence/Annotation results remain "
            "durable. Removed snapshot contents cannot be reopened as an "
            "Artifact View.",
            "",
            "| Artifact | Availability | Cleanup State | Pins | Purposes |",
            "|---|---|---|---|---|",
        ]
    )
    for resource in final_run.provider_cleanup:
        reference = ArtifactRef(
            kind=resource.resource_kind,
            id=resource.resource_id,
            provider=resource.provider,
        )
        lines.append(
            f"| `{resource.resource_id}` | "
            f"`{artifact_availability(reference, final_run.provider_cleanup)}` | "
            f"`{resource.state}` | "
            f"{markdown_cell(', '.join(resource.pins))} | "
            f"{markdown_cell(', '.join(resource.purposes))} |"
        )
    lines.append("")
    return "\n".join(lines)
