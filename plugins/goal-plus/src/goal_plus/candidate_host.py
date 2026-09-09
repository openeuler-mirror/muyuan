"""Read-only host assembly; no Search Runtime or model executor is constructed."""

from pathlib import Path

from goal_plus.application.candidate_queries import CandidateHostQueries
from goal_plus.application.search_context import SearchContext
from goal_plus.infrastructure.state.candidate_queries import FileCandidateQueries
from goal_plus.infrastructure.state.search_context import FileSearchContextSource


def candidate_host_queries(root_dir: Path | str) -> CandidateHostQueries:
    return CandidateHostQueries(
        FileCandidateQueries(root_dir),
        SearchContext(FileSearchContextSource(root_dir)),
    )
