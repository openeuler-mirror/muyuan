"""Public report adapters: gather inputs and supply packaged assets to pure renderers."""

from __future__ import annotations
from functools import lru_cache
from pathlib import Path
from typing import Any
from goal_plus._version import __version__
from goal_plus.infrastructure.report_inputs import (
    build_html_report_data as build_html_report_data,
)
from goal_plus.projections import report_html
from goal_plus.projections.report_html import (
    REPORT_SCHEMA_VERSION as REPORT_SCHEMA_VERSION,
    REPORT_DOCUMENT_SCHEMA_VERSION as REPORT_DOCUMENT_SCHEMA_VERSION,
    require_terminal_goal_plus_report,
)


@lru_cache(maxsize=1)
def _load_plotly_javascript() -> str | None:
    try:
        from plotly.offline import get_plotlyjs
    except ImportError:
        return None
    return str(get_plotlyjs())


def render_html_report(data: dict[str, Any]) -> str:
    return report_html.render_html_report(
        data, plotly_javascript=_load_plotly_javascript()
    )


def render_codex_observability_report(observation: dict[str, Any]) -> str:
    return report_html.render_codex_observability_report(
        observation, plotly_javascript=_load_plotly_javascript()
    )


def render_report_document(document: dict[str, Any]) -> str:
    return report_html.render_report_document(
        document, plotly_javascript=_load_plotly_javascript()
    )


def write_html_report(
    root_dir: Path | str,
    run_id: str,
    output_path: Path | None = None,
) -> Path:
    root = Path(root_dir).resolve()
    destination = output_path or root / "runs" / run_id / "report.html"
    data = build_html_report_data(root, run_id)
    require_terminal_goal_plus_report(data)
    destination.write_text(
        render_report_document(
            {
                "schema_version": REPORT_DOCUMENT_SCHEMA_VERSION,
                "goal_plus_version": __version__,
                "report_kind": "goal-plus",
                "data": data,
            }
        ),
        encoding="utf-8",
    )
    return destination
