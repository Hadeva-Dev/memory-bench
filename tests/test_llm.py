"""harness/llm.py: the only module that calls a model. Tested with a fake subprocess runner."""
import json
import subprocess

import pytest

from harness import llm


@pytest.fixture(autouse=True)
def cli_backend_by_default(monkeypatch):
    monkeypatch.delenv("MEMBENCH_LLM_BACKEND", raising=False)


class FakeProc:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def ok(result="hi", structured=None):
    d = {"type": "result", "is_error": False, "result": result,
         "usage": {"input_tokens": 3, "cache_creation_input_tokens": 10, "cache_read_input_tokens": 2,
                   "output_tokens": 5}, "total_cost_usd": 0.01}
    if structured is not None:
        d["structured_output"] = structured
    return FakeProc(json.dumps(d))


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_command_uses_the_isolation_recipe(tmp_path):
    rec = Recorder(ok("hello"))
    client = llm.LLM(tmp_path / "cache", run=rec)
    assert client.complete("m1", "SYS", "PROMPT") == "hello"
    cmd, kw = rec.calls[0]
    assert cmd[:2] == ["claude", "-p"]
    for flag in ["--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands"]:
        assert flag in cmd
    assert cmd[cmd.index("--model") + 1] == "m1"
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert cmd[cmd.index("--system-prompt") + 1] == "SYS"
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert "--json-schema" not in cmd
    assert kw["input"] == "PROMPT" and kw["timeout"] == 600
    assert kw["cwd"] and "cache" not in kw["cwd"]
    assert "CLAUDE_CODE_USER_EMAIL" not in kw["env"]


def test_schema_returns_structured_output(tmp_path):
    rec = Recorder(ok("{}", structured={"verdict": "correct"}))
    client = llm.LLM(tmp_path, run=rec)
    schema = {"type": "object"}
    assert client.complete("m", "s", "p", schema=schema) == {"verdict": "correct"}
    cmd, _ = rec.calls[0]
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == schema


def test_cache_hit_makes_no_call_and_key_covers_inputs(tmp_path):
    rec = Recorder(ok("a"), ok("b"), ok("c"))
    client = llm.LLM(tmp_path, run=rec)
    assert client.complete("m", "s", "p") == "a"
    assert client.complete("m", "s", "p") == "a"
    assert llm.LLM(tmp_path, run=rec).complete("m", "s", "p") == "a"  # persists on disk
    assert client.complete("m", "s", "p", nonce="rejudge") == "b"
    assert client.complete("m2", "s", "p") == "c"
    assert len(rec.calls) == 3
    assert client.stats["calls"] == 3 and client.stats["cache_hits"] == 1
    assert client.stats["input_tokens"] == 45 and client.stats["output_tokens"] == 15


def test_retries_twice_then_raises(tmp_path):
    bad = FakeProc("", returncode=1, stderr="boom")
    rec = Recorder(bad, subprocess.TimeoutExpired("claude", 600), ok("fine"))
    client = llm.LLM(tmp_path, run=rec, backoff=0)
    assert client.complete("m", "s", "p") == "fine"
    rec2 = Recorder(bad, bad, bad)
    with pytest.raises(llm.LLMError):
        llm.LLM(tmp_path / "x", run=rec2, backoff=0).complete("m", "s", "p")
    assert len(rec2.calls) == 3


def test_error_result_and_missing_structured_output_are_failures(tmp_path):
    err = FakeProc(json.dumps({"is_error": True, "result": "overloaded"}))
    rec = Recorder(err, ok("{}"), ok("{}", structured={"x": 1}))
    client = llm.LLM(tmp_path, run=rec, backoff=0)
    assert client.complete("m", "s", "p", schema={"type": "object"}) == {"x": 1}
    assert len(rec.calls) == 3


def test_failures_are_not_cached(tmp_path):
    bad = FakeProc("", returncode=1)
    with pytest.raises(llm.LLMError):
        llm.LLM(tmp_path, run=Recorder(bad, bad, bad), backoff=0).complete("m", "s", "p")
    assert llm.LLM(tmp_path, run=Recorder(ok("now")), backoff=0).complete("m", "s", "p") == "now"


def test_fake_llm_counts_calls():
    fake = llm.FakeLLM(lambda model, system, prompt, schema: "x")
    assert fake.complete("m", "s", "p") == "x"
    assert fake.stats["calls"] == 1


def test_network_outage_is_waited_out_without_using_retries(tmp_path, monkeypatch):
    slept = []
    monkeypatch.setattr(llm.time, "sleep", slept.append)
    down = FakeProc("", returncode=1, stderr="API Error: Can't reach the API server (ENOTFOUND)")
    rec = Recorder(down, down, down, down, ok("fine"))  # four outages: more than the 2 retries
    assert llm.LLM(tmp_path, run=rec).complete("m", "s", "p") == "fine"
    assert slept == [30, 60, 120, 300]


def test_outage_longer_than_the_cap_still_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    monkeypatch.setattr(llm, "TRANSIENT_MAX_WAIT", 100)
    down = FakeProc("", returncode=1, stderr="ENOTFOUND")
    with pytest.raises(llm.LLMError):
        llm.LLM(tmp_path, run=Recorder(*([down] * 20))).complete("m", "s", "p")


# The API backend (MEMBENCH_LLM_BACKEND=api), tested with a fake client shaped like the anthropic SDK.

