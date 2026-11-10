"""tools/llm_proxy.py and the competitor adapters (harness/adapters/mem0.py, zep_graphiti.py, claude_mem.py), with
fake models.

The proxy is tested through the real OpenAI Python client, with harness/llm.py's subprocess call replaced by a fake
`claude` runner. The adapters run end to end through harness.run on tests/fixtures/mini with a fake model behind
the proxy. They need the optional dependencies (`uv sync --extra competitors`) and are skipped without them;
claude_mem needs its isolated install (harness/adapters/claude_mem.py) and is skipped without it.
"""
import base64
import hashlib
import json
import os
import re
from functools import partial
from pathlib import Path

import numpy as np
import pytest

from harness import llm, run
from harness.prompts import _ISOLATION
from harness.score import stems
from tools import llm_proxy
from tools.llm_proxy import LLMProxy, extract_json_object, split_messages

# Before anything imports mem0 or graphiti_core: they read these at import time.
os.environ.setdefault("MEM0_TELEMETRY", "False")
os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")

openai = pytest.importorskip("openai")
FIX = Path(__file__).parent / "fixtures" / "mini"


def need_mem0():
    import harness.adapters.mem0  # noqa: F401  sets MEM0_TELEMETRY before mem0 is imported
    pytest.importorskip("mem0")


def fake_embed(texts, kind):
    """Bag of hashed stems in 384 dimensions, normalised: lexical overlap is similarity."""
    out = np.zeros((len(texts), 384), dtype=np.float32)
    for i, t in enumerate(texts):
        for w in stems(t):
            out[i, int(hashlib.md5(w.encode()).hexdigest(), 16) % 383] += 1
        out[i, 383] = 0.05
    return out / np.linalg.norm(out, axis=1, keepdims=True)


class FakeClaude:
    """Stands in for subprocess.run of the claude CLI: records commands, answers with `reply(system, prompt, schema)`."""

    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def __call__(self, cmd, input=None, **kw):
        schema = json.loads(cmd[cmd.index("--json-schema") + 1]) if "--json-schema" in cmd else None
        system = cmd[cmd.index("--system-prompt") + 1]
        self.calls.append({"cmd": cmd, "system": system, "prompt": input, "schema": schema})
        out = self.reply(system, input, schema)
        d = {"type": "result", "is_error": False, "result": out if isinstance(out, str) else json.dumps(out),
             "usage": {"input_tokens": 7, "output_tokens": 3}, "total_cost_usd": 0.0}
        if schema is not None:
            d["structured_output"] = out

        class P:
            returncode, stderr, stdout = 0, "", json.dumps(d)
        return P()


@pytest.fixture
def proxy_with(tmp_path):
    made = []

    def _make(reply, **kw):
        fc = FakeClaude(reply)
        p = LLMProxy(llm=llm.LLM(tmp_path / f"cache{len(made)}", run=fc, backoff=0), embed=fake_embed, **kw)
        client = openai.OpenAI(base_url=p.start(), api_key="unused", max_retries=0)
        made.append(p)
        return p, client, fc
    yield _make
    for p in made:
        p.stop()


# --- proxy ------------------------------------------------------------------------------------------------------

def test_chat_json_schema_goes_to_the_cli_as_json_schema(proxy_with):
    schema = {"type": "object", "properties": {"names": {"type": "array", "items": {"type": "string"}}},
              "required": ["names"]}
    p, client, fc = proxy_with(lambda s, pr, sc: {"names": ["Juno"]})
    r = client.chat.completions.create(
        model="gpt-4.1-mini", messages=[{"role": "system", "content": "Extract names."},
                                        {"role": "user", "content": "Juno called."}],
        response_format={"type": "json_schema", "json_schema": {"name": "Names", "schema": schema}})
    assert json.loads(r.choices[0].message.content) == {"names": ["Juno"]}
    call = fc.calls[0]
    assert call["schema"] == schema and call["prompt"] == "Juno called."
    assert call["system"].startswith("Extract names.") and call["system"].endswith(_ISOLATION)
    cmd = call["cmd"]
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5-5"  # whatever was asked for
    for flag in ("--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands"):
        assert flag in cmd
    assert cmd[cmd.index("--tools") + 1] == "" and cmd[cmd.index("--setting-sources") + 1] == ""
    c = p.cost()
    assert c["calls"] == 1 and c["model_calls"] == 1 and c["input_tokens"] == 7 and c["output_tokens"] == 3
    # the same request again is served from the disk cache: counted as a call, not a model call
    client.chat.completions.create(
        model="gpt-4.1-mini", messages=[{"role": "system", "content": "Extract names."},
                                        {"role": "user", "content": "Juno called."}],
        response_format={"type": "json_schema", "json_schema": {"name": "Names", "schema": schema}})
    c = p.cost()
    assert c["calls"] == 2 and c["model_calls"] == 1 and c["cache_hits"] == 1
    p.reset_stats()
    assert p.cost()["calls"] == 0 and p.cost()["model_calls"] == 0


