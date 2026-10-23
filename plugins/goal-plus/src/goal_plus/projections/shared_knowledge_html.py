"""Unified entry-card rendering for Evidence and Knowledge items.

Both kinds of entries — settled Evidence iterations and Knowledge-board
records — render as the same card: a signature header (type badge, source
label, writer, evidence_ref) followed by a uniform key→value body.
"""

from __future__ import annotations

import html
from typing import Any

from goal_plus.domain.shared_knowledge import SharedKnowledgeView

_TYPE_BADGES = {
    "fact": ("fact", "#0f766e", "#ccfbf1"),
    "think": ("think", "#b45309", "#fef3c7"),
    "subject": ("subject", "#1d4ed8", "#dbeafe"),
    "question": ("question", "#b91c1c", "#fee2e2"),
}

_STYLE = """.sk-entry-list { display: flex; flex-direction: column; gap: 0.4rem; }
.sk-entry { border: 1px solid #e5e7eb; border-radius: 6px; padding: 0.5rem 0.75rem;
            background: #ffffff; font-size: 0.85rem; }
.sk-entry dl { display: grid; grid-template-columns: max-content 1fr;
               gap: 0.15rem 0.6rem; margin: 0.3rem 0 0; font-size: 0.8rem; }
.sk-entry dt { color: #374151; font-family: ui-monospace, monospace; }
.sk-entry dd { margin: 0; overflow-wrap: anywhere; }
.sk-entry .sk-content { margin: 0.15rem 0 0 0.6rem; padding-left: 0.5rem;
                      border-left: 2px solid #e5e7eb; }
.sk-badge { display: inline-block; border-radius: 999px; padding: 0 0.5rem;
            font-size: 0.72rem; font-weight: 600; }
.sk-meta { color: #6b7280; font-size: 0.78rem; }
.sk-warn { color: #b45309; font-weight: 600; font-size: 0.78rem; margin-top: 0.2rem; }
.sk-outcome { color: #4b5563; font-size: 0.78rem; margin-top: 0.15rem; }
"""


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _badge(entry_type: str | None) -> str:
    label, foreground, background = _TYPE_BADGES.get(
        entry_type or "", ("entry", "#374151", "#f3f4f6")
    )
    return (
        f'<span class="sk-badge" style="color:{foreground};'
        f'background:{background}">{_escape(label)}</span>'
    )



def _get(entry: Any, key: str) -> Any:
    """Read a field from either a model object or a dict."""
    if isinstance(entry, dict):
        return entry.get(key)
    return getattr(entry, key, None)


def _kv(pairs: list[tuple[str, str]]) -> str:
    if not pairs:
        return ""
    return "<dl>" + "".join(
        f"<dt>{_escape(k)}</dt><dd>{_escape(v)}</dd>" for k, v in pairs
    ) + "</dl>"


def _layered(
    top: list[tuple[str, str]],
    content: list[tuple[str, str]],
) -> str:
    """Render layered key→value: top-level keys plus a nested content dict."""
    if not top and not content:
        return ""
    parts: list[str] = []
    for k, v in top:
        parts.append(f"<dt>{_escape(k)}</dt><dd>{_escape(v)}</dd>")
    if content:
        inner = "".join(
            f"<dt>{_escape(k)}</dt><dd>{_escape(v)}</dd>" for k, v in content
        )
        parts.append(
            "<dt>content</dt><dd><dl class=\"sk-content\">"
            + inner
            + "</dl></dd>"
        )
    return "<dl>" + "".join(parts) + "</dl>"


def evidence_card(
    *,
    candidate_id: str,
    iteration: dict[str, Any],
    selected: bool = False,
) -> str:
    """Render one settled Evidence iteration as a unified layered card."""
    content: list[tuple[str, str]] = []
    for key in ("score", "disposition", "hypothesis"):
        value = iteration.get(key)
        if value is not None:
            content.append((key, str(value)))
    commit = iteration.get("git_head") or iteration.get("commit")
    if commit:
        content.append(("commit", str(commit)[:12]))
    changed = iteration.get("changed_files")
    if changed:
        content.append(("changed_files", str(changed)))
    top: list[tuple[str, str]] = [
        ("subject", f"{candidate_id}:iter:{iteration.get('iteration', '?')}"),
        ("record_id", str(iteration.get("settlement_id") or iteration.get("git_head") or "")),
    ]
    view = iteration.get("view")
    if view:
        top.append(("view", str(view)))
    ev_rid = str(iteration.get("settlement_id") or iteration.get("git_head") or "")
    ev_short = ev_rid[:8] if len(ev_rid) >= 8 else ev_rid
    header = (
        f'{_badge("fact")} '
        f"<span class=\"sk-meta\">{_escape(candidate_id)}"
        + (" ★" if selected else "")
        + f" · iter {_escape(iteration.get('iteration', '?'))}"
        + (
            f" · <code>{_escape(ev_short)}</code>"
            if ev_short
            else ""
        )
        + "</span>"
    )
    return f'<div class="sk-entry">{header}{_layered(top, content)}</div>'


