"""tools/sessions_month: per-month checks and the merge into the truth log."""
import json

from tests.test_validate import build, fact, session
from tools.sessions_month import check, merge


def test_month_check_and_merge(tmp_path):
    root = build(tmp_path, facts=[fact("f1", session_ids=[])], sessions=[session("s0001", facts=("f1",))])
    (root / "sessions" / "s001.json").unlink(missing_ok=True)
    errs = check(root, 3)
    assert any("_links/3.json is missing" in e for e in errs)
    (root / "sessions" / "_links").mkdir()
    (root / "sessions" / "_links" / "3.json").write_text(json.dumps({"f1": ["s0001"]}))
    assert check(root, 3) == []
    assert merge(root) == 1
    assert json.loads((root / "world" / "truth.jsonl").read_text())["session_ids"] == ["s0001"]
    assert merge(root) == 0