def test_chat_json_object_returns_the_json_in_the_reply(proxy_with):
    p, client, fc = proxy_with(lambda s, pr, sc: 'Sure:\n```json\n{"memory": [{"id": "0", "text": "x"}]}\n```')
    r = client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}],
                                       response_format={"type": "json_object"})
    assert json.loads(r.choices[0].message.content) == {"memory": [{"id": "0", "text": "x"}]}
    assert fc.calls[0]["schema"] is None and llm_proxy.JSON_MODE in fc.calls[0]["system"]
    assert extract_json_object("no json here") == "no json here"


def test_chat_plain_text_and_transcript(proxy_with):
    p, client, fc = proxy_with(lambda s, pr, sc: "hello back")
    r = client.chat.completions.create(model="m", messages=[
        {"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
        {"role": "user", "content": [{"type": "text", "text": "how are you"}]}])
    assert r.choices[0].message.content == "hello back" and r.choices[0].finish_reason == "stop"
    assert fc.calls[0]["prompt"] == "User:\nhi\n\nAssistant:\nhello\n\nUser:\nhow are you"
    assert fc.calls[0]["system"] == _ISOLATION
    sys_, prompt = split_messages([{"role": "developer", "content": "D"}, {"role": "user", "content": "U"}])
    assert sys_ == "D\n\n" + _ISOLATION and prompt == "U"


def test_chat_tool_calls(proxy_with):
    tools = [{"type": "function", "function": {"name": "add_memory", "description": "store one fact",
                                               "parameters": {"type": "object",
                                                              "properties": {"data": {"type": "string"}},
                                                              "required": ["data"]}}}]
    p, client, fc = proxy_with(lambda s, pr, sc: {"call": {"tool": "add_memory", "arguments": {"data": "likes tea"}}})
    r = client.chat.completions.create(model="m", messages=[{"role": "user", "content": "I like tea"}], tools=tools)
    msg = r.choices[0].message
    assert r.choices[0].finish_reason == "tool_calls"
    assert msg.tool_calls[0].function.name == "add_memory"
    assert json.loads(msg.tool_calls[0].function.arguments) == {"data": "likes tea"}
    branches = fc.calls[0]["schema"]["properties"]["call"]["anyOf"]
    assert branches[0]["properties"]["arguments"]["required"] == ["data"]
    assert branches[-1]["properties"]["tool"] == {"const": "none"}  # tool_choice auto: may decline
    assert "add_memory" in fc.calls[0]["system"]


def test_chat_stream_is_one_chunk_then_done(proxy_with):
    p, client, fc = proxy_with(lambda s, pr, sc: "streamed reply")
    chunks = list(client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}], stream=True,
                                                 stream_options={"include_usage": True}))
    assert "".join(c.choices[0].delta.content or "" for c in chunks if c.choices) == "streamed reply"
    assert chunks[0].choices[0].finish_reason == "stop" and chunks[-1].usage.total_tokens > 0
    assert len(fc.calls) == 1


def test_embeddings_float_and_base64(proxy_with):
    p, client, _ = proxy_with(lambda s, pr, sc: "")
    want = fake_embed(["hello world", "noodle house"], "passage")
    r = client.embeddings.create(model="text-embedding-3-small", input=["hello world", "noodle house"])  # base64
    got = np.array([d.embedding for d in r.data], dtype=np.float32)
    assert got.shape == (2, 384) and np.allclose(got, want, atol=1e-6)
    r2 = client.embeddings.create(model="x", input="hello world", encoding_format="float")
    assert np.allclose(r2.data[0].embedding, want[0], atol=1e-6)
    raw = p.embeddings({"input": ["a"], "encoding_format": "base64"})["data"][0]["embedding"]
    assert np.frombuffer(base64.b64decode(raw), dtype="<f4").shape == (384,)
    c = p.cost()
    assert c["embed_calls"] == 3 and c["embedded_texts"] == 4 and c["calls"] == 0


