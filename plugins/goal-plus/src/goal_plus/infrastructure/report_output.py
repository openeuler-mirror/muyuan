"""Report filesystem reads and writes, with packaged renderer assets."""

from pathlib import Path

from goal_plus.application.candidate_ledger import CandidateLedger
from goal_plus.application.search_research import SearchResearch
from goal_plus.domain.results_ledger import RESULTS_TSV_RELATIVE_PATH
from goal_plus.infrastructure.state.search_run import FileSearchRunState
from goal_plus.infrastructure.state.files import load_json
from goal_plus.infrastructure.state.results_ledger import read_candidate_results_ledger
from goal_plus.ports.report import MarkdownReportInputs, LinkedReportGoal
from goal_plus.ports.state import RunRecord
from goal_plus.projections.report_markdown import (
    render_markdown_report,
    render_artifact_lifecycle,
)


class FileReportQueries(FileSearchRunState):
    def __init__(
        self, root_dir: Path, research: SearchResearch, ledger: CandidateLedger
    ):
        super().__init__(root_dir)
        self.research, self.ledger = research, ledger

    def linked_goals(self, run_id: str):
        goals = []
        for path in sorted((self.root_dir / "goal-plus").glob("*/goal.json")):
            try:
                payload = load_json(path)
            except (OSError, ValueError):
                continue
            tasks = payload.get("search_tasks")
            if not isinstance(tasks, list) or not any(
                isinstance(task, dict) and task.get("run_id") == run_id
                for task in tasks
            ):
                continue
            control = payload.get("control")
            goals.append(
                LinkedReportGoal(
                    str(payload.get("goal_plus_id") or path.parent.name),
                    str(payload.get("status") or "active"),
                    control.get("state") if isinstance(control, dict) else None,
                )
            )
        return goals

    def markdown_exists(self, run_id: str) -> bool:
        return (self.run_dir(run_id) / "report.md").exists()

    def markdown_inputs(self, run_id: str) -> MarkdownReportInputs:
        run = self.read_run(run_id)
        frozen = self.read_frozen_spec(run.frozen_spec_id)
        records = self.read_candidates(run_id)
        sessions = self.read_agent_sessions(run_id)
        return MarkdownReportInputs(
            run=run,
            frozen=frozen,
            candidates=records,
            plans=self.read_plans(run_id),
            sessions=sessions,
            path=self.run_dir(run_id) / "report.md",
            history={
                r.candidate_id: self.research.history_candidate_payload(r, frozen.spec)
                for r in records
            },
            ledger_projections={
                r.candidate_id: self.ledger.projection(frozen, r) for r in records
            },
            ledgers={r.candidate_id: read_candidate_results_ledger(r) for r in records},
            existing_ledgers={
                r.task.workspace / RESULTS_TSV_RELATIVE_PATH
                for r in records
                if r.task.workspace_provider != "thinkthread"
                and (r.task.workspace / RESULTS_TSV_RELATIVE_PATH).is_file()
            },
            handles={
                s.agent_session_id: self.research.display_session_handle(s)
                for s in sessions
            },
        )


class FileReportOutput:
    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir

    def write_markdown(
        self, inputs: MarkdownReportInputs, lifecycle: RunRecord | None = None
    ) -> Path:
        text = render_markdown_report(inputs)
        if lifecycle is not None:
            text += "\n" + render_artifact_lifecycle(lifecycle)
        inputs.path.write_text(text, encoding="utf-8")
        return inputs.path

    def write_html(self, run_id: str) -> None:
        from goal_plus.reporting import write_html_report

        write_html_report(self.root_dir, run_id)
