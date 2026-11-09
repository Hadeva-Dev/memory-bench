"""harness/adapters/dawnmere.py: model routing without the engine; end to end on the mini fixture (slow: it downloads
the released engine and its models, then runs them with a fake model)."""
import json
import shutil
from pathlib import Path

import pytest

from harness import data, llm
from harness.adapter import AdapterError
from harness.adapters import dawnmere
from harness.data import session_view
from harness.prompts import _ISOLATION

FIX = Path(__file__).parent / "fixtures" / "mini"


class FakeLLM:
    def __init__(self, fail=None):
        self.calls, self.fail, self.stats = [], fail, {"calls": 0}

    def complete(self, model, system, prompt, schema=None, nonce=""):
        self.calls.append((model, system, prompt, schema))
        if self.fail:
            raise llm.LLMError(self.fail)
        return {"ok": True}


def test_engine_calls_get_the_benchmark_model_and_the_isolation_sentence(monkeypatch):
    monkeypatch.delenv("DAWNMERE_BENCH_MODELS", raising=False)
    a = dawnmere.Dawnmere()
    a.llm = FakeLLM()
    out = a._model({"model": "haiku", "system": "You file entries.", "prompt": "p", "schema": {"type": "object"},
                    "job": "classify"})
    assert out == {"ok": True, "output": {"ok": True}}
    model, system, prompt, schema = a.llm.calls[0]
    assert model == llm.DEFAULT_ANSWERER and system == "You file entries.\n\n" + _ISOLATION and prompt == "p"
    assert a.jobs == {"classify": 1} and a.name == "dawnmere"


def test_product_models_follow_the_engine_settings(monkeypatch):
    monkeypatch.setenv("DAWNMERE_BENCH_MODELS", "product")
    a = dawnmere.Dawnmere()
    a.llm = FakeLLM()
    a._model({"model": "haiku", "system": "s", "prompt": "p"})
    assert a.llm.calls[0][0] == "claude-haiku-5-5" and a.name == "dawnmere-product-models"
    monkeypatch.setenv("DAWNMERE_BENCH_MODELS", "cheap")
    with pytest.raises(AdapterError):
        dawnmere.Dawnmere()


def test_a_failed_call_goes_back_to_the_engine_but_a_usage_cap_stops_the_run(monkeypatch):
    monkeypatch.delenv("DAWNMERE_BENCH_MODELS", raising=False)
    a = dawnmere.Dawnmere()
    a.llm = FakeLLM(fail="no structured_output in the response")
    assert a._model({"model": "haiku", "system": "s", "prompt": "p"})["ok"] is False
    a.llm = FakeLLM(fail="error result: usage_limit_reached")
    with pytest.raises(AdapterError):
        a._model({"model": "haiku", "system": "s", "prompt": "p"})


def test_a_local_wheel_must_be_an_engine_wheel(monkeypatch, tmp_path):
    monkeypatch.setenv("DAWNMERE_WHEEL", str(tmp_path / "something-0.1.0-py3-none-any.whl"))
    with pytest.raises(AdapterError):
        dawnmere.install()


def fill(schema):
    """The smallest value a JSON schema accepts: what a model that finds nothing would answer."""
    t = schema.get("type")
    t = next((x for x in t if x != "null"), "null") if isinstance(t, list) else t
    if "enum" in schema:
        return schema["enum"][0]
    if t == "object":
        return {k: fill(v) for k, v in (schema.get("properties") or {}).items() if k in (schema.get("required") or [])}
    return {"array": [], "string": "", "integer": 0, "number": 0, "boolean": False}.get(t)


class SchemaLLM(FakeLLM):
    def complete(self, model, system, prompt, schema=None, nonce=""):
        self.calls.append((model, system, prompt, schema))
        return fill(schema or {})


@pytest.mark.slow
def test_end_to_end_on_the_mini_fixture(tmp_path):
    ds = data.load(FIX)
    a = dawnmere.Dawnmere()
    a.llm = SchemaLLM()
    store = tmp_path / "work" / "store"
    store.mkdir(parents=True)
    a.reset(store)
    try:
        assert a.context("what do you know about me?", "typed", "probe-empty").item_ids == []
        for s in ds.sessions:
            a.set_clock(data.ts(s["end_ts"]).timestamp())
            a.ingest(session_view(s, ds.device_names))
        a.maintain()
        r = a.context("what time is my dentist appointment?", "typed", "probe-1")
        assert isinstance(r.text, str) and all(i.startswith(("m", "c")) for i in r.item_ids)
        assert a.context("what time is my dentist appointment?", "remote", "probe-2").tokens >= 0
        a.close()
        shutil.copytree(store, tmp_path / "snap")
        a.reopen()
        assert a.guard() == {"refused": []}
        assert a.llm.calls and all(m == llm.DEFAULT_ANSWERER for m, *_ in a.llm.calls)
        assert json.loads((store.parent / "dawnmere-engine.json").read_text())["engine_version"]
    finally:
        a._stop()