def test_bad_requests_get_openai_errors(proxy_with):
    _, client, _ = proxy_with(lambda s, pr, sc: "")
    with pytest.raises(openai.BadRequestError):
        client.embeddings.create(model="x", input=[])
    with pytest.raises(openai.InternalServerError):
        proxy_with(lambda s, pr, sc: 1 / 0)[1].chat.completions.create(
            model="m", messages=[{"role": "user", "content": "x"}])


# --- adapters end to end on the mini fixture -----------------------------------------------------------------------

def run_model(model, system, prompt, schema):
    """The harness's own answerer and judges (as in tests/test_run.py, simplified)."""
    from harness.prompts import read_payload
    if schema is None:
        ctx = prompt.split("MEMORY CONTEXT:\n", 1)[1].split("\n\nUSER MESSAGE:", 1)[0]
        return "I don't know." if ctx == "(empty)" else f"From memory: {ctx}"
    p = read_payload(prompt)
    if "verdict" in schema["properties"]:
        text = p.get("answer", p.get("context", ""))
        gold = set(stems(p.get("gold_answer") or ""))
        hit = len(gold & set(stems(text))) / max(1, len(gold))
        return {"verdict": "correct" if hit >= 0.6 else "wrong", "reason": "overlap"}
    if "revealed" in schema["properties"]:
        return {"revealed": [], "reason": ""}
    return {"present": [], "reason": ""}


def mem0_model(model, system, prompt, schema):
    """Mem0's additive extraction: every user line of the new messages becomes a memory."""
    new = prompt.split("## New Messages", 1)[1].split("## Observation Date", 1)[0]
    mems = [{"id": str(i), "text": line.split(":", 1)[1].strip()}
            for i, line in enumerate(new.strip().splitlines()) if line.startswith("user:")]
    return json.dumps({"memory": mems})


def _between(text, a, b):
    if a not in text:
        return ""
    return text.split(a, 1)[1].split(b, 1)[0]


def graphiti_model(model, system, prompt, schema):
    """Graphiti's structured calls: one entity besides the speaker, one fact per line of the episode."""
    title = schema.get("title")
    current = (_between(prompt, "<CURRENT MESSAGE>", "</CURRENT MESSAGE>")
               or _between(prompt, "<CURRENT_MESSAGE>", "</CURRENT_MESSAGE>") or _between(prompt, "<TEXT>", "</TEXT>"))
    lines = [ln.strip() for ln in current.strip().splitlines() if ln.strip()]
    if title == "ExtractedEntities":
        return {"extracted_entities": [{"name": "user", "entity_type_id": 0},
                                       {"name": "life", "entity_type_id": 0}]}
    if title == "ExtractedEdges":
        return {"edges": [{"source_entity_name": "user", "target_entity_name": "life", "relation_type": "SAID",
                           "fact": ln, "valid_at": None, "invalid_at": None} for ln in lines]}
    return minimal(schema)


def minimal(schema, root=None):
    """The smallest instance a JSON schema accepts (empty lists, empty strings, zero, null where allowed)."""
    root = root or schema
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[1]
        return minimal(root["$defs"][name], root)
    if "anyOf" in schema:
        nulls = [s for s in schema["anyOf"] if s.get("type") == "null"]
        return None if nulls else minimal(schema["anyOf"][0], root)
    t = schema.get("type")
    if t == "object":
        return {k: minimal(v, root) for k, v in schema.get("properties", {}).items()
                if k in schema.get("required", [])}
    return {"array": [], "string": "", "integer": 0, "number": 0, "boolean": False, "null": None}.get(t)


def adapter_model(extract):
    """One fake behind the proxy: extraction calls go to `extract`, harness calls never reach the proxy."""
    return llm.FakeLLM(extract)


