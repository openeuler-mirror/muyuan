from __future__ import annotations


REPORT_CSS = """
:root {
  color-scheme: light;
  --page: #f4f6f8;
  --surface: #ffffff;
  --surface-subtle: #f8fafb;
  --text: #17212b;
  --muted: #5b6977;
  --border: #dce2e8;
  --border-strong: #b8c2cc;
  --accent: #176b87;
  --accent-soft: #e7f2f5;
  --success: #18794e;
  --success-soft: #e8f5ee;
  --warning: #a15c00;
  --warning-soft: #fff3d6;
  --failure: #b42318;
  --failure-soft: #fdecea;
  --worker: #6b5aa6;
  --parent: #b35c00;
  --metric-1: #dceff2;
  --metric-2: #9bcdd5;
  --metric-3: #4f9fb0;
  --metric-4: #176b87;
  --radius: 8px;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  background: var(--page);
  color: var(--text);
  font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 14px;
  line-height: 1.5;
  letter-spacing: 0;
}
button, input, select { font: inherit; }
button { letter-spacing: 0; }
a { color: var(--accent); }
code, pre, .mono, .metric-value, .timeline-time {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
}
.wrap { width: min(1440px, 100%); margin: 0 auto; padding: 0 24px; }
.masthead {
  background: var(--surface);
  border-bottom: 1px solid var(--border);
}
.masthead-inner {
  min-height: 82px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
}
.identity { min-width: 0; }
.eyebrow {
  color: var(--muted);
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
}
h1, h2, h3, p { margin-top: 0; }
h1 { margin-bottom: 4px; font-size: 28px; line-height: 36px; }
h2 { margin-bottom: 20px; font-size: 20px; line-height: 28px; }
h3 { margin-bottom: 12px; font-size: 14px; line-height: 20px; }
.identity-line { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.id-line { color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
.masthead-actions { display: flex; align-items: center; gap: 18px; flex: 0 0 auto; }
.generated { color: var(--muted); font-size: 11px; text-align: right; }
.generated strong { display: block; color: var(--text); font-size: 12px; }
.button {
  min-height: 36px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 7px 12px;
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  background: var(--surface);
  color: var(--text);
  cursor: pointer;
  font-weight: 650;
}
.button:hover { background: var(--surface-subtle); }
.button svg { width: 16px; height: 16px; }
.status {
  display: inline-flex;
  align-items: center;
  min-height: 22px;
  padding: 2px 8px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface-subtle);
  color: var(--muted);
  font-size: 11px;
  font-weight: 750;
  text-transform: uppercase;
  white-space: nowrap;
}
.status.success { color: var(--success); border-color: #b8dfca; background: var(--success-soft); }
.status.warning { color: var(--warning); border-color: #ead298; background: var(--warning-soft); }
.status.failure { color: var(--failure); border-color: #efbbb6; background: var(--failure-soft); }
.status.official { color: var(--accent); border-color: #a9d2dc; background: var(--accent-soft); }
.section-nav {
  position: sticky;
  top: 0;
  z-index: 20;
  background: rgba(255, 255, 255, 0.97);
  border-bottom: 1px solid var(--border);
}
.section-nav .wrap { display: flex; gap: 26px; overflow-x: auto; }
.section-nav a {
  padding: 13px 0 11px;
  color: var(--muted);
  border-bottom: 2px solid transparent;
  font-size: 13px;
  font-weight: 650;
  text-decoration: none;
  white-space: nowrap;
}
.section-nav a:hover { color: var(--accent); border-color: var(--accent); }
main { padding-top: 30px; padding-bottom: 72px; }
main.wrap { overflow-x: clip; }
.report-section {
  scroll-margin-top: 58px;
  padding: 0 0 40px;
  margin: 0 0 40px;
  border-bottom: 1px solid var(--border);
}
.section-kicker { margin-bottom: 14px; color: var(--muted); font-size: 11px; font-weight: 750; text-transform: uppercase; }
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(8, minmax(0, 1fr));
  gap: 1px;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--border);
}
.kpi { min-width: 0; min-height: 105px; padding: 16px; background: var(--surface); }
.kpi-label { color: var(--muted); font-size: 11px; font-weight: 650; }
.metric-value { margin: 8px 0 2px; font-size: 22px; line-height: 28px; font-weight: 750; overflow-wrap: anywhere; }
.metric-value.success { color: var(--success); }
.metric-value.warning { color: var(--warning); }
.kpi-detail { color: var(--muted); font-size: 11px; }
.two-column { display: grid; grid-template-columns: minmax(0, 2fr) minmax(300px, 1fr); gap: 24px; }
.panel { border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); }
.panel-body { padding: 20px; }
.panel + .panel { margin-top: 16px; }
.objective { margin-bottom: 0; color: var(--text); font-size: 15px; line-height: 24px; overflow-wrap: anywhere; }
.raw-goal { color: var(--muted); white-space: pre-wrap; overflow-wrap: anywhere; }
.fact-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; }
.fact { min-width: 0; padding-top: 12px; border-top: 1px solid var(--border); }
.fact dt { color: var(--muted); font-size: 11px; font-weight: 650; }
.fact dd { margin: 4px 0 0; font-weight: 650; overflow-wrap: anywhere; }
.completion-note { border-left: 3px solid var(--success); }
.completion-note p { margin-bottom: 0; color: var(--muted); }
.metric-gap-list { margin: 0; padding: 0; list-style: none; }
.metric-gap-list li { display: grid; grid-template-columns: minmax(190px, 0.8fr) 110px minmax(260px, 2fr); gap: 14px; padding: 9px 0; border-top: 1px solid var(--border); }
.metric-gap-list li:first-child { border-top: 0; }
.metric-gap-list code { color: var(--text); overflow-wrap: anywhere; }
.metric-gap-kind { color: var(--muted); font-size: 10px; font-weight: 750; text-transform: uppercase; }
.metric-gap-reason { color: var(--muted); font-size: 12px; }
.coverage { margin-top: 16px; }
.coverage-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; font-size: 12px; }
.coverage-bar { height: 5px; margin-top: 7px; overflow: hidden; border-radius: 3px; background: var(--border); }
.coverage-bar > span { display: block; height: 100%; background: var(--accent); }
.timeline-shell { overflow: hidden; }
.trajectory-shell { margin-bottom: 18px; }
.trajectory-head { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; margin-bottom: 8px; }
.trajectory-head h3 { margin: 0; }
.trajectory-head span { color: var(--muted); font-size: 11px; }
.trajectory-plot { width: 100%; min-height: 380px; }
.stop-progress-chart { padding: 18px 20px 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); }
.stop-progress-chart svg { display: block; width: 100%; height: auto; min-height: 180px; }
.stop-progress-axis { display: flex; justify-content: space-between; margin-top: 6px; color: var(--muted); font-size: 10px; }
.timeline-head { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 18px 20px; border-bottom: 1px solid var(--border); }
.timeline-head h2 { margin: 0; }
.metric-lens-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 10px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--surface-subtle);
}
.metric-scale { display: flex; align-items: center; gap: 7px; color: var(--muted); font-size: 10px; }
.metric-scale-bar { display: grid; grid-template-columns: repeat(4, 18px); height: 10px; overflow: hidden; border: 1px solid var(--border-strong); border-radius: 3px; }
.metric-scale-bar i:nth-child(1) { background: var(--metric-1); }
.metric-scale-bar i:nth-child(2) { background: var(--metric-2); }
.metric-scale-bar i:nth-child(3) { background: var(--metric-3); }
.metric-scale-bar i:nth-child(4) { background: var(--metric-4); }
.metric-control { display: inline-flex; overflow-x: auto; border: 1px solid var(--border-strong); border-radius: 6px; background: var(--surface); }
.metric-control button {
  min-height: 30px;
  padding: 5px 9px;
  border: 0;
  border-right: 1px solid var(--border);
  background: var(--surface);
  color: var(--muted);
  cursor: pointer;
  font-size: 11px;
  font-weight: 650;
  white-space: nowrap;
}
.metric-control button:last-child { border-right: 0; }
.metric-control button:hover { background: var(--accent-soft); color: var(--accent); }
.metric-control button[aria-pressed="true"] { background: var(--accent); color: #fff; }
.timeline-scroll { overflow-x: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }
.timeline { width: var(--timeline-width, 980px); min-width: 980px; }
.score-row { display: grid; grid-template-columns: 190px 1fr; min-height: 64px; border-bottom: 1px solid var(--border); background: var(--surface-subtle); }
.score-label { position: sticky; left: 0; z-index: 5; padding: 11px 14px; background: var(--surface-subtle); box-shadow: 1px 0 0 var(--border); }
.score-label strong, .score-label span { display: block; }
.score-label strong { font-size: 11px; }
.score-label span { margin-top: 2px; color: var(--muted); font-size: 9px; }
.score-track { position: relative; min-height: 64px; overflow: hidden; background: var(--surface); }
.score-track svg { display: block; width: 100%; height: 64px; }
.score-reference { stroke: var(--border-strong); stroke-width: 1; stroke-dasharray: 4 4; vector-effect: non-scaling-stroke; }
.score-step { fill: none; stroke: var(--success); stroke-width: 2; vector-effect: non-scaling-stroke; }
.score-point { fill: var(--surface); stroke: var(--success); stroke-width: 2; vector-effect: non-scaling-stroke; }
.score-ref-label { position: absolute; left: 8px; z-index: 2; padding: 0 3px; background: var(--surface); color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 9px; line-height: 12px; white-space: nowrap; }
.timeline-rows { max-height: min(62vh, 680px); overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }
.timeline-row { display: grid; grid-template-columns: 190px 1fr; min-height: 48px; border-bottom: 1px solid var(--border); }
.timeline-row:first-child { position: sticky; top: 0; z-index: 4; }
.timeline-label { position: sticky; left: 0; z-index: 3; padding: 13px 14px; background: var(--surface-subtle); box-shadow: 1px 0 0 var(--border); color: var(--muted); font-size: 10px; font-weight: 750; text-transform: uppercase; overflow-wrap: anywhere; }
.timeline-label strong, .timeline-label small { display: block; }
.timeline-label small { margin-top: 2px; color: var(--muted); font-size: 9px; font-weight: 600; line-height: 12px; text-transform: none; }
.timeline-row.redispatched .timeline-label { box-shadow: inset 3px 0 0 var(--accent), 1px 0 0 var(--border); }
.timeline-track { position: relative; min-height: 48px; border-left: 1px solid var(--border); background: var(--surface); }
.timeline-track::before, .timeline-track::after { content: ""; position: absolute; inset: 0 auto 0 33.333%; border-left: 1px solid var(--border); }
.timeline-track::after { left: 66.666%; }
.timeline-event {
  position: absolute;
  top: 12px;
  min-width: 8px;
  height: 24px;
  padding: 4px 7px;
  overflow: hidden;
  border-radius: 4px;
  color: #fff;
  font-size: 10px;
  line-height: 16px;
  text-overflow: ellipsis;
  white-space: nowrap;
  z-index: 2;
}
.timeline-event.main { background: var(--accent); }
.timeline-event.worker { background: var(--worker); }
.timeline-event.parent { background: var(--parent); }
.timeline-event.success { background: var(--success); }
.timeline-event.failure { background: var(--failure); }
.timeline-event.worker-session { display: flex; align-items: center; gap: 5px; border: 1px solid transparent; }
.timeline-event.worker-session.metric-level-1 { background: var(--metric-1); color: #17434b; }
.timeline-event.worker-session.metric-level-2 { background: var(--metric-2); color: #143e46; }
.timeline-event.worker-session.metric-level-3 { background: var(--metric-3); color: #fff; }
.timeline-event.worker-session.metric-level-4 { background: var(--metric-4); color: #fff; }
.timeline-shell[data-metric-mode="status"] .timeline-event.worker-session { background: var(--worker); color: #fff; }
.timeline-event.worker-session.session-failure { border: 2px solid var(--failure); box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.65); }
.session-state-icon { width: 13px; height: 13px; flex: 0 0 auto; color: var(--failure); }
.timeline-shell[data-metric-mode="status"] .session-state-icon { color: #fff; }
.metric-readout { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.retry-badge { display: inline-block; margin-left: 5px; padding: 0 4px; border: 1px solid var(--border-strong); border-radius: 3px; color: var(--accent); font-size: 8px; line-height: 13px; }
.timeline-idle { position: absolute; inset: 0 auto 0 0; z-index: 1; border-right: 1px dashed var(--border-strong); border-left: 1px dashed var(--border-strong); background: #f0f2f4; }
.timeline-idle-label { position: absolute; inset: 50% auto auto 50%; transform: translate(-50%, -50%); color: var(--muted); font-size: 9px; font-weight: 700; white-space: nowrap; }
.timeline-event.point { top: 4px; width: 10px !important; min-width: 10px; height: 10px; padding: 0; border: 2px solid var(--surface); border-radius: 50%; }
.timeline-axis { display: flex; justify-content: space-between; padding: 8px 10px 9px 200px; color: var(--muted); font-size: 10px; }
.timeline-key { display: flex; flex-wrap: wrap; gap: 14px; padding: 12px 20px; border-top: 1px solid var(--border); color: var(--muted); font-size: 11px; }
.key-dot { display: inline-block; width: 9px; height: 9px; margin-right: 5px; border-radius: 50%; background: var(--accent); }
.key-dot.worker { background: var(--worker); }
.key-dot.parent { background: var(--parent); }
.event-log { margin-top: 16px; }
.event-list { margin: 0; padding: 0; list-style: none; }
.event-list li { display: grid; grid-template-columns: 155px 90px 1fr; gap: 12px; padding: 8px 0; border-top: 1px solid var(--border); }
.lane { color: var(--muted); font-size: 11px; font-weight: 700; text-transform: uppercase; }
.task-tabs { display: flex; gap: 6px; margin-bottom: 18px; overflow-x: auto; }
.task-tab {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 38px;
  padding: 7px 12px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface);
  color: var(--muted);
  cursor: pointer;
  white-space: nowrap;
}
.task-tab[aria-selected="true"] { color: var(--accent); border-color: var(--accent); box-shadow: inset 0 -2px 0 var(--accent); }
.task-panel { margin-bottom: 26px; }
.js .task-panel[hidden] { display: none; }
.task-head { padding: 20px; border-bottom: 1px solid var(--border); background: var(--surface-subtle); }
.task-title-line { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.task-title-line h3 { margin: 0; font-size: 16px; }
.task-objective { max-width: 960px; margin: 10px 0 0; color: var(--muted); overflow-wrap: anywhere; }
.task-metrics { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 1px; background: var(--border); border-bottom: 1px solid var(--border); }
.task-metric { min-height: 78px; padding: 12px 16px; background: var(--surface); }
.task-metric strong { display: block; margin-top: 4px; font-size: 16px; overflow-wrap: anywhere; }
.subsection { padding: 20px; border-top: 1px solid var(--border); }
.subsection:first-child { border-top: 0; }
details.summary-block > summary { cursor: pointer; list-style: none; }
details.summary-block > summary::-webkit-details-marker { display: none; }
.table-scroll {
  min-width: 0;
  max-width: 100%;
  overflow-x: auto;
  contain: inline-size;
  border: 1px solid var(--border);
  border-radius: 6px;
}
table { width: 100%; border-collapse: collapse; background: var(--surface); font-size: 12px; }
.hook-table { min-width: 1040px; }
th { background: var(--surface-subtle); color: var(--muted); font-size: 10px; text-align: left; text-transform: uppercase; }
th, td { padding: 10px 12px; border-bottom: 1px solid var(--border); vertical-align: top; overflow-wrap: anywhere; }
tbody tr:last-child td { border-bottom: 0; }
.selected-row td:first-child { box-shadow: inset 3px 0 0 var(--success); }
.evidence-view-toolbar {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 18px;
  padding: 12px;
  border: 1px solid var(--border);
  border-bottom: 0;
  border-radius: 6px 6px 0 0;
  background: var(--surface-subtle);
}
.evidence-view-summary { display: flex; flex-wrap: wrap; gap: 14px; color: var(--muted); font-size: 11px; }
.evidence-view-summary strong { display: block; color: var(--text); font-size: 15px; }
.evidence-view-filters { display: flex; align-items: end; justify-content: flex-end; gap: 8px; flex-wrap: wrap; }
.evidence-view-filter { display: grid; gap: 3px; color: var(--muted); font-size: 10px; font-weight: 700; text-transform: uppercase; }
.evidence-view-filter select, .evidence-view-filter input {
  min-height: 34px;
  padding: 6px 9px;
  border: 1px solid var(--border-strong);
  border-radius: 5px;
  background: var(--surface);
  color: var(--text);
  font-size: 12px;
  text-transform: none;
}
.evidence-view-filter input { width: 220px; }
.evidence-view-count { min-width: 82px; padding-bottom: 8px; color: var(--muted); font-size: 11px; text-align: right; }
.evidence-view-scroll { max-height: 680px; border-radius: 0 0 6px 6px; overflow: auto; scrollbar-gutter: stable; }
.evidence-view-table { min-width: 1880px; table-layout: fixed; }
.evidence-view-table thead { position: sticky; top: 0; z-index: 3; }
.evidence-view-table th:nth-child(1) { width: 178px; }
.evidence-view-table th:nth-child(2) { width: 94px; }
.evidence-view-table th:nth-child(3) { width: 74px; }
.evidence-view-table th:nth-child(4) { width: 90px; }
.evidence-view-table th:nth-child(5) { width: 112px; }
.evidence-view-table th:nth-child(6) { width: 250px; }
.evidence-view-table th:nth-child(7) { width: 330px; }
.evidence-view-table th:nth-child(8) { width: 270px; }
.evidence-view-table th:nth-child(9) { width: 190px; }
.evidence-view-table th:nth-child(10) { width: 150px; }
.evidence-view-table tbody tr:hover td { background: var(--accent-soft); }
.evidence-view-table .official-evidence-row td { background: #f2f8fa; }
.evidence-view-table .official-evidence-row:hover td { background: #e2f0f3; }
.evidence-copy { color: var(--text); line-height: 18px; }
.evidence-score-kind { margin-top: 4px; color: var(--accent); font-size: 10px; font-weight: 700; text-transform: uppercase; }
.evidence-view-copy { color: var(--text); font-weight: 600; line-height: 18px; }
.evidence-view-meta { display: flex; align-items: center; gap: 7px; margin-top: 7px; }
.evidence-view-empty { color: var(--muted); font-style: italic; }
.evidence-tool-list { display: grid; gap: 8px; }
.evidence-tool { line-height: 16px; }
.evidence-tool-id { color: var(--accent); font-size: 11px; font-weight: 700; }
.evidence-tool-detail { margin-top: 3px; color: var(--muted); font-size: 11px; }
.evidence-view-error { margin-top: 5px; color: var(--failure); font-size: 11px; line-height: 16px; }
.evidence-view-monitor { margin-top: 5px; color: var(--muted); font-size: 10px; line-height: 15px; }
.revision { display: block; overflow: hidden; color: var(--accent); text-overflow: ellipsis; white-space: nowrap; }
.stats-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }
.stats-table { border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
.stats-table h3 { margin: 0; padding: 11px 12px; border-bottom: 1px solid var(--border); background: var(--surface-subtle); }
.activity-summary { margin-bottom: 32px; }
.stat-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 8px 12px; border-top: 1px solid var(--border); font-size: 11px; }
.stat-row:first-of-type { border-top: 0; }
.stat-row span:first-child { color: var(--muted); overflow-wrap: anywhere; }
.stat-row strong { text-align: right; overflow-wrap: anywhere; }
details.summary-block { margin-top: 14px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); }
details.summary-block > summary { padding: 11px 13px; color: var(--accent); font-weight: 700; }
details.summary-block > div, details.summary-block > pre { margin: 0; padding: 14px; border-top: 1px solid var(--border); }
pre { max-height: 600px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 11px; }
.warning-list { margin: 0; padding-left: 18px; }
.warning-list li + li { margin-top: 6px; }
.footnote { margin: 18px 0 0; color: var(--muted); font-size: 11px; }
@media (max-width: 1100px) {
  .kpi-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .two-column { grid-template-columns: 1fr; }
  .task-metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .stats-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .fact-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 760px) {
  .wrap { padding: 0 14px; }
  .masthead-inner { align-items: flex-start; flex-direction: column; padding: 15px 0; }
  .masthead-actions { width: 100%; justify-content: space-between; }
  .generated { text-align: left; }
  h1 { font-size: 24px; line-height: 31px; }
  .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .task-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .stats-grid { grid-template-columns: 1fr; }
  .event-list li { grid-template-columns: 1fr; gap: 2px; }
  .metric-gap-list li { grid-template-columns: 1fr; gap: 2px; }
  .timeline-head, .metric-lens-toolbar { align-items: flex-start; flex-direction: column; }
  .trajectory-head { align-items: flex-start; flex-direction: column; gap: 2px; }
  .trajectory-plot { min-height: 340px; }
  .evidence-view-toolbar { align-items: stretch; flex-direction: column; }
  .evidence-view-filters { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .evidence-view-filter input { width: 100%; }
  .evidence-view-count { min-width: 0; padding: 7px 0 0; text-align: left; }
  .metric-control { width: 100%; }
  .metric-control button { min-width: 0; flex: 1 1 0; padding-right: 4px; padding-left: 4px; font-size: 10px; }
}
@media (max-width: 480px) {
  .kpi-grid, .task-metrics, .fact-grid { grid-template-columns: 1fr; }
  .kpi { min-height: auto; }
  .masthead-actions { align-items: flex-start; flex-direction: column; }
  .button { width: 100%; }
  .evidence-view-filters { grid-template-columns: 1fr; }
}
@media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } }
@media print {
  @page { margin: 12mm; }
  body { background: #fff; font-size: 11px; }
  .no-print, .section-nav, .task-tabs { display: none !important; }
  .wrap { width: 100%; max-width: none; padding: 0; }
  .masthead-inner { min-height: auto; padding: 0 0 12px; }
  main { padding: 16px 0 0; }
  .report-section { margin-bottom: 22px; padding-bottom: 22px; }
  .js .task-panel[hidden], .task-panel { display: block !important; }
  .panel, .table-scroll, .stats-table, .timeline-shell, .trajectory-shell { break-inside: avoid; }
  details > * { display: block !important; }
  .timeline-scroll, .timeline-rows { max-height: none; overflow: visible; }
  .evidence-view-scroll { max-height: none; overflow: visible; }
  [data-evidence-row][hidden] { display: table-row !important; }
  .timeline { width: 100% !important; min-width: 0; }
  .timeline-row:first-child, .timeline-label { position: static; }
  .metric-lens-toolbar { display: none; }
  .kpi-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .kpi { min-height: 70px; padding: 10px; }
  .metric-value { font-size: 16px; }
}
"""


