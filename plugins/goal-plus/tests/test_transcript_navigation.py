"""Navigation stays bounded without merging or dropping inference contexts."""

from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
from xml.etree.ElementTree import TreeBuilder

import pytest

from goal_plus.infrastructure.agent_transcript import write_transcript_files
from goal_plus.projections.agent_transcript import (
    AgentTranscriptSession, TranscriptEvent, render_transcript_html,
)
from tests.test_fork import fork_runtime
from tests.test_thinkthread_verification import prepared_candidates


class _HTMLTree(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.builder = TreeBuilder()
        self.feed(page)
        self.close()
        self.root = self.builder.close()

    def handle_starttag(self, tag, attrs):
        self.builder.start(tag, {key: value or "" for key, value in attrs})
        if tag in {"meta", "br", "input"}:
            self.builder.end(tag)

    def handle_endtag(self, tag):
        self.builder.end(tag)

    def handle_data(self, data):
        self.builder.data(data)


def _worker(run="run_1", candidate="c001", session="worker_1"):
    return AgentTranscriptSession(session, "pi", "candidate", identity={
        "run_id": run, "candidate_id": candidate, "agent_session_id": session,
    })


def _inference(role="verifier", iteration=1, attempt=1, **identity):
    return AgentTranscriptSession(
        f"{role}_{iteration}_{attempt}", "pi", role, identity={
            "run_id": "run_1", "candidate_id": "c001", "iteration": iteration,
            "settlement_id": f"settlement_{iteration}", "task_id": f"{role}_task_{iteration}",
            "attempt": attempt, "worker_session_id": "worker_1", **identity,
        }, events=[TranscriptEvent(kind="context", label="provider request #1", content="complete context")],
    )


def test_many_inferences_do_not_expand_top_level_navigation(tmp_path):
    sessions = [AgentTranscriptSession("main_1", "pi", "main"), _worker(),
                _worker(session="worker_2")]
    sessions.extend(_inference(role, iteration, attempt)
                    for iteration in range(1, 101) for role in ("verifier", "annotator")
                    for attempt in (1, 2, 3))
    page = render_transcript_html(sessions)
    root = _HTMLTree(page).root
    nav = root.find(".//nav")
    assert [link.get("href") for link in nav.findall("a")] == ["#s1", "#candidate-2"]
    index = root.find(".//article[@id='candidate-2']")
    assert len(index.findall("./div/a")) == 2
    details = index.find("details")
    assert "open" not in details.attrib
    assert "verifier 300, annotator 300" in details.find("summary").text
    assert len(details.findall(".//tbody/tr")) == 200
    assert len(details.findall(".//tbody/tr/td/div/a")) == 600
    assert len(root.findall(".//section")) == len(sessions)
    ids = [element.get("id") for element in root.iter() if element.get("id")]
    assert len(ids) == len(set(ids))
    assert all(link.get("href")[1:] in ids for link in root.findall(".//a"))
    assert page.count("complete context") == 600
    # A synthetic preview, never a re-export or alteration of a real run's records.
    (tmp_path / "navigation.html").write_text(page, encoding="utf-8")


def test_grouping_keeps_run_settlement_task_and_all_retries_distinct():
    sessions = [
        _worker(), _inference(attempt=2), _worker(run="run_2", session="worker_other"),
        _inference(), _inference(run_id="run_2", worker_session_id="worker_other"),
        _inference(settlement_id="other_settlement"), _inference(task_id="other_task"),
        _inference(), _worker(session="worker_replacement"), _inference(role="annotator"),
    ]
    root = _HTMLTree(render_transcript_html(sessions)).root
    nav = root.find(".//nav")
    assert [span.text for span in nav.findall("span")] == ["run_1", "run_2"]
    assert [a.get("href") for a in nav.findall("a")] == ["#candidate-1", "#candidate-3"]
    first = root.find(".//article[@id='candidate-1']")
    second = root.find(".//article[@id='candidate-3']")
    rows = first.findall(".//tbody/tr")
    assert len(rows) == 4
    same_task = next(row for row in rows if [a.get("href") for a in row.findall(".//a")]
                     == ["#s4", "#s8", "#s2"])
    assert "verifier_1_1" in "".join(same_task.itertext())
    assert [a.get("href") for a in second.findall(".//tbody//a")] == ["#s5"]
    assert root.find(".//section[@id='s5']//a[@href='#s3']") is not None
    assert root.find(".//section[@id='s4']//a[@href='#s1']") is not None
    assert root.find(".//section[@id='s4']//a[@href='#candidate-1']") is not None


def test_unlinked_records_remain_reachable_without_guessing_worker_identity():
    sessions = [_worker(), _inference(candidate_id="missing"), _inference(task_id=None),
                _inference(settlement_id=None), _inference(iteration=0),
                _inference(run_id="missing"), _inference(worker_session_id=None)]
    root = _HTMLTree(render_transcript_html(sessions)).root
    index = root.find(".//article[@id='unlinked-inferences']")
    assert len(index.findall(".//tbody/tr")) == 5
    assert {a.get("href") for a in index.findall(".//tbody//a")} == {f"#s{i}" for i in range(2, 7)}
    assert root.find(".//nav/a[@href='#unlinked-inferences']") is not None
    assert root.find(".//article[@id='candidate-1']//tbody//a[@href='#s7']") is not None
    assert root.find(".//section[@id='s7']//a[@href='#s1']") is None
    assert "unavailable" in "".join(root.find(".//section[@id='s7']").itertext())


def test_inference_only_export_and_untrusted_labels():
    malicious = '<script>alert("context")</script>'
    session = _inference(candidate_id=malicious, task_id=malicious)
    session.events[0] = TranscriptEvent(kind="context", content=malicious * 3000)
    root = _HTMLTree(render_transcript_html([session], max_chars=10)).root
    assert len(root.findall(".//script")) == 1
    assert root.find(".//nav/a").get("href") == "#unlinked-inferences"
    assert root.find(".//article//a").get("href") == "#s1"
    assert malicious * 3000 in "".join(root.find(".//section").itertext())
    empty = _HTMLTree(render_transcript_html([])).root
    assert empty.find(".//nav") is None


@pytest.mark.parametrize("result_format", ["direct", "structured", "text"])
def test_fork_context_links_exact_history_and_versions_without_rewriting_state(fork_runtime, result_format):
    from goal_plus.infrastructure.transcript_context import attach_report_context

    runtime, run_id, tasks, host, _ = fork_runtime
    host.fork_search_run(run_id)
    state = runtime.candidate_creation.state
    task = tasks[0]
    binding = state.read_candidate(run_id, task.candidate_id).task.candidate_origin
    report = runtime.run_verifier(run_id, task.candidate_id,
                         agent_session_id=binding.worker_identity["agent_session_id"],
                         hypothesis="baseline worker evidence")
    candidate = state.read_candidate(run_id, task.candidate_id)
    binding = candidate.task.candidate_origin
    original = state.read_frozen_spec(binding.frozen_spec_id)
    revised = runtime.revise_candidate_spec(
        binding.worker_identity["agent_session_id"], "context-revision", original.frozen_spec_id,
        "Clarify objective", {"objective": "Clarified branch objective"},
    )
    runtime.run_verifier(run_id, task.candidate_id,
                         agent_session_id=binding.worker_identity["agent_session_id"],
                         hypothesis="evidence under clarified contract")
    iteration = state.read_candidate(run_id, task.candidate_id).iterations[0]
    report_json = report.model_dump_json()
    result_content = report_json
    if result_format == "structured":
        result_content = json.dumps({"structuredContent": json.loads(report_json)})
    elif result_format == "text":
        result_content = json.dumps({
            "content": [{"type": "text", "text": report_json}], "isError": False,
        })
    parent = AgentTranscriptSession(binding.parent_native_session_id, "codex", "main", events=[
        TranscriptEvent(kind="meta", label="task complete", meta={"native_turn_id": "turn-7"}),
    ])
    child = AgentTranscriptSession("branch-1", "codex", "candidate", identity={
        "run_id": run_id, "candidate_id": task.candidate_id,
        "agent_session_id": binding.worker_identity["agent_session_id"],
    }, events=[
        TranscriptEvent(kind="meta", label="task complete", meta={"native_turn_id": "turn-7"}),
        TranscriptEvent(kind="user", label="prompt", content="BRANCH_PROMPT"),
        TranscriptEvent(kind="tool_result", label="goal_plus_search_run_verifier", content=result_content),
        *[TranscriptEvent(kind="tool_result", label="goal_plus_search_run_verifier", content=json.dumps(payload))
          for payload in (
              {"content": [{"type": "text", "text": report_json}], "isError": True},
              {"content": [{"type": "text", "text": "not JSON"}]},
              {"content": [{"type": "text", "text": report_json}, {"type": "text", "text": "ambiguous"}]},
          )],
    ])
    inference = AgentTranscriptSession("inv-1", "codex", "verifier", identity={
        "run_id": run_id, "candidate_id": task.candidate_id, "iteration": 1,
        "settlement_id": iteration.settlement_id, "task_id": "task-1", "attempt": 1,
        "worker_session_id": binding.worker_identity["agent_session_id"],
    })
    wrong = AgentTranscriptSession("inv-wrong", "codex", "annotator", identity={
        **inference.identity, "settlement_id": "another-settlement"})
    before = state.read_candidate(run_id, task.candidate_id)
    attach_report_context(runtime.root_dir, [parent, child, inference, wrong])
    assert child.report_context["candidate_origin"]["frozen_spec_id"] == original.frozen_spec_id
    assert child.report_context["inherited_events"] == 1
    assert child.report_context["iterations"][0]["frozen_spec_id"] == original.frozen_spec_id
    assert child.report_context["iterations"][0]["frozen_spec_id"] != revised.frozen_spec_id
    assert child.report_context["iterations"][0]["event_sequences"] == [3]
    assert child.report_context["iterations"][1]["frozen_spec_id"] == revised.frozen_spec_id
    assert child.report_context["iterations"][1]["event_sequences"] == []
    assert inference.report_context["iterations"][0]["frozen_spec_id"] == original.frozen_spec_id
    assert not wrong.report_context
    assert state.read_candidate(run_id, task.candidate_id) == before
    html = render_transcript_html([parent, child])
    assert '<details class="inherited-history">' in html
    assert html.index("</details>", html.index('class="inherited-history"')) < html.index("BRANCH_PROMPT")
    assert "report.html#iteration-" in html and "report.html#contract-" in html
    directory = runtime.root_dir / "host-logs/agent-transcript"
    result = write_transcript_files(directory, [parent, child, inference, wrong])
    from goal_plus.reporting import build_html_report_data, render_html_report

    data = build_html_report_data(runtime.root_dir, run_id)
    report_html = render_html_report(data)
    report_path = runtime.root_dir / "runs" / run_id / "report.html"
    report_path.write_text(report_html)
    details = data["search_tasks"][0]
    evidence = next(item for item in details["candidates"] if item["candidate_id"] == task.candidate_id)["iterations"][0]
    assert evidence["transcript"]["event_state"] == "exact"
    for link in evidence["transcript"]["links"]:
        assert f'id="{link["href"].split("#")[1]}"' in (directory / "index.html").read_text()
    worker_page = directory / result["sessions"][1]["name"] / "transcript.html"
    assert '../index.html#session-' in worker_page.read_text()


def test_tt_settlement_links_only_delivered_authoritative_feedback(prepared_candidates):
    from copy import deepcopy

    from goal_plus.application.candidate_host import append_candidate_attempt_feedback
    from goal_plus.domain.evidence import CandidateAttemptFeedback
    from goal_plus.infrastructure.transcript_context import attach_report_context
    from goal_plus.ports.thinkthread_verification import DeferredCandidateAttempt
    from goal_plus.tools import SearchTools
    from tests._thinkthread_candidates import attempt_parameters, verify_candidate

    runtime, sdk, [session] = prepared_candidates()
    accepted, reports = [], []
    for number in (1, 2):
        params = attempt_parameters(runtime, session, iteration=number)
        request = DeferredCandidateAttempt(
            **{key: value for key, value in params.items() if key != "hypothesis"},
            request_id=f"request-{number}",
        )
        accepted.append(SearchTools(runtime, candidate_attempt=request).goal_plus_search_run_verifier(
            run_id=session.run_id, candidate_id=session.candidate_id,
            agent_session_id=session.agent_session_id, hypothesis=params["hypothesis"],
        ))
        reports.append(verify_candidate(runtime, sdk, session, iteration=number))
    first = reports[0]
    feedback = CandidateAttemptFeedback(
        attempt_id=first.attempt_id, request_id="request-1", run_id=session.run_id,
        candidate_id=session.candidate_id, agent_session_id=session.agent_session_id,
        attempt_iteration=first.attempt_iteration, score_report=first,
        attempt_artifact_ref=first.attempt_artifact_ref,
        workspace_artifact_after_attempt=first.workspace_artifact_after_settlement,
        verified_at=session.created_at,
    )
    prompt = append_candidate_attempt_feedback("Continue the retained Child", feedback)
    events = [TranscriptEvent(kind="tool_result", label="goal_plus_search_run_verifier",
                              content=json.dumps(item)) for item in accepted]
    for field, value in (("run_id", "foreign-run"), ("agent_session_id", "foreign-session"),
                         ("attempt_artifact_ref", {"kind": "fs_snapshot", "id": "fsnap-foreign", "provider": "thinkthread"}),
                         ("frozen_spec_id", "foreign-spec")):
        payload = deepcopy(feedback.model_dump(mode="json"))
        if field != "frozen_spec_id":
            payload[field] = value
        if field != "agent_session_id":
            payload["score_report"][field] = value
        foreign = CandidateAttemptFeedback.model_validate(payload)
        events.append(TranscriptEvent(kind="user", label="prompt", content=
            append_candidate_attempt_feedback("Continue the retained Child", foreign)))
    events.extend([
        TranscriptEvent(kind="assistant", label="reply", content=prompt),
        TranscriptEvent(kind="user", label="prompt", content=feedback.model_dump_json()),
        TranscriptEvent(kind="user", label="prompt", content=prompt),
    ])
    child = AgentTranscriptSession("native-child", "pi", "candidate", identity={
        "run_id": session.run_id, "candidate_id": session.candidate_id,
        "agent_session_id": session.agent_session_id,
        "thinkthread_id": session.session_handle.external_id,
    }, events=events)
    state = runtime.candidate_creation.state
    before = state.read_candidate(session.run_id, session.candidate_id)

    attach_report_context(runtime.root_dir, [child])

    assert child.report_context["iterations"][0]["event_sequences"] == [len(events)]
    assert child.report_context["iterations"][1]["event_sequences"] == []
    assert child.events[-1].meta["settlement_id"] == before.iterations[0].settlement_id
    assert all("settlement_id" not in event.meta for event in child.events[:-1])
    assert state.read_candidate(session.run_id, session.candidate_id) == before


def test_report_links_use_export_manifest_and_keep_missing_context_explicit(tmp_path: Path):
    from goal_plus.infrastructure.report_transcripts import attach_transcript_links

    task = {"run_id": "r1", "candidates": [{"candidate_id": "c1", "iterations": [{"iteration": 1, "agent_session_id": "a1"}]}],
            "sessions": [{"agent_session_id": "a1", "candidate_id": "c1"}]}
    attach_transcript_links(tmp_path, task)
    assert task["candidates"][0]["transcript"]["state"] == "not_exported"
    directory = tmp_path / "host-logs/agent-transcript"
    write_transcript_files(directory, [])
    attach_transcript_links(tmp_path, task)
    assert task["candidates"][0]["transcript"]["state"] == "not_collected"
    worker = AgentTranscriptSession("native1", "pi", "candidate", identity={
        "run_id": "r1", "candidate_id": "c1", "agent_session_id": "a1"},
        events=[TranscriptEvent(kind="user", label="prompt", content="actual prompt")])
    write_transcript_files(directory, [worker])
    attach_transcript_links(tmp_path, task)
    link = task["sessions"][0]["transcript"]["links"][0]
    page = (directory / "index.html").read_text()
    assert f'id="{link["href"].split("#")[1]}"' in page
    assert task["candidates"][0]["iterations"][0]["transcript"]["event_state"] == "unknown"
    assert task["candidates"][0]["transcript"]["state"] == "available"
    worker.events = []
    for state in ("unavailable", "parse_failed"):
        worker.report_context["collection"] = {"state": state, "reason": "Native history could not be read."}
        write_transcript_files(directory, [worker])
        attach_transcript_links(tmp_path, task)
        assert task["candidates"][0]["transcript"]["state"] == state
        assert "No readable history" in task["candidates"][0]["transcript"]["reason"]


def test_stable_navigation_and_footprint_do_not_claim_actual_context(tmp_path: Path):
    session = AgentTranscriptSession("one", "codex", "candidate", events=[TranscriptEvent(content="body")])
    write_transcript_files(tmp_path, [session])
    one = json.loads((tmp_path / "manifest.json").read_text())["sessions"][0]
    write_transcript_files(tmp_path, [AgentTranscriptSession("other", "pi", "main"), session])
    two = json.loads((tmp_path / "manifest.json").read_text())["sessions"][1]
    assert one["anchor"] == two["anchor"]
    html = (tmp_path / "index.html").read_text()
    assert "history-text" in html and "not the current model window" in html
    assert "model-context" not in html
    branches = [AgentTranscriptSession("same-inherited-native-id", "codex", "candidate", identity={
        "run_id": "r1", "candidate_id": candidate, "agent_session_id": agent},
        events=[TranscriptEvent(content=candidate)]) for candidate, agent in (("c1", "a1"), ("c2", "a2"))]
    result = write_transcript_files(tmp_path, branches)
    assert len({item["name"] for item in result["sessions"]}) == 2
    for item, branch in zip(result["sessions"], branches):
        assert branch.identity["candidate_id"] in (tmp_path / item["name"] / "transcript.html").read_text()


def test_report_deep_links_reveal_other_tasks_and_contract_details():
    from goal_plus.projections.report_assets import REPORT_SCRIPT

    harness = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const listeners = {};
const panels = ['r1', 'r2'].map((runId, index) => ({dataset: {runId}, hidden: index === 1, tagName: 'ARTICLE', parentElement: null}));
const buttons = panels.map((panel, index) => ({dataset: {taskTarget: panel.dataset.runId}, selected: index === 0 ? 'true' : 'false',
  setAttribute(_, value) {this.selected = value;}, getAttribute() {return this.selected;}, addEventListener() {}}));
let scrolled = [];
const detail = {tagName: 'DETAILS', open: false, parentElement: panels[1], closest() {return panels[1];}, scrollIntoView() {scrolled.push('contract');}};
const candidate = {tagName: 'TR', parentElement: panels[0], closest() {return panels[0];}, scrollIntoView() {scrolled.push('candidate');}};
const location = {hash: '#contract-r2-spec-2'};
const document = {querySelectorAll(selector) {return selector === '[data-task-target]' ? buttons : selector === '.task-panel' ? panels : [];},
  getElementById(id) {return {'contract-r2-spec-2': detail, 'candidate-r1-c1': candidate}[id];}};
const window = {addEventListener(name, fn) {listeners[name] = fn;}};
vm.runInNewContext(fs.readFileSync(0, 'utf8'), {document, window, location, history: {replaceState() {}}, console});
assert.equal(panels[1].hidden, false);
assert.equal(panels[0].hidden, true);
assert.equal(detail.open, true);
location.hash = '#candidate-r1-c1';
listeners.hashchange();
assert.equal(panels[0].hidden, false);
assert.equal(panels[1].hidden, true);
assert.deepEqual(scrolled, ['contract', 'candidate']);
'''
    result = subprocess.run(["node", "-e", harness], input=REPORT_SCRIPT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