@pytest.fixture
def run_adapter(tmp_path, monkeypatch):
    def _go(module, extract, *args):
        mod = pytest.importorskip(f"harness.adapters.{module}")
        made = []

        def proxy(**kw):
            kw.update(llm=adapter_model(extract), embed=fake_embed)
            p = LLMProxy(**kw)
            made.append(p)
            return p
        monkeypatch.setattr(mod, "LLMProxy", proxy)
        fl = llm.FakeLLM(run_model)
        argv = ["--adapter", module, "--data-root", str(FIX), "--results-dir", str(tmp_path / "results"),
                "--run-id", "r1", *args]
        code = run.main(argv, llm_client=fl, all_splits=True)
        d = tmp_path / "results" / module / "r1"
        recs = {r["case_id"]: r for r in map(json.loads, (d / "cases.jsonl").read_text().splitlines())}
        for p in made:
            p.stop()
        return code, recs, json.loads((d / "summary.json").read_text()), made
    return _go


def test_mem0_end_to_end(run_adapter):
    need_mem0()
    code, recs, summary, proxies = run_adapter("mem0", mem0_model)
    assert code == 0 and len(recs) == 16
    assert not [r["error"] for r in recs.values() if r["error"]]
    for r in recs.values():
        assert all(re.fullmatch(r"[0-9a-f-]{36}", i) for i in r["context"]["item_ids"])
        assert len(r["context"]["item_ids"]) == (r["context"]["text"].count("\n") + 1 if r["context"]["text"] else 0)
    assert "dentist" in recs["c-update-0003"]["context"]["text"]  # L2: extracted from the sessions
    assert "oat milk cortado" in recs["e-scale-0001"]["context"]["text"]  # L1: seeded with infer=False
    # one extraction call per ingested session (5 in the main replay, 2 in the distractor replay), none for seeds
    assert summary["cost"]["system_total"]["model_calls"] == 7


def test_mem0_unit_ingest_seed_and_snapshot(tmp_path):
    need_mem0()
    import shutil
    from harness import data
    from harness.adapters.mem0 import Mem0Adapter, turn_message
    ds = data.load(FIX)
    prompts = []

    def extract(model, system, prompt, schema):
        prompts.append(prompt)
        return mem0_model(model, system, prompt, schema)
    a = Mem0Adapter(llm=adapter_model(extract), embed=fake_embed)
    try:
        a.reset(tmp_path / "w")
        assert a.context("What do you know about me?", "typed", "p").item_ids == []
        a.seed([data.seed_view(ds.facts["f0001"])])
        assert a.cost()["model_calls"] == 0 and a.cost()["embed_calls"] >= 1
        s = ds.session_by_id["s0002"]  # ends 2026-03-03 18:06 -08:00 = 2026-03-04 02:06 UTC
        a.set_clock(data.epoch(s["end_ts"]))
        a.ingest(data.session_view(s, ds.device_names))
        assert a.cost()["model_calls"] == 1
        assert "## Observation Date\n2026-03-04" in prompts[0]  # Mem0 dates in UTC, from the benchmark clock
        assert "user: Someone nearby: can you turn the oven off" in prompts[0]
        assert "bystander" not in prompts[0]
        stored = {m["memory"]: m for m in a.dump()}
        assert stored["her favorite place is lantern noodle house"]["created_at"].startswith("2026-03-04T02:06")
        assert stored["her favorite place is lantern noodle house"]["metadata"]["channel"] == "voice"
        r = a.context("which noodle place does my sister like", "remote", "p")
        assert r.text.startswith("- ") and "lantern noodle house" in r.text and len(r.item_ids) == r.text.count("\n") + 1
        a.close()
        shutil.copytree(tmp_path / "w", tmp_path / "snap")
        a.reopen()
        a.ingest(data.session_view(ds.session_by_id["s0003"], ds.device_names))
        a.close()
        shutil.rmtree(tmp_path / "w")
        shutil.copytree(tmp_path / "snap", tmp_path / "w")
        a.reopen()
        assert not any("dentist" in m["memory"] for m in a.dump())
    finally:
        a.close()
    sam = {"speaker": "p010", "speaker_name": "Samir Haddad", "text": "hi"}
    assert turn_message(sam) == {"role": "user", "content": "Samir Haddad: hi", "name": "Samir Haddad"}
    assert turn_message({"speaker": "principal", "speaker_name": "Joana Reis", "text": "hi"}) == \
        {"role": "user", "content": "hi"}  # the principal stays Mem0's user
    assert turn_message({"speaker": "assistant", "speaker_name": "Assistant", "text": "ok"})["role"] == "assistant"
    assert turn_message({"speaker": "Caroline", "text": "hi"})["content"] == "Caroline: hi"  # external suites