REPORT_SCRIPT = """
(function () {
  function reportColor(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function axisRef(prefix, index) {
    return index === 0 ? prefix : prefix + String(index + 1);
  }

  function layoutAxisKey(prefix, index) {
    return prefix + 'axis' + (index === 0 ? '' : String(index + 1));
  }

  function windowSeries(calls, values, details, start, end) {
    var result = {calls: [], values: [], details: []};
    calls.forEach(function (call, index) {
      if (call < start || call > end) return;
      result.calls.push(call);
      result.values.push(values[index]);
      result.details.push(details[index]);
    });
    return result;
  }

  function windowStep(calls, values, start, end) {
    var result = {calls: [], values: []};
    var priorIndex = null;
    calls.forEach(function (call, index) {
      if (call <= start) priorIndex = index;
      if (call >= start && call <= end) {
        result.calls.push(call);
        result.values.push(values[index]);
      }
    });
    if (priorIndex !== null && (result.calls.length === 0 || result.calls[0] > start)) {
      result.calls.unshift(start);
      result.values.unshift(values[priorIndex]);
    }
    return result;
  }

  function failureBand(axisSpec) {
    var range = axisSpec.range || [0, 1];
    var span = Math.max(range[1] - range[0], 0.1);
    if (axisSpec.type === 'log') {
      return {
        low: Math.pow(10, range[0]),
        marker: Math.pow(10, range[0] + span * 0.035),
        high: Math.pow(10, range[0] + span * 0.075)
      };
    }
    return {
      low: range[0],
      marker: range[0] + span * 0.035,
      high: range[0] + span * 0.075
    };
  }

  function renderTrajectory(node) {
    if (!window.Plotly || node.dataset.plotlyRendered === 'true') return;
    var payload;
    try {
      payload = JSON.parse(node.dataset.searchTrajectory || '{}');
    } catch (error) {
      node.textContent = 'Search trajectory data could not be decoded.';
      return;
    }
    var windows = payload.call_window
      ? [payload.call_window]
      : [{
          start: 0,
          end: payload.evaluations,
          tick: Math.max(1, Math.ceil(payload.evaluations / 12)),
          marker_size: 5
        }];
    var callLabel = payload.call_label || 'Verifier call';
    var bestLabel = payload.best_label || 'Global best';
    var pointLabel = payload.point_label || 'Iteration';
    var failureLabel = payload.failure_label || 'attempt failed';
    var failureLegend = payload.failure_legend || 'Failed attempt';
    var axisSpec = payload.score_axis || {type: 'linear', range: null};
    var palette = [
      reportColor('--accent'),
      reportColor('--worker'),
      reportColor('--parent'),
      reportColor('--success'),
      reportColor('--warning'),
      reportColor('--failure')
    ];
    var symbols = ['circle', 'square', 'diamond', 'triangle-up', 'triangle-down', 'cross'];
    var traces = [];
    var shapes = [];
    var annotations = [];
    var candidateLegendSeen = {};
    var globalLegendSeen = false;
    var selectedLegendSeen = false;
    var failureLegendSeen = false;
    var layout = {
      autosize: true,
      height: windows.length === 1 ? 500 : 170 + windows.length * 230,
      margin: {l: 74, r: 24, t: 92, b: 56},
      paper_bgcolor: reportColor('--surface'),
      plot_bgcolor: reportColor('--surface'),
      font: {family: 'Inter, ui-sans-serif, system-ui, sans-serif', color: reportColor('--text')},
      hoverlabel: {
        bgcolor: reportColor('--surface'),
        bordercolor: reportColor('--border-strong'),
        font: {color: reportColor('--text')}
      },
      hovermode: 'closest',
      legend: {
        orientation: 'h', x: 0, y: 1.04, xanchor: 'left', yanchor: 'bottom',
        font: {color: reportColor('--muted')}
      },
      shapes: shapes,
      annotations: annotations
    };
    var verticalGap = windows.length === 1 ? 0 : 0.065;
    var panelHeight = (1 - verticalGap * (windows.length - 1)) / windows.length;
    var failurePosition = failureBand(axisSpec);

    windows.forEach(function (windowSpec, windowIndex) {
      var xRef = axisRef('x', windowIndex);
      var yRef = axisRef('y', windowIndex);
      var xKey = layoutAxisKey('x', windowIndex);
      var yKey = layoutAxisKey('y', windowIndex);
      var domainTop = 1 - windowIndex * (panelHeight + verticalGap);
      var domainBottom = Math.max(0, domainTop - panelHeight);
      var failureCalls = [];
      var failureValues = [];
      var failureDetails = [];

      layout[xKey] = {
        domain: [0, 1],
        anchor: yRef,
        range: [windowSpec.start - 0.5, windowSpec.end + 0.5],
        dtick: windowSpec.tick,
        title: {text: windowIndex === windows.length - 1 ? callLabel : ''},
        color: reportColor('--muted'),
        gridcolor: reportColor('--border'),
        zerolinecolor: reportColor('--border'),
        automargin: true
      };
      layout[yKey] = {
        domain: [domainBottom, domainTop],
        anchor: xRef,
        type: axisSpec.type || 'linear',
        title: {text: payload.metric_name + (axisSpec.type === 'log' ? ' · log' : '')},
        color: reportColor('--muted'),
        gridcolor: reportColor('--border'),
        zerolinecolor: reportColor('--border'),
        tickformat: '~s',
        automargin: true
      };
      if (axisSpec.type === 'log') layout[yKey].dtick = 'D2';
      if (axisSpec.range) layout[yKey].range = axisSpec.range;

      if (windows.length > 1) {
        annotations.push({
          xref: 'paper', yref: 'paper', x: 0.995, y: domainTop,
          text: 'Calls ' + windowSpec.start + '–' + windowSpec.end,
          showarrow: false, xanchor: 'right', yanchor: 'top', yshift: -4,
          bgcolor: reportColor('--surface'), borderpad: 2,
          font: {size: 10, color: reportColor('--muted')}
        });
      }

      payload.trajectories.forEach(function (trajectory, trajectoryIndex) {
        var color = palette[trajectoryIndex % palette.length];
        var series = windowSeries(
          trajectory.calls, trajectory.scores, trajectory.details,
          windowSpec.start, windowSpec.end
        );
        if (series.calls.length) {
          traces.push({
            type: 'scatter',
            mode: 'lines+markers',
            name: trajectory.candidate_id + (trajectory.selected ? ' · selected' : ''),
            x: series.calls,
            y: series.values,
            customdata: series.details,
            xaxis: xRef,
            yaxis: yRef,
            showlegend: !candidateLegendSeen[trajectory.candidate_id],
            line: {color: color, width: trajectory.selected ? 2.6 : 1.8},
            marker: {
              color: color,
              size: (windowSpec.marker_size || 5) + (trajectory.selected ? 1.5 : 0),
              symbol: symbols[trajectoryIndex % symbols.length],
              line: {color: reportColor('--surface'), width: 0.8}
            },
            hovertemplate:
              '<b>' + trajectory.candidate_id + '</b><br>' +
              callLabel + ' %{x}<br>' +
              payload.metric_name + ' %{y:.4f}<br>' +
              pointLabel + ' %{customdata[0]} · %{customdata[1]}<br>' +
              '%{customdata[2]}<extra></extra>'
          });
          candidateLegendSeen[trajectory.candidate_id] = true;
        }
        (trajectory.failed_calls || []).forEach(function (call, failureIndex) {
          if (call < windowSpec.start || call > windowSpec.end) return;
          failureCalls.push(call);
          failureValues.push(failurePosition.marker);
          failureDetails.push([
            trajectory.candidate_id,
            (trajectory.failed_details[failureIndex] || [])[0],
            (trajectory.failed_details[failureIndex] || [])[1],
            (trajectory.failed_details[failureIndex] || [])[2],
            (trajectory.failed_scores || [])[failureIndex],
            (trajectory.failed_details[failureIndex] || [])[3] || failureLabel
          ]);
        });
      });

      var globalSeries = windowStep(
        payload.global_best.calls, payload.global_best.scores,
        windowSpec.start, windowSpec.end
      );
      if (globalSeries.calls.length) {
        traces.push({
          type: 'scatter',
          mode: 'lines',
          name: bestLabel,
          x: globalSeries.calls,
          y: globalSeries.values,
          xaxis: xRef,
          yaxis: yRef,
          showlegend: !globalLegendSeen,
          line: {color: reportColor('--text'), width: 3, shape: 'hv'},
          hovertemplate: callLabel + ' %{x}<br>Best-so-far %{y:.4f}<extra></extra>'
        });
        globalLegendSeen = true;
      }

      if (payload.selected_point && payload.selected_point.call >= windowSpec.start &&
          payload.selected_point.call <= windowSpec.end) {
        traces.push({
          type: 'scatter',
          mode: 'markers',
          name: 'Selected point',
          x: [payload.selected_point.call],
          y: [payload.selected_point.score],
          xaxis: xRef,
          yaxis: yRef,
          showlegend: !selectedLegendSeen,
          marker: {
            color: reportColor('--success'),
            size: 15,
            symbol: 'star',
            line: {color: reportColor('--text'), width: 2}
          },
          hovertemplate:
            '<b>Selected · ' + payload.selected_point.candidate_id + '</b><br>' +
            callLabel + ' %{x}<br>' + payload.metric_name + ' %{y:.4f}<extra></extra>'
        });
        selectedLegendSeen = true;
      }

      if (failureCalls.length) {
        shapes.push({
          type: 'rect', xref: xRef, yref: yRef,
          x0: windowSpec.start, x1: windowSpec.end,
          y0: failurePosition.low, y1: failurePosition.high,
          fillcolor: reportColor('--failure'), opacity: 0.055, line: {width: 0}, layer: 'below'
        });
        traces.push({
          type: 'scatter',
          mode: 'markers',
          name: failureLegend,
          x: failureCalls,
          y: failureValues,
          customdata: failureDetails,
          xaxis: xRef,
          yaxis: yRef,
          showlegend: !failureLegendSeen,
          marker: {
            color: reportColor('--failure'),
            size: (windowSpec.marker_size || 5) + 3,
            symbol: 'x',
            line: {width: 1}
          },
          hovertemplate:
            '<b>%{customdata[0]} · ' + failureLabel + '</b><br>' +
            callLabel + ' %{x}<br>' + pointLabel + ' %{customdata[1]} · %{customdata[2]}<br>' +
            '%{customdata[3]}<br>%{customdata[5]}<br>Raw score %{customdata[4]}<extra></extra>'
        });
        failureLegendSeen = true;
      }

      if (Number.isFinite(payload.baseline) && (axisSpec.type !== 'log' || payload.baseline > 0)) {
        shapes.push({
          type: 'line', xref: xRef, yref: yRef,
          x0: windowSpec.start, x1: windowSpec.end,
          y0: payload.baseline, y1: payload.baseline,
          line: {color: reportColor('--muted'), width: 1, dash: 'dot'}
        });
      }
    });

    node.style.minHeight = String(layout.height) + 'px';
    node.style.height = String(layout.height) + 'px';
    node.dataset.plotlyRendered = 'true';
    window.Plotly.newPlot(node, traces, layout, {
      displaylogo: false,
      responsive: true,
      modeBarButtonsToRemove: ['lasso2d', 'select2d'],
      toImageButtonOptions: {format: 'svg', filename: payload.export_name || 'complete-search-trajectory'}
    });
  }

  function renderVisibleTrajectories() {
    Array.prototype.forEach.call(document.querySelectorAll('[data-search-trajectory]'), function (node) {
      if (node.closest('.task-panel[hidden]')) return;
      if (node.dataset.plotlyRendered === 'true') {
        window.Plotly.Plots.resize(node);
      } else {
        renderTrajectory(node);
      }
    });
  }

  var buttons = Array.prototype.slice.call(document.querySelectorAll('[data-task-target]'));
  var panels = Array.prototype.slice.call(document.querySelectorAll('.task-panel'));
  function activate(runId, updateHash) {
    buttons.forEach(function (button) {
      button.setAttribute('aria-selected', button.dataset.taskTarget === runId ? 'true' : 'false');
    });
    panels.forEach(function (panel) { panel.hidden = panel.dataset.runId !== runId; });
    renderVisibleTrajectories();
    if (updateHash && history.replaceState) history.replaceState(null, '', '#task-' + runId);
  }
  buttons.forEach(function (button) {
    button.addEventListener('click', function () { activate(button.dataset.taskTarget, true); });
  });
  if (buttons.length) {
    var requested = location.hash.indexOf('#task-') === 0 ? location.hash.slice(6) : null;
    var initial = buttons.some(function (button) { return button.dataset.taskTarget === requested; })
      ? requested : buttons.find(function (button) { return button.getAttribute('aria-selected') === 'true'; }).dataset.taskTarget;
    activate(initial, false);
  }
  renderVisibleTrajectories();

  window.addEventListener('beforeprint', function () {
    Array.prototype.forEach.call(document.querySelectorAll('[data-search-trajectory]'), renderTrajectory);
  });

  var metricFormatters = {
    'score-gain': function (value) { return (value >= 0 ? '+' : '') + value.toFixed(4); },
    'score-raw': function (value) { return value.toFixed(4); },
    'tokens-per-minute': function (value) { return Math.round(value).toLocaleString() + '/min'; },
    'cost-per-minute': function (value) { return '$' + value.toFixed(4) + '/min'; },
    'verifier-density': function (value) { return value.toFixed(1) + '/min'; }
  };
  Array.prototype.forEach.call(document.querySelectorAll('[data-metric-lens]'), function (lens) {
    var metricButtons = Array.prototype.slice.call(lens.querySelectorAll('[data-metric-mode]'));
    var workerEvents = Array.prototype.slice.call(lens.querySelectorAll('.worker-session'));
    var lowLabel = lens.querySelector('[data-metric-low]');
    var highLabel = lens.querySelector('[data-metric-high]');
    var scaleBar = lens.querySelector('.metric-scale-bar');

    function missingLabel(mode) {
      return mode === 'score-gain' && lens.dataset.scoreGainBaseline === 'false'
        ? 'No baseline' : 'Not observed';
    }

    function setMode(mode) {
      var values = workerEvents.map(function (event) {
        var raw = event.getAttribute('data-metric-' + mode);
        return raw === null || raw === '' ? NaN : Number(raw);
      }).filter(Number.isFinite);
      var low = values.length ? Math.min.apply(Math, values) : null;
      var high = values.length ? Math.max.apply(Math, values) : null;
      lens.dataset.metricMode = mode;
      metricButtons.forEach(function (button) {
        button.setAttribute('aria-pressed', button.dataset.metricMode === mode ? 'true' : 'false');
      });
      workerEvents.forEach(function (event) {
        event.classList.remove('metric-level-1', 'metric-level-2', 'metric-level-3', 'metric-level-4');
        var readout = event.querySelector('.metric-readout');
        if (mode === 'status') {
          if (readout) readout.textContent = event.dataset.terminalState || 'unknown';
          return;
        }
        var raw = event.getAttribute('data-metric-' + mode);
        var value = raw === null || raw === '' ? NaN : Number(raw);
        if (!Number.isFinite(value)) {
          if (readout) readout.textContent = missingLabel(mode);
          return;
        }
        var ratio = high === low ? 0.5 : (value - low) / (high - low);
        var level = Math.min(4, Math.floor(ratio * 4) + 1);
        event.classList.add('metric-level-' + level);
        if (readout) readout.textContent = metricFormatters[mode](value);
      });
      if (mode === 'status') {
        if (lowLabel) lowLabel.textContent = 'Completed';
        if (highLabel) highLabel.textContent = 'Timed out';
        if (scaleBar) scaleBar.hidden = true;
      } else {
        if (lowLabel) lowLabel.textContent = low === null ? missingLabel(mode) : metricFormatters[mode](low);
        if (highLabel) highLabel.textContent = high === null ? missingLabel(mode) : metricFormatters[mode](high);
        if (scaleBar) scaleBar.hidden = false;
      }
    }

    metricButtons.forEach(function (button) {
      button.addEventListener('click', function () { setMode(button.dataset.metricMode); });
    });
    setMode(lens.dataset.metricMode || 'tokens-per-minute');
  });

  Array.prototype.forEach.call(document.querySelectorAll('[data-evidence-view]'), function (view) {
    var rows = Array.prototype.slice.call(view.querySelectorAll('[data-evidence-row]'));
    var count = view.querySelector('[data-evidence-count]');
    var filters = {
      candidate: view.querySelector('[data-evidence-filter="candidate"]'),
      disposition: view.querySelector('[data-evidence-filter="disposition"]'),
      viewState: view.querySelector('[data-evidence-filter="view-state"]'),
      text: view.querySelector('[data-evidence-filter="text"]')
    };

    function applyEvidenceFilters() {
      var query = filters.text ? filters.text.value.trim().toLowerCase() : '';
      var visible = 0;
      rows.forEach(function (row) {
        var matches = (!filters.candidate || !filters.candidate.value || row.dataset.candidate === filters.candidate.value) &&
          (!filters.disposition || !filters.disposition.value || row.dataset.disposition === filters.disposition.value) &&
          (!filters.viewState || !filters.viewState.value || row.dataset.viewState === filters.viewState.value) &&
          (!query || row.textContent.toLowerCase().indexOf(query) !== -1);
        row.hidden = !matches;
        if (matches) visible += 1;
      });
      if (count) count.textContent = visible + ' of ' + rows.length + ' rows';
    }

    [filters.candidate, filters.disposition, filters.viewState].forEach(function (filter) {
      if (filter) filter.addEventListener('change', applyEvidenceFilters);
    });
    if (filters.text) filters.text.addEventListener('input', applyEvidenceFilters);
    applyEvidenceFilters();
  });
})();
"""