class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def api_ok(text="hi", stop="end_turn"):
    return Obj(content=[Obj(type="thinking", thinking=""), Obj(type="text", text=text)], stop_reason=stop,
               usage=Obj(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=100,
                         cache_read_input_tokens=50))


def sdk_error(cls, status, etype):
    import anthropic
    import httpx2
    resp = httpx2.Response(status, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    body = {"type": "error", "error": {"type": etype, "message": "slow down"}}
    return cls(f"Error code: {status}", response=resp, body=body)


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("MEMBENCH_LLM_BACKEND", "api")


def test_backend_comes_from_the_environment_and_defaults_to_cli(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMBENCH_LLM_BACKEND", raising=False)
    assert llm.LLM(tmp_path).backend == "cli"
    monkeypatch.setenv("MEMBENCH_LLM_BACKEND", "api")
    assert llm.LLM(tmp_path).backend == "api"
    monkeypatch.setenv("MEMBENCH_LLM_BACKEND", "bogus")
    with pytest.raises(ValueError):
        llm.LLM(tmp_path)


def test_api_returns_text_and_sends_the_same_system_and_prompt(tmp_path, api):
    fake = FakeClient(api_ok("hello"))
    rec = Recorder()
    assert llm.LLM(tmp_path, run=rec, client=fake).complete("claude-sonnet-5-5", "SYS", "PROMPT") == "hello"
    assert rec.calls == []  # the CLI is never run
    kw = fake.calls[0]
    assert kw["model"] == "claude-sonnet-5-5" and kw["system"] == "SYS"
    assert kw["messages"] == [{"role": "user", "content": "PROMPT"}]
    assert kw["max_tokens"] >= 4096 and "output_config" not in kw and "temperature" not in kw


def test_api_returns_a_dict_for_a_schema(tmp_path, api):
    from harness.prompts import IDS_SCHEMA, VERDICT_SCHEMA
    fake = FakeClient(api_ok('{"verdict": "correct", "reason": "r"}'), api_ok('{"revealed": [], "reason": "r"}'))
    client = llm.LLM(tmp_path, client=fake)
    assert client.complete("m", "s", "p", schema=VERDICT_SCHEMA) == {"verdict": "correct", "reason": "r"}
    assert client.complete("m", "s", "p", schema=IDS_SCHEMA["revealed"]) == {"revealed": [], "reason": "r"}
    fmt = fake.calls[0]["output_config"]["format"]
    assert fmt == {"type": "json_schema", "schema": VERDICT_SCHEMA}
    assert fake.calls[1]["output_config"]["format"]["schema"]["additionalProperties"] is False


def test_api_usage_and_cost_are_mapped(tmp_path, api):
    client = llm.LLM(tmp_path, client=FakeClient(api_ok(), api_ok()))
    client.complete("claude-sonnet-5-5", "s", "p")
    assert client.stats["input_tokens"] == 1150 and client.stats["output_tokens"] == 200
    # 1000 * $2 + 100 * $2.50 + 50 * $0.20 + 200 * $10, per million tokens
    assert client.stats["cost_usd"] == pytest.approx((2000 + 250 + 10 + 2000) / 1e6)
    client.complete("some-unpriced-model", "s", "p")
    assert client.stats["cost_usd"] == pytest.approx(4260 / 1e6)


def test_cache_is_shared_between_backends_and_records_the_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMBENCH_LLM_BACKEND", "api")
    assert llm.LLM(tmp_path, client=FakeClient(api_ok("from api"))).complete("m", "s", "p") == "from api"
    entry = json.loads(next(tmp_path.rglob("*.json")).read_text())
    assert entry["backend"] == "api" and entry["output"] == "from api"
    monkeypatch.setenv("MEMBENCH_LLM_BACKEND", "cli")
    assert llm.LLM(tmp_path, run=Recorder()).complete("m", "s", "p") == "from api"  # a hit, no CLI call


@pytest.mark.parametrize("name,status,etype", [("RateLimitError", 429, "rate_limit_error"),
                                               ("OverloadedError", 529, "overloaded_error"),
                                               ("ServiceUnavailableError", 503, "api_error")])
def test_api_errors_become_transient_llm_errors(tmp_path, api, monkeypatch, name, status, etype):
    import anthropic
    slept = []
    monkeypatch.setattr(llm.time, "sleep", slept.append)
    err = sdk_error(getattr(anthropic, name), status, etype)
    fake = FakeClient(err, api_ok("fine"))
    assert llm.LLM(tmp_path, client=fake).complete("m", "s", "p") == "fine"
    assert slept == [30]  # waited out as an outage, not counted as a retry
    with pytest.raises(llm.LLMError) as info:
        llm.LLM(tmp_path / "x", client=FakeClient(err))._call("m", "s", "p", None)
    assert str(status) in str(info.value) and llm.TRANSIENT.search(str(info.value))


def test_api_bad_request_and_refusal_are_ordinary_failures(tmp_path, api):
    import anthropic
    bad = sdk_error(anthropic.BadRequestError, 400, "invalid_request_error")
    fake = FakeClient(bad, api_ok("no", stop="refusal"), api_ok("not json"))
    with pytest.raises(llm.LLMError) as info:
        llm.LLM(tmp_path, client=fake, backoff=0).complete("m", "s", "p", schema={"type": "object"})
    assert not llm.TRANSIENT.search(str(bad)) and "no structured output" in str(info.value)
    assert len(fake.calls) == 3