def test_zep_graphiti_end_to_end(run_adapter):
    pytest.importorskip("graphiti_core")
    pytest.importorskip("kuzu")
    code, recs, summary, _ = run_adapter("zep_graphiti", graphiti_model)
    assert code == 0 and len(recs) == 16
    assert not [r["error"] for r in recs.values() if r["error"]]
    r = recs["b-retrieval-0002"]
    assert "<FACTS>" in r["context"]["text"] and "lantern noodle house" in r["context"]["text"].lower()
    assert r["passed"]
    assert any("oat milk cortado" in r["context"]["text"] for r in recs.values() if r["level"] == "L1")  # bulk seeds
    for r in recs.values():
        assert all(re.fullmatch(r"[0-9a-f-]{36}", i) for i in r["context"]["item_ids"])
    assert summary["cost"]["system_total"]["model_calls"] > 0


def test_zep_graphiti_unit_episode_times_and_snapshot(tmp_path):
    pytest.importorskip("graphiti_core")
    import shutil
    from harness import data
    from harness.adapters.zep_graphiti import ZepGraphiti, context_block, fact_json, transcript
    ds = data.load(FIX)
    seen = []

    def extract(model, system, prompt, schema):
        seen.append((schema.get("title"), prompt))
        return graphiti_model(model, system, prompt, schema)
    a = ZepGraphiti(llm=adapter_model(extract), embed=fake_embed)
    try:
        a.reset(tmp_path / "w")
        assert a.context("What do you know about me?", "typed", "p").item_ids == []
        s = ds.session_by_id["s0002"]
        a.ingest(data.session_view(s, ds.device_names))
        assert any("2026-03-03T18:00:00-08:00" in p for _, p in seen)  # reference time = session start
        assert any("user: so my sister juno is coming next month" in p for _, p in seen)
        assert any("Someone nearby: can you turn the oven off" in p for _, p in seen)
        assert not any("bystander:" in p for _, p in seen)
        r = a.context("which noodle place does my sister like", "remote", "p")
        assert "lantern noodle house" in r.text and "(" in r.text and "present)" in r.text
        assert len(r.item_ids) >= 1
        a.close()
        shutil.copytree(tmp_path / "w", tmp_path / "snap")
        a.reopen()
        a.ingest(data.session_view(ds.session_by_id["s0003"], ds.device_names))
        assert "dentist" in a.context("dentist appointment", "typed", "p").text.lower()
        a.close()
        shutil.rmtree(tmp_path / "w")
        shutil.copytree(tmp_path / "snap", tmp_path / "w")
        a.reopen()
        assert "dentist" not in a.context("dentist appointment", "typed", "p").text.lower()
    finally:
        a.close()
    assert transcript({"turns": [{"speaker": "principal", "speaker_name": "Joana Reis", "text": "a"},
                                 {"speaker": "p9", "speaker_name": "Samir Haddad", "text": "b"},
                                 {"speaker": "assistant", "speaker_name": "Assistant", "text": "c"}]}) == \
        "user: a\nSamir Haddad: b\nassistant: c"
    assert transcript({"turns": [{"speaker": "Caroline", "text": "a"}]}) == "Caroline: a"  # external suites
    assert "<ENTITIES>\n\n</ENTITIES>" in context_block([], [])
    rec = json.loads(fact_json(data.seed_view(ds.facts["f0001"])))
    assert rec["subject"] == "user" and rec["predicate"] == "usual coffee order" and rec["value"] == "oat milk cortado"


def need_claude_mem():
    from harness.adapters import claude_mem
    if not (claude_mem.HOME / ".local" / "bin" / "claude-mem").exists():
        pytest.skip("claude-mem is not installed under results/.cache/claude-mem/home")


