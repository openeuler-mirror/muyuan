"""Pure renderers turning collected agent-transcript events into reviewable text/HTML.

The event model and renderers are deliberately content-complete: this projection
is only fed from explicitly opt-in debug exports that re-read local native
session histories. Missing facts stay empty; nothing is inferred here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import html
import json
from typing import Any

# Kinds are stable renderer vocabulary; collectors map host records onto them.
EVENT_KINDS = (
    "session", "user", "assistant", "thinking", "tool_call", "tool_result",
    "injection", "compaction", "context", "error", "meta",
)
# Kinds whose content rides the provider request; the rest are host-side
# session records that only cost disk, never context tokens.
CONTEXT_BEARING_KINDS = frozenset(
    {"user", "assistant", "thinking", "tool_call", "tool_result", "injection"}
)
_HTML_DEFAULT_MAX_CHARS = 20000
_DEFAULT_GAP_SECONDS = 120.0


@dataclass(frozen=True)
class TranscriptEvent:
    at: str = ""
    kind: str = "meta"
    label: str = ""
    content: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentTranscriptSession:
    session_id: str
    harness: str
    role: str
    source_path: str = ""
    identity: dict[str, Any] = field(default_factory=dict)
    events: list[TranscriptEvent] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    model: str = ""    # e.g. "glm-5.3-flash (zai)" from the last observed model_change
    thinking: str = "" # pi thinkingLevel / codex reasoning effort, last observed


def _epoch(at: str) -> float | None:
    if not isinstance(at, str) or not at:
        return None
    try:
        parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _clock(at: str, *, with_date: bool = False) -> str:
    epoch = _epoch(at)
    if epoch is None:
        return at or "--:--:--"
    moment = datetime.fromtimestamp(epoch, timezone.utc)
    return moment.strftime("%m-%d %H:%M:%S" if with_date else "%H:%M:%S")


def _duration_text(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    if seconds >= 60:
        return f"{seconds / 60:.1f}m"
    return f"{seconds:.1f}s"


def _session_span(events: list[TranscriptEvent]) -> tuple[str, str, float | None]:
    epochs = [value for value in (_epoch(event.at) for event in events) if value is not None]
    if not epochs:
        first = events[0].at if events else ""
        return first, (events[-1].at if events else ""), None
    start, end = datetime.fromtimestamp(epochs[0], timezone.utc), datetime.fromtimestamp(epochs[-1], timezone.utc)
    return start.isoformat(), end.isoformat(), epochs[-1] - epochs[0]


def _count_by_kind(events: list[TranscriptEvent]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        counts[event.kind] = counts.get(event.kind, 0) + 1
    return counts


def _estimate_tokens(text: str) -> int:
    """Same heuristic the Pi extension uses: CJK about one token per character,
    everything else about four characters per token. An estimate, not billing."""
    cjk = sum(1 for ch in text if "\u3000" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef")
    return cjk + (len(text) - cjk + 3) // 4


@dataclass(frozen=True)
class _FootprintRow:
    kind: str
    label: str
    count: int
    chars: int
    tokens: int


def _content_footprint(events: list[TranscriptEvent]) -> dict[str, Any]:
    """Group event content volume by kind+label to expose trim candidates."""
    groups: dict[tuple[str, str], list[int]] = {}
    for event in events:
        chars = len(event.content)
        row = groups.setdefault((event.kind, event.label or "-"), [0, 0, 0])
        row[0] += 1
        row[1] += chars
        row[2] += _estimate_tokens(event.content)
    rows = [_FootprintRow(kind, label, *values) for (kind, label), values in groups.items()]
    totals = {
        bucket: {"chars": sum(row.chars for row in rows if in_bucket(row.kind)),
                 "tokens": sum(row.tokens for row in rows if in_bucket(row.kind)),
                 "count": sum(row.count for row in rows if in_bucket(row.kind))}
        for bucket, in_bucket in (
            ("context", lambda kind: kind in CONTEXT_BEARING_KINDS),
            ("records", lambda kind: kind not in CONTEXT_BEARING_KINDS),
        )
    }
    return {"totals": totals,
            "context": sorted((row for row in rows if row.kind in CONTEXT_BEARING_KINDS),
                              key=lambda row: -row.chars),
            "records": sorted((row for row in rows if row.kind not in CONTEXT_BEARING_KINDS),
                              key=lambda row: -row.chars)}


def _usage_note(meta: dict[str, Any], content: str = "") -> str:
    """One provider call's cost, spelled out: context read, generation, total.

    All figures are tokens; the context figure is the input side of that call
    (including cache hits), i.e. the context size the model saw at that moment.
    """
    usage = meta.get("usage")
    note = ""
    if isinstance(usage, dict):
        parts = []
        context = usage.get("input")
        spent = None
        if type(context) is int and context >= 0:
            spent = context
            for key in ("cacheRead", "cacheWrite"):
                value = usage.get(key, 0)
                if type(value) is int and value >= 0:
                    spent += value
            parts.append(f"context {spent:,} tok")
        output = usage.get("output")
        if type(output) is int and output >= 0:
            parts.append(f"output {output:,} tok")
            if spent is not None:
                parts.append(f"total {spent + output:,} tok")
        model = meta.get("model")
        if isinstance(model, str) and model:
            parts.append(model)
        note = " · ".join(parts)
    if content:
        note = (note + " · " if note else "") + f"{len(content):,} chars"
    return note


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _gap_markers(
    events: list[TranscriptEvent], gap_seconds: float,
) -> dict[int, float]:
    """Return {event_index: seconds_since_previous_event} for gaps above the threshold."""
    markers: dict[int, float] = {}
    previous: float | None = None
    for index, event in enumerate(events):
        epoch = _epoch(event.at)
        if epoch is None:
            continue
        if previous is not None and gap_seconds > 0 and epoch - previous >= gap_seconds:
            markers[index] = epoch - previous
        previous = epoch
    return markers


def _identity_line(identity: dict[str, Any]) -> str:
    parts = [f"{key}={value}" for key, value in identity.items() if value not in (None, "")]
    return " ".join(parts)


def _display_name(session: AgentTranscriptSession) -> str:
    """Role plus the most distinguishing identity token, e.g. ``candidate c001``."""
    identity = session.identity or {}
    if session.role in {"verifier", "annotator"}:
        return (f"{session.role} {identity.get('candidate_id', '?')}"
                f" / iteration {identity.get('iteration', '?')}"
                f" / attempt {identity.get('attempt', '?')}")
    for key in ("candidate_id", "goal_plus_id", "thinkthread_id", "agent_session_id"):
        value = identity.get(key)
        if isinstance(value, str) and value:
            return f"{session.role} {value}"
    return session.role


def _is_prompt_event(event: TranscriptEvent) -> bool:
    """A real prompt entering the session marks a dispatch/continuation boundary.

    Codex developer/system context messages share the ``user`` kind but are
    static context, not a new dispatch.
    """
    return event.kind == "user" and event.label == "prompt"


def _footprint_text_lines(events: list[TranscriptEvent], *, top: int = 4) -> list[str]:
    footprint = _content_footprint(events)
    context, records = footprint["totals"]["context"], footprint["totals"]["records"]
    lines = [
        f"  footprint: model-context {context['chars']:,} chars / ~{context['tokens']:,} tok"
        f" ({context['count']} events) · host-records {records['chars']:,} chars /"
        f" ~{records['tokens']:,} tok (disk only, {records['count']} events)",
    ]
    for bucket, title in (("context", "top context"), ("records", "top records")):
        for row in footprint[bucket][:top]:
            lines.append(f"    {title:<12} {row.label or row.kind} ×{row.count}"
                         f" = {row.chars:,} chars ~{row.tokens:,} tok [{row.kind}]")
    return lines



_KIND_COLORS = {
    "session": "#111827", "user": "#1d4ed8", "assistant": "#15803d", "thinking": "#6b7280",
    "tool_call": "#b45309", "tool_result": "#0f766e", "injection": "#dc2626",
    "compaction": "#7c3aed", "context": "#92400e", "error": "#b91c1c", "meta": "#4b5563",
}

_REPORT_CSS = """
:root { color-scheme: light dark; }
body { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
       font-size: 12.5px; margin: 0; padding: 16px; background: #f8fafc; color: #111827; }
nav { position: sticky; top: 0; background: #f8fafc; padding: 8px 0; border-bottom: 2px solid #cbd5e1;
      display: flex; flex-wrap: wrap; gap: 8px; z-index: 2; max-height: 6rem; overflow: auto; }
nav a { color: #1d4ed8; text-decoration: none; background: #e0e7ff; border-radius: 4px; padding: 2px 8px; }
nav a:hover { text-decoration: underline; }
.navgroup { color: #475569; background: #e2e8f0; border-radius: 4px; padding: 2px 8px; font-size: 11px; }
body { overflow-wrap: anywhere; }
section, .transcript-index { scroll-margin-top: 8rem; }
.transcript-index { padding: 14px 0; border-bottom: 1px solid #cbd5e1; }
.transcript-index h2 { margin: 0 0 8px; font-size: 14px; }
.transcript-index a { color: #1d4ed8; }
.worker-links, .attempt-links { display: flex; flex-wrap: wrap; gap: 6px 14px; }
.inference-index { margin: 10px 0 0; }
.inference-index > summary { color: inherit; font-size: 12px; }
.index-filters { display: flex; flex-wrap: wrap; gap: 12px; margin: 10px 0; }
.index-filters[hidden], .index-list tr[hidden] { display: none; }
.index-filters label { display: flex; align-items: center; gap: 6px; }
.index-filters input { width: 7rem; }
.index-filters input, .index-filters select { font: inherit; padding: 4px; }
.index-list { max-height: min(22rem, 50vh); overflow: auto; margin-top: 8px; }
.index-list table { width: 100%; table-layout: fixed; border-collapse: collapse; }
.index-list th, .index-list td { text-align: left; vertical-align: top; padding: 8px;
                               border-bottom: 1px solid #cbd5e1; }
.index-list small { display: block; margin-top: 4px; color: #64748b; }
.index-list th:first-child { width: 26%; }
.index-list th:last-child { width: 36%; }
section { background: white; border: 1px solid #cbd5e1; border-radius: 8px;
          margin: 18px 0; overflow: hidden; }
header { background: #1e293b; color: #e2e8f0; padding: 10px 14px; }
header h2 { margin: 0; font-size: 14px; }
header .facts { margin-top: 6px; display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 11.5px; color: #cbd5e1; }
table.summary { width: 100%; table-layout: fixed; border-collapse: collapse; font-size: 11.5px; }
table.summary td { padding: 3px 10px; border-top: 1px solid #e2e8f0; vertical-align: top; }
table.summary td:first-child { color: #64748b; white-space: nowrap; width: 90px; }
ol.events { list-style: none; margin: 0; padding: 6px 0; }
ol.events li { padding: 2px 14px; border-left: 4px solid transparent; }
ol.events li:hover { background: #f1f5f9; }
.k { font-weight: 700; }
.time { color: #94a3b8; margin-right: 8px; }
.seq { color: #cbd5e1; margin-right: 6px; }
.label { color: #0f172a; }
.note { color: #64748b; font-style: italic; }
details { margin: 2px 0 2px 84px; }
details summary { cursor: pointer; color: #64748b; font-size: 11px; }
pre { margin: 4px 0; padding: 8px; background: #f1f5f9; border-radius: 6px;
      white-space: pre-wrap; word-break: break-word; max-height: 420px; overflow: auto; }
td pre { margin: 2px 0 6px; max-height: none; }
.gap { text-align: center; color: #b45309; background: #fffbeb; font-weight: 700;
       padding: 3px; margin: 4px 0; border-left: none !important; }
.turn { text-align: center; color: #1d4ed8; background: #eff6ff; font-weight: 700;
        padding: 3px; margin: 4px 0; border-left: none !important; }
.warning { color: #b45309; padding: 2px 14px; }
footer { color: #94a3b8; margin: 24px 0 8px; font-size: 11.5px; }
@media (prefers-color-scheme: dark) {
  body { background: #0f172a; color: #e2e8f0; }
  nav, section { background: #1e293b; }
  ol.events li:hover { background: #334155; }
  .label { color: #f1f5f9; }
  pre { background: #0f172a; }
  .gap { background: #422006; }
  .turn { background: #172554; }
  .transcript-index a { color: #93c5fd; }
  .index-list small { color: #94a3b8; }
}
"""


_INDEX_SCRIPT = """
document.querySelectorAll('.inference-index').forEach(index => {
  const filters = index.querySelector('.index-filters');
  const iteration = filters.querySelector('input');
  const role = filters.querySelector('select');
  const rows = Array.from(index.querySelectorAll('tbody tr'));
  const update = () => {
    let visible = 0;
    rows.forEach(row => {
      row.hidden = (iteration.value !== '' && row.dataset.iteration !== iteration.value)
        || (role.value !== '' && row.dataset.role !== role.value);
      if (!row.hidden) visible++;
    });
    index.querySelector('.index-empty').hidden = visible !== 0;
  };
  iteration.addEventListener('input', update);
  role.addEventListener('change', update);
  filters.hidden = false;
});
"""


def _candidate_key(session: AgentTranscriptSession) -> tuple[str, str] | None:
    run, candidate = (session.identity.get(key) for key in ("run_id", "candidate_id"))
    if isinstance(run, str) and run and isinstance(candidate, str) and candidate:
        return run, candidate
    return None


def _complete_inference_identity(session: AgentTranscriptSession) -> bool:
    identity = session.identity
    return (all(isinstance(identity.get(key), str) and identity[key]
                for key in ("settlement_id", "task_id"))
            and all(type(identity.get(key)) is int and identity[key] > 0
                    for key in ("iteration", "attempt")))


def _html_inference_index(records: list[tuple[int, AgentTranscriptSession]], *, unlinked: bool = False) -> str:
    counts = {role: sum(session.role == role for _, session in records)
              for role in ("verifier", "annotator")}
    parts = ['<details class="inference-index"><summary>Inferences: '
             f'verifier {counts["verifier"]}, annotator {counts["annotator"]}</summary>',
             '<div class="index-filters" hidden>'
             '<label>Iteration <input type="number" min="1" step="1" placeholder="All"></label>'
             '<label>Role <select aria-label="Role"><option value="">All</option>'
             '<option value="verifier">Verifier</option><option value="annotator">Annotator</option>'
             '</select></label></div>',
             '<div class="index-list" tabindex="0" role="region" aria-label="Inference records">'
             '<table><thead><tr><th scope="col">Iteration / settlement</th>'
             '<th scope="col">Role / task</th><th scope="col">Calls</th></tr></thead><tbody>']
    groups: dict[tuple, list[tuple[int, AgentTranscriptSession]]] = {}
    for position, session in records:
        identity = session.identity
        # Incomplete identities must not accidentally merge unrelated invocations.
        key = ((position,) if unlinked else (
            identity["iteration"], identity["settlement_id"], session.role, identity["task_id"]))
        groups.setdefault(key, []).append((position, session))
    ordered = sorted(groups.values(), key=lambda group: (
        group[0][1].identity.get("iteration") if type(group[0][1].identity.get("iteration")) is int else 0,
        str(group[0][1].identity.get("settlement_id") or ""), group[0][1].role,
        str(group[0][1].identity.get("task_id") or ""), group[0][0],
    ))
    for group in ordered:
        session = group[0][1]
        identity = session.identity
        iteration = html.escape(str(identity.get("iteration", "unavailable")))
        settlement = html.escape(str(identity.get("settlement_id") or "unavailable"))
        task = html.escape(str(identity.get("task_id") or "unavailable"))
        target = (f'<small>{html.escape(str(identity.get("run_id") or "unavailable"))} / '
                  f'{html.escape(str(identity.get("candidate_id") or "unavailable"))}</small>') if unlinked else ""
        parts.append(f'<tr data-iteration="{iteration}" data-role="{html.escape(session.role)}">'
                     f'<td>iteration {iteration}<small>{settlement}</small>{target}</td>'
                     f'<td>{html.escape(session.role)}<small>{task}</small></td><td><div class="attempt-links">')
        for position, call in sorted(group, key=lambda item: (
            item[1].identity.get("attempt") if type(item[1].identity.get("attempt")) is int else 0, item[0],
        )):
            attempt = call.identity.get("attempt", "unavailable")
            duplicate = sum(item.identity.get("attempt") == attempt for _, item in group) > 1
            label = f"attempt {attempt} / {call.harness}"
            if duplicate or unlinked:
                label += f" / {call.session_id}"
            parts.append(f'<a href="#s{position}" title="{html.escape(call.session_id)}">{html.escape(label)}</a>')
        parts.append('</div></td></tr>')
    parts.append('</tbody></table></div><p class="index-empty" hidden>No matching calls.</p></details>')
    return "".join(parts)


def _html_navigation(sessions: list[AgentTranscriptSession]) -> tuple[str, dict[int, str]]:
    candidates: dict[tuple[str, str], list[tuple[int, AgentTranscriptSession]]] = {}
    for position, session in enumerate(sessions, 1):
        if session.role == "candidate" and (key := _candidate_key(session)) is not None:
            candidates.setdefault(key, []).append((position, session))
    inferences: dict[tuple[str, str], list[tuple[int, AgentTranscriptSession]]] = {}
    unlinked: list[tuple[int, AgentTranscriptSession]] = []
    navigation: dict[str, list[str]] = {}
    indexes: list[str] = []
    backlinks: dict[int, str] = {}
    for position, session in enumerate(sessions, 1):
        key = _candidate_key(session)
        if session.role in {"verifier", "annotator"}:
            if key in candidates and _complete_inference_identity(session):
                inferences.setdefault(key, []).append((position, session))
            else:
                unlinked.append((position, session))
            continue
        run = session.identity.get("run_id")
        run = run if isinstance(run, str) else ""
        if session.role == "candidate" and key in candidates:
            if position != candidates[key][0][0]:
                continue
            link = f'<a href="#candidate-{position}">candidate {html.escape(key[1])}</a>'
        else:
            link = f'<a href="#s{position}">{html.escape(_display_name(session) + "/" + session.harness)}</a>'
        navigation.setdefault(run, []).append(link)
    parts = ['<nav aria-label="Transcript navigation">'] if sessions else []
    for run in sorted(navigation, key=lambda value: (bool(value), value)):
        if run:
            parts.append(f'<span class="navgroup">{html.escape(run)}</span>')
        parts.extend(navigation[run])
    for (run, candidate), workers in candidates.items():
        index_id = f"candidate-{workers[0][0]}"
        indexes.append(f'<article class="transcript-index" id="{index_id}">'
                       f'<h2>{html.escape(run)} / candidate {html.escape(candidate)}</h2>'
                       '<div class="worker-links">')
        for position, session in workers:
            backlinks[position] = index_id
            indexes.append(f'<a href="#s{position}">{html.escape(session.session_id + "/" + session.harness)}</a>')
        indexes.append('</div>')
        records = inferences.get((run, candidate), [])
        if records:
            indexes.append(_html_inference_index(records))
            backlinks.update({position: index_id for position, _ in records})
        indexes.append('</article>')
    if unlinked:
        parts.append(f'<a href="#unlinked-inferences">Unlinked inferences ({len(unlinked)})</a>')
        indexes.append('<article class="transcript-index" id="unlinked-inferences">'
                       '<h2>Unlinked inferences</h2>')
        indexes.append(_html_inference_index(unlinked, unlinked=True))
        indexes.append('</article>')
        backlinks.update({position: "unlinked-inferences" for position, _ in unlinked})
    if sessions:
        parts.append('</nav>')
    return "".join(parts + indexes), backlinks


def _is_raw_message_stream(event: TranscriptEvent) -> bool:
    return event.kind == "meta" and event.label in {
        "host event: message_start", "host event: message_update",
    }


def _html_event_rows(
    events: list[TranscriptEvent], *, max_chars: int, gap_seconds: float,
    raw_stream: bool = False,
) -> str:
    rows: list[str] = []
    markers = _gap_markers(events, gap_seconds)
    prompt_count = 0
    for index, event in enumerate(events):
        # Keep original sequence numbers so both views still match events.jsonl.
        if _is_raw_message_stream(event) != raw_stream:
            continue
        if index in markers:
            rows.append(f'<li class="gap">⏸ gap {_duration_text(markers[index])}</li>')
        if _is_prompt_event(event):
            prompt_count += 1
            boundary = "launch" if prompt_count == 1 else "dispatch/continuation"
            rows.append(f'<li class="turn">▶ prompt #{prompt_count} · {boundary}</li>')
        color = _KIND_COLORS.get(event.kind, "#4b5563")
        note = _usage_note(event.meta, event.content)
        if event.kind == "tool_result" and event.meta.get("is_error") is True:
            note = " ".join(part for part in ("ERROR", note) if part)
        label = html.escape(event.label)
        if note:
            label += f' <span class="note">{html.escape(note)}</span>'
        row = (f'<li style="border-left-color:{color}">'
               f'<span class="seq">{index + 1}</span>'
               f'<span class="time">{html.escape(_clock(event.at))}</span>'
               f'<span class="k" style="color:{color}">{html.escape(event.kind)}</span>'
               f' <span class="label">{label}</span>')
        if event.content:
            body, truncated = _truncate(event.content, max_chars)
            suffix = (f"\n…(+{len(event.content) - len(body)} chars truncated, total {len(event.content)})"
                      if truncated else "")
            # Observation bookkeeping (meta/context) stays collapsed; only the
            # conversation itself opens by default.
            open_attr = "" if event.kind in ("meta", "context") else " open"
            row += (f'<details{open_attr}><summary>content</summary>'
                    f'<pre>{html.escape(body + suffix)}</pre></details>')
        rows.append(row + "</li>")
    return "".join(rows)


def render_transcript_html(
    sessions: list[AgentTranscriptSession],
    *,
    generated_at: str | None = None,
    max_chars: int = _HTML_DEFAULT_MAX_CHARS,
    gap_seconds: float = _DEFAULT_GAP_SECONDS,
    title: str = "Goal Plus agent transcript",
) -> str:
    """Self-contained static HTML page: navigation, per-session summary and event rows."""
    def anchor(session: AgentTranscriptSession, position: int) -> str:
        return f"s{position}"

    parts: list[str] = [
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        f"<title>{html.escape(title)}</title><style>{_REPORT_CSS}</style></head><body>",
        f"<h1>{html.escape(title)}</h1>",
    ]
    if generated_at:
        parts.append(f"<p>generated {html.escape(generated_at)} · {len(sessions)} record(s)</p>")
    navigation, backlinks = _html_navigation(sessions)
    parts.append(navigation)
    for position, session in enumerate(sessions, 1):
        start, end, span = _session_span(session.events)
        counts = _count_by_kind(session.events)
        activity = " · ".join(f"{kind} {counts[kind]}" for kind in EVENT_KINDS if counts.get(kind))
        model_line = "model " + session.model if session.model else ""
        if session.thinking:
            model_line = (model_line + " · " if model_line else "model · ") + f"thinking {session.thinking}"
        facts = [
            f"<span>events {len(session.events)}</span>",
            f"<span>{html.escape(activity)}</span>" if activity else "",
            f"<span>{html.escape(model_line)}</span>" if model_line else "",
            f"<span>span {html.escape(_clock(start, with_date=True))} → {html.escape(_clock(end))}"
            + (f" ({_duration_text(span)})</span>" if span is not None else "</span>"),
        ]
        rows = [
            ("source", html.escape(session.source_path)),
            ("identity", html.escape(_identity_line(session.identity))),
        ]
        if position in backlinks:
            rows.append(("index", f'<a href="#{backlinks[position]}">'
                                 + ("Unlinked inferences" if backlinks[position] == "unlinked-inferences"
                                    else "Candidate index") + '</a>'))
        if session.role in {"verifier", "annotator"}:
            identity = session.identity
            workers = [(worker_position, worker) for worker_position, worker in enumerate(sessions, 1)
                       if worker.role == "candidate"
                       and worker.identity.get("run_id") == identity.get("run_id")
                       and worker.identity.get("candidate_id") == identity.get("candidate_id")]
            worker_id = identity.get("worker_session_id")
            workers = [(number, worker) for number, worker in workers
                       if worker_id and worker.identity.get("agent_session_id") == worker_id]
            if len(workers) == 1:
                worker_position, worker = workers[0]
                rows.append(("worker", f'<a href="#{anchor(worker, worker_position)}">'
                                       f'{html.escape(_display_name(worker))}</a>'))
            else:
                rows.append(("worker", "ambiguous candidate sessions" if workers else "unavailable"))
        footprint_lines = _footprint_text_lines(session.events)
        rows.append(("footprint", "<pre>" + html.escape(
            footprint_lines[0] + "\n" + "\n".join(line.strip() for line in footprint_lines[1:]))
            + "</pre>"))
        for warning in session.warnings:
            rows.append(("warning", html.escape(warning)))
        parts.append(f'<section id="{anchor(session, position)}"><header>'
                     f'<h2>#{position} {html.escape(_display_name(session))} ({html.escape(session.harness)})'
                     f' — {html.escape(session.session_id or "(unknown)")}</h2>'
                     f'<div class="facts">{"".join(fact for fact in facts if fact)}</div></header>')
        parts.append('<table class="summary">')
        for key, value in rows:
            if value:
                parts.append(f"<tr><td>{key}</td><td>{value}</td></tr>")
        parts.append("</table>")
        if session.events:
            event_limit = 0 if session.role in {"verifier", "annotator"} else max_chars
            parts.append(f'<ol class="events">{_html_event_rows(session.events, max_chars=event_limit, gap_seconds=gap_seconds)}</ol>')
            stream_count = sum(_is_raw_message_stream(event) for event in session.events)
            if stream_count:
                parts.append('<details class="raw-message-stream">'
                             f'<summary>Raw message stream ({stream_count} events)</summary>'
                             '<ol class="events">'
                             + _html_event_rows(session.events, max_chars=event_limit,
                                                gap_seconds=gap_seconds, raw_stream=True)
                             + '</ol></details>')
        parts.append("</section>")
    parts.append("<footer>Usage notes: context/output/total are tokens for that one provider call"
                 " (context = input side including cache hits, i.e. the context size the model"
                 " saw); N chars = this event's content length; usage appears once per model call.<br>"
                 "Debug export from local native session histories; may contain prompts,"
                 " tool arguments and outputs. Do not commit.</footer>")
    if any(session.role in {"verifier", "annotator"} for session in sessions):
        parts.append(f"<script>{_INDEX_SCRIPT}</script>")
    parts.append("</body></html>")
    return "".join(parts)


def render_transcript_jsonl(sessions: list[AgentTranscriptSession]) -> str:
    """Flat, one-event-per-line JSONL for offline tooling; content is not truncated."""
    lines = []
    for session in sessions:
        envelope = {
            "session_id": session.session_id, "harness": session.harness,
            "role": session.role, "source": session.source_path, "identity": session.identity,
        }
        for index, event in enumerate(session.events, 1):
            lines.append(json.dumps({
                **envelope, "seq": index, "at": event.at, "kind": event.kind,
                "label": event.label, "content": event.content, "meta": event.meta,
            }, ensure_ascii=False))
    return "\n".join(lines) + ("\n" if lines else "")
