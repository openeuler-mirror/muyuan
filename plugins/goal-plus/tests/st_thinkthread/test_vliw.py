from __future__ import annotations

import pytest


pytestmark = [pytest.mark.st, pytest.mark.st_pi_thinkthread]


def test_vliw_retained_candidate_settles_two_turns_and_annotations(run_thinkthread_scenario):
    report = run_thinkthread_scenario("vliw_retained_turns")

    assert [turn["score"] for turn in report["turns"]] == [143638, 139542]
    assert len({turn["attempt_id"] for turn in report["turns"]}) == 2
    assert len({turn["artifact_id"] for turn in report["turns"]}) == 2
    assert len({turn["inner_git_head"] for turn in report["turns"]}) == 2
    assert report["annotations_completed"] == 2
    assert report["immutable_reads"] >= 1
    assert report["root_unchanged"] is True
    assert report["promotion_mode"] == "artifact_only"
    assert report["promotion_state"] == "prepared"
    assert report["cleanup_states"] == ["removed"]
