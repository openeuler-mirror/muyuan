"""Terminal report output and subsequent provider cleanup in explicit order."""

from pathlib import Path
from goal_plus.application.artifact_lifecycle import ArtifactLifecycle
from goal_plus.ports.report import ReportQueries, ReportOutput


class Report:
    def __init__(
        self, queries: ReportQueries, output: ReportOutput, lifecycle: ArtifactLifecycle
    ) -> None:
        self.queries, self.output, self.lifecycle = queries, output, lifecycle

    def blocking_goals(self, run_id: str) -> list[tuple[str, str]]:
        return [
            (goal.goal_plus_id, goal.status)
            for goal in self.queries.linked_goals(run_id)
            if goal.status not in {"complete", "abandoned"}
            and goal.control_state != "closed"
            and not (goal.control_state is None and goal.status == "blocked")
        ]

    def report(self, run_id: str) -> Path:
        blocking = self.blocking_goals(run_id)
        if blocking:
            details = ", ".join(f"{identity}={status}" for identity, status in blocking)
            raise RuntimeError(
                "cannot generate a report before every linked Goal Plus record "
                "reaches a terminal status (complete or abandoned); "
                f"current: {details}"
            )
        inputs = self.queries.markdown_inputs(run_id)
        path = self.output.write_markdown(inputs)
        self.output.write_html(run_id)
        cleanup = self.lifecycle.cleanup(run_id)
        if cleanup["state"] != "not_applicable":
            self.output.write_markdown(inputs, self.queries.read_run(run_id))
            self.output.write_html(run_id)
        return path

    def refresh_existing(self, run_id: str) -> None:
        if self.queries.markdown_exists(run_id) and not self.blocking_goals(run_id):
            self.report(run_id)
