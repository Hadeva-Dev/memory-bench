"""harness/replay.py: grouping cases, replaying sessions, daily maintenance, checkpoint snapshots."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness import data
from harness.adapter import Adapter, AdapterError, ContextResult
from harness.replay import plan, Replayer

FIX = Path(__file__).parent / "fixtures" / "mini"
DS = data.load(FIX)


class Recording(Adapter):
    """Keeps a list of what it saw, on disk across close/reopen; every probe also writes to the store."""
    name = "recording"

    def __init__(self, sticky=False):
        self.log, self.sticky, self.items = [], sticky, []

    def reset(self, workdir):
        self.wd = Path(workdir)
        if not self.sticky:
            self.items = []
        self.log.append(("reset",))

    def set_clock(self, ts):
        self.now = ts
        self.log.append(("clock", ts))

    def seed(self, facts):
        self.items += [f["fact_id"] for f in facts]
        self.log.append(("seed", len(facts)))

    def ingest(self, session):
        self.items.append(session["session_id"])
        self.log.append(("ingest", session["session_id"], self.now))

    def maintain(self):
        self.log.append(("maintain", self.now))

    def context(self, prompt, channel, session_id):
        text = " ".join(self.items)
        self.items.append(f"probe:{session_id}")
        return ContextResult(text=text, item_ids=list(self.items[:-1]))

    def close(self):
        (self.wd / "items.json").write_text(json.dumps(self.items))

    def reopen(self):
        self.items = json.loads((self.wd / "items.json").read_text())


def run(cases, adapter=None, tmp=None, **kw):
    adapter = adapter or Recording()
    seen = {}

    def probe(case):
        r = adapter.context(case["prompt"], case["channel"], f"probe-{case['case_id']}")
        seen[case["case_id"]] = (r.text.split(), adapter.now)

    stats = Replayer(adapter, DS, tmp, **kw).run(plan(cases, DS), probe)
    return adapter, seen, stats


def test_plan_groups_l1_by_seed_set_and_l2_by_distractors():
    groups = plan(DS.cases, DS)
    seed = [g for g in groups if g.kind == "seed"]
    rep = [g for g in groups if g.kind == "replay"]
    assert len(seed) == 4 and sorted(g.distractors for g in rep) == [0, 20]
    d0 = next(g for g in rep if g.distractors == 0)
    assert [c for c, _ in d0.cuts] == ["2026-03-04T00:00:00-08:00", "2026-03-10T00:00:00-07:00",
                                       "2026-03-10T08:00:00-07:00", "2026-03-12T00:00:00-07:00"]
    assert sum(len(g.cases) for g in groups) == len(DS.cases)


def test_probes_see_exactly_the_sessions_ended_by_their_cut(tmp_path):
    l2 = [c for c in DS.cases if c["level"] != "L1" and c["setup"]["distractors"] == 0]
    _, seen, stats = run(l2, tmp=tmp_path)
    sess = lambda cid: [x for x in seen[cid][0] if x.startswith("s0")]
    assert sess("b-retrieval-0002") == ["s0001", "s0002"]
    assert sess("c-update-0001") == ["s0001", "s0002", "s0003"]
    assert sess("d-forget-0001") == ["s0001", "s0002", "s0003", "s0004", "s0005"]
    assert stats["ingested"] == 5  # history ingested once per run


def test_probe_side_effects_do_not_reach_later_checkpoints(tmp_path):
    l2 = [c for c in DS.cases if c["level"] != "L1" and c["setup"]["distractors"] == 0]
    _, seen, _ = run(l2, tmp=tmp_path)
    assert not any(x.startswith("probe:") for x in seen["d-forget-0001"][0])


def test_clock_and_daily_maintenance(tmp_path):
    l2 = [c for c in DS.cases if c["case_id"] == "d-forget-0001"]
    a, seen, stats = run(l2, tmp=tmp_path)
    ingests = [e for e in a.log if e[0] == "ingest"]
    for _, sid, now in ingests:
        assert now == data.epoch(DS.session_by_id[sid]["end_ts"])
    maint = [e[1] for e in a.log if e[0] == "maintain"]
    # one per local midnight from 2026-03-03 to 2026-03-12 inclusive
    assert len(maint) == 10 and stats["maintained"] == 10
    days = [datetime.fromtimestamp(t, timezone.utc) for t in maint]
    assert all(d.minute == 0 for d in days)
    assert seen["d-forget-0001"][1] == data.epoch("2026-03-12T09:00:00-07:00")
    order = [e[0] for e in a.log if e[0] in ("ingest", "maintain")]
    assert order[:3] == ["ingest", "maintain", "ingest"]


def test_seed_groups_seed_once_with_distractors_mixed_in(tmp_path):
    l1 = [c for c in DS.cases if c["case_id"] == "e-scale-0001"]
    a, seen, _ = run(l1, tmp=tmp_path)
    assert ("seed", 51) in a.log
    toks = seen["e-scale-0001"][0]
    assert "f0001" in toks and 0 < toks.index("f0001") < 50  # not simply first or last
    assert seen["e-scale-0001"][1] == data.epoch(l1[0]["at_ts"])


def test_l2_distractors_are_seeded_before_replay(tmp_path):
    l2 = [c for c in DS.cases if c["case_id"] == "e-scale-0002"]
    a, seen, _ = run(l2, tmp=tmp_path)
    assert ("seed", 20) in a.log
    assert seen["e-scale-0002"][0][-2:] == ["s0001", "s0002"]


def test_reset_must_produce_an_empty_store(tmp_path):
    sticky = Recording(sticky=True)
    sticky.items = ["leftover"]
    with pytest.raises(AdapterError, match="empty"):
        run(DS.cases[:1], adapter=sticky, tmp=tmp_path)


def test_checkpoints_kept_on_request(tmp_path):
    l2 = [c for c in DS.cases if c["case_id"] in ("b-retrieval-0002", "c-update-0001")]
    run(l2, tmp=tmp_path, keep_checkpoints=True)
    snaps = sorted(p.name for p in (tmp_path / "checkpoints").iterdir())
    assert snaps == ["2026-03-04T000000-0800", "2026-03-10T000000-0700"]
    assert json.loads((tmp_path / "checkpoints" / snaps[0] / "items.json").read_text()) == ["s0001", "s0002"]
    run(l2, tmp=tmp_path / "b")
    assert not (tmp_path / "b" / "checkpoints").exists() or not any((tmp_path / "b" / "checkpoints").iterdir())


def test_ingest_never_hands_over_gold_bearing_session_fields(tmp_path):
    got = []

    class Keys(Recording):
        def ingest(self, session):
            got.append((set(session), {k for t in session["turns"] for k in t}))
            super().ingest(session)

    l2 = [c for c in DS.cases if c["level"] != "L1" and c["setup"]["distractors"] == 0]
    run(l2, adapter=Keys(), tmp=tmp_path)
    assert got and all(k == set(data.SESSION_FIELDS) | {"participant_names"} for k, _ in got)
    assert all(tk == {"ts", "speaker", "text", "speaker_name"} for _, tk in got)
    assert not {"asserts_facts", "renders_events", "notes"} & set(data.SESSION_FIELDS)


class Wrapper(Recording):
    """Reports no items and always returns a fixed wrapper text, as some systems do when nothing is found."""
    name = "wrapper"

    def context(self, prompt, channel, session_id):
        return ContextResult(text="No relevant memories.")


def test_empty_store_check_accepts_a_fixed_wrapper_with_no_items(tmp_path):
    a, seen, _ = run(DS.cases[:1], adapter=Wrapper(), tmp=tmp_path)
    assert DS.cases[0]["case_id"] in seen
