"""Navigation stays bounded without merging or dropping inference contexts."""

from html.parser import HTMLParser
from xml.etree.ElementTree import TreeBuilder

from goal_plus.projections.agent_transcript import (
    AgentTranscriptSession, TranscriptEvent, render_transcript_html,
)


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
