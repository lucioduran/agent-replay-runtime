"""Check saved evidence against current sources and replay invariants."""

import argparse
import hashlib
import json
from pathlib import Path

from agent_replay import replay

ROOT = Path(__file__).resolve().parents[1]


def verify(report, check_sources=True):
    assert report["schema_version"] == 1
    assert report["metadata"]["api_calls"] == 0
    assert report["metadata"]["api_cost_usd"] == 0
    if check_sources:
        paths = sorted([*ROOT.glob("agent_replay/*.py"), *ROOT.glob("experiments/*.py")])
        actual = {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths
        }
        assert report["metadata"]["source_sha256"] == actual, "Source fingerprints differ"
    expected = {
        (mode, stage)
        for mode in ("idempotent", "unsafe")
        for stage in ("after_request", "after_effect", "after_ack")
    }
    assert len(report["cases"]) == 6
    assert {(c["mode"], c["stage"]) for c in report["cases"]} == expected
    for case in report["cases"]:
        before, after = case["events_before"], case["events_after"]
        state = replay(after)
        replay(before)
        assert after[: len(before)] == before
        assert state.as_json() == case["final_state"]
        assert case["termination"]["returncode"] != 0
        assert case["termination"]["handshake"] == {"checkpoint": case["stage"]}
        assert case["termination"]["stderr"] == ""
        unresolved = case["mode"] == "unsafe" and case["stage"] == "after_request"
        assert state.finished is not unresolved
        assert len(case["effects_before"]) == (0 if case["stage"] == "after_request" else 1)
        assert len(case["effects_after"]) == (0 if unresolved else 1)
        if state.finished:
            effect = case["effects_after"][0]
            assert state.results["effect"] == {
                "effect_id": effect["effect_id"],
                "payload": effect["payload"],
            }
        if case["mode"] == "unsafe" and case["stage"] == "after_effect":
            assert case["reconciliation"] is not None
            assert "resolved" in [event["kind"] for event in after]
        else:
            assert case["reconciliation"] is None
        assert case["replay_consistent"] is True
        assert case["repeat_resume_unchanged"] == state.finished
    assert report["control"] == {"uninterrupted_effects": 1}
    effects = report["baseline"]["effects"]
    assert len(effects) == 2 and effects[0]["key"] == effects[1]["key"]
    assert effects[0]["payload"] == effects[1]["payload"]
    assert report["summary"] == {
        "crash_cases": 6,
        "idempotent_recovered_without_duplicates": 3,
        "unsafe_blocked": 2,
        "unsafe_reconciled_from_evidence": 1,
        "baseline_effects": 2,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    verify(json.loads(args.report.read_text()))
    print("Evidence replay and source fingerprints verified")


if __name__ == "__main__":
    main()
