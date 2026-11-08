"""tools/validate.py: schema and cross-reference checks over world/, sessions/ and cases/."""
import json
from pathlib import Path

from tools.validate import validate

TS = "2026-03-02T09:00:00-08:00"


def fact(fid, **kw):
    row = {"fact_id": fid, "subject": "principal", "predicate": "likes", "value": "tea", "valid_from": TS,
           "valid_to": None, "origin": "principal", "sensitivity": [], "stated_by": "principal", "channel": "typed",
           "session_ids": ["s001"], "supersedes": None, "notes": ""}
    row.update(kw)
    return row


def session(sid="s001", facts=("f1",)):
    return {"session_id": sid, "channel": "typed", "start_ts": TS, "end_ts": "2026-03-02T09:05:00-08:00",
            "participants": ["principal", "assistant"],
            "turns": [{"ts": TS, "speaker": "principal", "text": "I like tea."}],
            "renders_events": ["e1"], "asserts_facts": list(facts), "notes": ""}


def case(cid="c1", **gold):
    g = {"answer": "tea", "accept": [], "must_include_facts": ["f1"], "must_not_include_facts": [],
         "must_not_include_text": [], "expect": "answer"}
    g.update(gold)
    return {"case_id": cid, "area": "retrieval", "subarea": "paraphrase", "level": "L2",
            "at_ts": "2026-03-05T10:00:00-08:00", "channel": "typed", "prompt": "what do I drink?",
            "setup": {"sessions_through": "2026-03-05T00:00:00-08:00", "seed_fact_ids": None, "distractors": 0},
            "gold": g, "scoring": "judge", "difficulty": 2,
            "evidence": {"fact_ids": ["f1"], "session_ids": ["s001"], "why": "stated in s001 at 09:00"},
            "tags": []}


def build(root, facts=None, sessions=None, cases=None):
    (root / "world").mkdir()
    (root / "sessions").mkdir()
    (root / "cases").mkdir()
    (root / "docs").mkdir()
    (root / "docs" / "taxonomy.json").write_text(json.dumps({"areas": [{"area": "retrieval", "group": "B",
        "subareas": [{"subarea": "paraphrase", "target_cases": 1, "levels": ["L2"], "failure_mode": "x"}]}]}))
    (root / "world" / "timeline.jsonl").write_text(json.dumps(
        {"event_id": "e1", "ts": TS, "summary": "said tea", "creates": ["f1"], "changes": [], "ends": []}) + "\n")
    (root / "world" / "truth.jsonl").write_text("".join(json.dumps(f) + "\n" for f in (facts or [fact("f1")])))
    for s in sessions or [session()]:
        (root / "sessions" / f"{s['session_id']}.json").write_text(json.dumps(s))
    (root / "cases" / "b.jsonl").write_text("".join(json.dumps(c) + "\n" for c in (cases or [case()])))
    return root


def errors(root, **kw):
    return validate(build(root, **kw), schemas=Path(__file__).parent.parent / "schemas")


def test_valid_bundle_passes(tmp_path):
    assert errors(tmp_path) == []


def test_schema_violation_reported_with_location(tmp_path):
    bad = case()
    del bad["prompt"]
    errs = errors(tmp_path, cases=[bad])
    assert any("cases/b.jsonl:1" in e and "prompt" in e for e in errs)


def test_unknown_fact_in_case_evidence(tmp_path):
    errs = errors(tmp_path, cases=[case(must_include_facts=["nope"])])
    assert any("nope" in e for e in errs)


def test_fact_must_be_valid_at_probe_time(tmp_path):
    facts = [fact("f1", valid_to="2026-03-04T00:00:00-08:00")]
    errs = errors(tmp_path, facts=facts)
    assert any("not valid at" in e and "f1" in e for e in errs)


def test_truth_and_session_cross_reference_both_ways(tmp_path):
    errs = errors(tmp_path, sessions=[session(facts=())])
    assert any("f1" in e and "s001" in e for e in errs)


def test_duplicate_case_id(tmp_path):
    errs = errors(tmp_path, cases=[case("c1"), case("c1")])
    assert any("duplicate case_id c1" in e for e in errs)


def test_case_area_must_be_in_taxonomy(tmp_path):
    c = case()
    c["area"] = "vibes"
    errs = errors(tmp_path, cases=[c])
    assert any("vibes" in e for e in errs)


def test_sessions_through_after_probe(tmp_path):
    c = case()
    c["setup"]["sessions_through"] = "2026-03-06T00:00:00-08:00"
    assert any("sessions_through" in e for e in errors(tmp_path, cases=[c]))


def test_supersedes_must_exist_and_turn_order(tmp_path):
    s = session()
    s["turns"].append({"ts": "2026-03-02T08:00:00-08:00", "speaker": "principal", "text": "earlier?"})
    errs = errors(tmp_path, facts=[fact("f1", supersedes="ghost")], sessions=[s])
    assert any("ghost" in e for e in errs)
    assert any("out of order" in e for e in errs)


def test_gold_text_must_not_leak_into_must_not_include(tmp_path):
    errs = errors(tmp_path, cases=[case(must_not_include_text=["tea"])])
    assert any("both expected and forbidden" in e for e in errs)


def test_split_manifest_names_every_case_once(tmp_path):
    root = build(tmp_path, cases=[case("c1"), case("c2"), case("c3")])
    (root / "cases" / "split.json").write_text(json.dumps({"dev": ["c1", "c2", "private-1"], "test": ["c2"]}))
    errs = validate(root, schemas=Path(__file__).parent.parent / "schemas")
    assert any("split.json" in e and "c3" in e and "neither" in e for e in errs)
    assert any("split.json" in e and "c2" in e and "both" in e for e in errs)
    assert not any("c1" in e or "private-1" in e for e in errs)  # ids with no case present are the private half
    assert len(errs) == 2