def claude_mem_model(model, system, prompt, schema):
    """claude-mem's observer: a summary for a summary request, an observation naming the tool output otherwise."""
    last = prompt.rsplit("User:", 1)[-1]
    if "PROGRESS SUMMARY" in last:
        said = re.findall(r"Claude's Full Response to User:\n(.*)", last)
        return (f"<summary><request>Talked: {said[0][:60] if said else ''}</request><investigated></investigated>"
                "<learned>Learned it.</learned><completed></completed><next_steps></next_steps><notes></notes>"
                "</summary>")
    out = re.findall(r"<outcome>(.*?)</outcome>", last, re.S)
    title = re.sub(r"[^A-Za-z0-9 ]", " ", out[-1])[:80].strip() if out else "Tool output"
    return (f"<observation><type>discovery</type><title>{title}</title><subtitle></subtitle><facts></facts>"
            "<narrative>Seen.</narrative><concepts><concept>how-it-works</concept></concepts><files_read></files_read>"
            "<files_modified></files_modified></observation>")


def test_claude_mem_turn_events():
    from harness.adapters.claude_mem import turn_events
    t = lambda sp, text, name=None: {"ts": "2026-03-01T10:00:00-08:00", "speaker": sp, "text": text,
                                     "speaker_name": name or sp}
    typed = {"channel": "voice", "turns": [t("principal", "a"), t("principal", "b"), t("bystander", "c", "Someone nearby"),
                                           t("assistant", "d"), t("assistant", "e"), t("principal", "f")]}
    assert [e[:2] for e in turn_events(typed)] == [("prompt", "a\nb\nSomeone nearby: c"), ("reply", "d\n\ne"),
                                                    ("prompt", "f")]
    text = {"channel": "inbound_text", "turns": [t("p001", "hi", "Sam"), t("principal", "yo", "Joana Reis")]}
    assert [e[:2] for e in turn_events(text)] == [("tool", "Sam: hi\nJoana Reis: yo")]
    web = {"channel": "web", "turns": [t("principal", "read this"), t("web", "page", "site.example"),
                                       t("assistant", "done")]}
    assert [e[0] for e in turn_events(web)] == ["prompt", "tool", "reply"]


def test_claude_mem_end_to_end(run_adapter):
    need_claude_mem()
    code, recs, summary, proxies = run_adapter("claude_mem", claude_mem_model, "--levels", "L2,L3")
    assert code == 0 and recs
    assert not [r["error"] for r in recs.values() if r["error"]]
    texts = [r["context"]["text"] for r in recs.values()]
    assert any("Talked:" in x for x in texts)  # session summaries reach the SessionStart block
    for r in recs.values():
        ids = r["context"]["item_ids"]
        assert all(re.fullmatch(r"S?\d+", i) for i in ids)
        assert len(ids) == len(set(ids))
    # one summary call per assistant turn, one observation call per tool event: 4 replies + 1 email in each replay
    assert summary["cost"]["system_total"]["model_calls"] >= 5


def test_competitors_are_loadable_by_name():
    need_mem0()
    pytest.importorskip("graphiti_core")
    from harness.adapter import load_adapter
    assert load_adapter("mem0").name == "mem0"
    assert load_adapter("zep_graphiti").name == "zep_graphiti"


# --- one real call -------------------------------------------------------------------------------------------------

@pytest.mark.slow
def test_proxy_with_the_real_model_and_embedder(tmp_path):
    p = LLMProxy(cache_dir=tmp_path / "cache")  # fresh cache: a real call
    try:
        client = openai.OpenAI(base_url=p.start(), api_key="unused")
        schema = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
        r = client.chat.completions.create(
            model="gpt-4.1-mini", messages=[{"role": "system", "content": "Extract the city the user lives in."},
                                            {"role": "user", "content": "I moved to Lisbon last spring."}],
            response_format={"type": "json_schema", "json_schema": {"name": "City", "schema": schema}})
        assert json.loads(r.choices[0].message.content)["city"].lower() == "lisbon"
        e = client.embeddings.create(model="bge", input=["my sister is vegetarian", "what can my sibling not eat?",
                                                         "the car needs new tires"])
        v = np.array([d.embedding for d in e.data])
        assert v.shape == (3, 384)
        assert v[0] @ v[1] > v[2] @ v[1]
        c = p.cost()
        assert c["model_calls"] == 1 and c["input_tokens"] > 0 and c["embedded_texts"] == 3
    finally:
        p.stop()