def knowledge_card(
    entry: Any,
    outcome: dict[str, Any] | None = None,
    *,
    contradiction: bool = False,
) -> str:
    """Render one Knowledge-board entry as a unified layered card."""
    raw_content = _get(entry, "content") or {}
    if isinstance(raw_content, dict):
        all_pairs = list(raw_content.items())
    else:
        all_pairs = [("raw", str(raw_content))]
    top: list[tuple[str, str]] = []
    content: list[tuple[str, str]] = []
    for key, value in all_pairs:
        if key in ("type", "subject"):
            continue  # type in badge, subject goes to top
        if key in ("confidence", "limits"):
            top.append((key, value))
        else:
            content.append((key, value))
    if not any(k == "subject" for k, _ in top):
        subject = next((v for k, v in all_pairs if k == "subject"), "")
        if subject:
            top.insert(0, ("subject", subject))
    if not any(k == "section" for k, _ in top):
        sk = _get(entry, "section_key") or ""
        top.append(("section", str(sk)))
    if not any(k == "record_id" for k, _ in top):
        rid = _get(entry, "record_id") or ""
        if rid:
            top.append(("record_id", str(rid)))
    if not any(k == "content_hash" for k, _ in top):
        ch = _get(entry, "content_hash") or ""
        if ch:
            top.append(("content_hash", str(ch)[:16]))
    entry_type = _get(entry, "entry_type")
    section_key = _get(entry, "section_key")
    writer = _get(entry, "writer")
    evidence_ref = _get(entry, "evidence_ref")
    rid_short = ""
    rid_full = str(_get(entry, "record_id") or "")
    if rid_full:
        # 取最后一段作为短标识 (run_0001:xxx-1 → xxx-1)
        rid_short = rid_full.rsplit(":", 1)[-1] if ":" in rid_full else rid_full
    header = (
        f"{_badge(entry_type)} "
        f'<span class="sk-meta">{_escape(writer)}'
        + (
            f" · {_escape(evidence_ref)}"
            if evidence_ref
            else ""
        )
        + (
            f" · <code>{_escape(rid_short)}</code>"
            if rid_short
            else ""
        )
        + "</span>"
    )
    body = _layered(top, content)
    outcome_html = ""
    if outcome:
        parts = []
        if outcome.get("disposition"):
            parts.append(f"disposition={_escape(outcome['disposition'])}")
        if outcome.get("score") is not None:
            parts.append(f"score={_escape(outcome['score'])}")
        if parts:
            outcome_html = (
                f'<div class="sk-outcome">{" · ".join(parts)}</div>'
            )
    warn = (
        '<div class="sk-warn">&#9888; 同主键存在多条未合并条目</div>'
        if contradiction
        else ""
    )
    return f'<div class="sk-entry">{header}{body}{outcome_html}{warn}</div>'


def render_unified_entries(
    *,
    evidence_items: list[dict[str, Any]],
    knowledge_view: SharedKnowledgeView | None = None,
    outcome_by_record: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Render Evidence and Knowledge entries interleaved in one uniform list."""
    outcomes = outcome_by_record or {}
    cards: list[tuple[str, str]] = []

    for item in evidence_items:
        created = str(item.get("created_at") or "")
        cid = str(item.get("candidate_id") or "")
        it = item.get("iteration") or {}
        cards.append((
            created,
            evidence_card(
                candidate_id=cid,
                iteration=it,
                selected=bool(item.get("candidate_selected")),
            ),
        ))

    if knowledge_view is not None:
        section_counts: dict[str, int] = {}
        for entry in knowledge_view.entries:
            key = f"{entry.section_key}:{entry.subject}"
            section_counts[key] = section_counts.get(key, 0) + 1
        for entry in knowledge_view.entries:
            key = f"{entry.section_key}:{entry.subject}"
            cards.append((
                "",
                knowledge_card(
                    entry,
                    outcomes.get(entry.record_id),
                    contradiction=section_counts.get(key, 0) > 1,
                ),
            ))

    if not cards:
        return '<p class="sk-meta">No shared entries.</p>'
    cards.sort(key=lambda pair: pair[0])
    return (
        f'<style>{_STYLE}</style>'
        f'<div class="sk-entry-list">{"".join(c for _, c in cards)}</div>'
    )


__all__ = ["evidence_card", "knowledge_card", "render_unified_entries"]
