"""The only module that calls a model.

Calls go to the `claude` CLI in print mode with an isolation recipe, so no user settings, hooks, MCP servers,
tools or saved sessions reach the model:

    claude -p --model MODEL --no-session-persistence --strict-mcp-config --tools "" --setting-sources ""
           --disable-slash-commands --system-prompt SYSTEM --output-format json [--json-schema SCHEMA]

with the prompt on stdin and the working directory a fresh temporary directory. The child gets the parent's
environment minus variables that carry the operator's identity or session (see _SCRUB). The CLI still attaches
the logged-in account's email, the machine and today's date; no flag removes them, so every system prompt in
harness/prompts.py tells the model that information is not about the user, and the slow test in
tests/test_slow.py checks the answering model shows no knowledge of any user. If MEMBENCH_CLAUDE_OAUTH_TOKEN is
set (a token from `claude setup-token`), the child runs with that token and an empty temporary config directory
instead of the operator's login.

Setting MEMBENCH_LLM_BACKEND=api sends the same system prompt and prompt to the Anthropic Messages API instead,
through the official `anthropic` SDK, which reads ANTHROPIC_API_KEY from the environment; no Claude Code install is
needed. A JSON schema becomes a structured-output request (output_config.format), so the result is the same dict the
CLI returns as structured_output. Nothing is attached on this path (no account, machine or date), so the isolation
sentence in the prompts is harmless there. The API is billed per token at API_PRICES; the CLI draws on a Claude
subscription's plan limits. The API backend is unit-tested against a fake client in tests/test_llm.py and not yet
live-tested; a maintainer with a key live-tests it with:

    MEMBENCH_LLM_BACKEND=api uv run python -c "from harness.llm import LLM; print(LLM('/tmp/llmcache').complete('claude-haiku-5-5', 'Be brief.', 'Say hi.'))"

Responses are cached on disk under results/.cache/llm keyed by (model, system, prompt, schema, nonce), so reruns
are cheap and deterministic. A different nonce forces a fresh call (used to measure judge agreement). The key does
not include the backend, so both backends share one cache; each entry records the backend that produced it.
"""
import hashlib
import json
import re
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import anthropic

DEFAULT_ANSWERER = "claude-sonnet-5-5"
DEFAULT_JUDGE = "claude-opus-5-5"
TIMEOUT_S = 600
RETRIES = 2
BACKENDS = ("cli", "api")
# Thinking is on for the default models and counts against max_tokens, so this leaves room well beyond the
# longest expected output (a few sentences of answer or a judge's short reason).
API_MAX_TOKENS = 16000
# Messages API prices in USD per million tokens (input, output, cache read), from the claude-api skill's model
# table cached 2026-10-06. Cache writes bill at 1.25x input; the harness sends no cache_control, so both cache
# counts are normally zero. Haiku 5.5's rates hold for prompts up to 100K tokens ($0.50 / $2.50 above) and its
# cache-read rate is the usual 0.1x input. A model not listed costs 0.0.
API_PRICES = {
    "claude-fable-5-1": (10.00, 50.00, 0.25),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}

_SCRUB_PREFIXES = ("CLAUDE_CODE_", "CLAUDE_AGENT_SDK", "CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT",
                   "CLAUDE_PREVIEW_", "OTEL_")
# Kept although they match a prefix: they decide how the child reaches the API, not who the user is.
_KEEP = {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"}


class LLMError(RuntimeError):
    pass


def command(model, system, schema=None):
    cmd = ["claude", "-p", "--model", model, "--no-session-persistence", "--strict-mcp-config", "--tools", "",
           "--setting-sources", "", "--disable-slash-commands", "--system-prompt", system, "--output-format", "json"]
    if schema is not None:
        cmd += ["--json-schema", json.dumps(schema, sort_keys=True)]
    return cmd


def child_env(config_dir=None):
    env = {k: v for k, v in os.environ.items()
           if k in _KEEP or not k.startswith(_SCRUB_PREFIXES)}
    token = os.environ.get("MEMBENCH_CLAUDE_OAUTH_TOKEN")
    if token and config_dir:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


def api_cost(model, input_tokens, output_tokens, cache_creation, cache_read):
    if model not in API_PRICES:
        return 0.0
    inp, out, read = API_PRICES[model]
    return (input_tokens * inp + cache_creation * inp * 1.25 + cache_read * read + output_tokens * out) / 1e6


def api_schema(schema):
    """The schema as structured outputs accept it. The SDK's transform_schema sets additionalProperties false on
    every object and moves unsupported constraints into descriptions, which schemas from other projects (through
    tools/llm_proxy.py) need. One it cannot read (an enum with no type, as in prompts.VERDICT_SCHEMA) is sent
    unchanged."""
    try:
        return anthropic.transform_schema(schema)
    except (ValueError, TypeError):
        return schema


def cache_key(model, system, prompt, schema=None, nonce=""):
    blob = json.dumps([model, system, prompt, schema, nonce], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


TRANSIENT = re.compile(r"Can't reach the API server|ENOTFOUND|ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|socket hang up|"
                       r"network error|usage_limit_reached|rate.?limit|\b429\b|overloaded|Service Unavailable|"
                       r"Bad Gateway|Gateway Timeout|usage.?limit|weekly.?limit|hit your [\w-]* ?limit|"
                       r"limit (?:will )?resets?|\bresets\b", re.I)
# A plan's usage cap (5-hour or weekly) is waited out like an outage; MEMBENCH_TRANSIENT_MAX_WAIT (seconds) lets a long
# run sit through a cap that resets hours later instead of failing its calls.
TRANSIENT_MAX_WAIT = int(os.environ.get("MEMBENCH_TRANSIENT_MAX_WAIT") or 2 * 3600)
TRANSIENT_STEPS = (30, 60, 120, 300, 600)


class LLM:
    def __init__(self, cache_dir, run=subprocess.run, timeout=TIMEOUT_S, retries=RETRIES, backoff=5.0,
                 client=None):
        self._lock = threading.Lock()  # the runner calls complete() from several threads (--jobs)
        self.cache_dir = Path(cache_dir)
        self.run, self.timeout, self.retries, self.backoff = run, timeout, retries, backoff
        self.backend = os.environ.get("MEMBENCH_LLM_BACKEND") or "cli"
        if self.backend not in BACKENDS:
            raise ValueError(f"MEMBENCH_LLM_BACKEND must be one of {BACKENDS}, not {self.backend!r}")
        self.client = client  # an object with messages.create; built on the first API call when None
        self.stats = {"calls": 0, "cache_hits": 0, "failures": 0, "input_tokens": 0, "output_tokens": 0,
                      "cost_usd": 0.0}

    def _path(self, key):
        return self.cache_dir / key[:2] / f"{key}.json"

    def complete(self, model, system, prompt, schema=None, nonce=""):
        """Return the model's text, or its structured output (a dict) when a JSON schema is given."""
        key = cache_key(model, system, prompt, schema, nonce)
        path = self._path(key)
        if path.exists():
            with self._lock:
                self.stats["cache_hits"] += 1
            return json.loads(path.read_text())["output"]
        last, attempt, waited, outages = None, 0, 0, 0
        while attempt <= self.retries:
            try:
                out, usage = self._call(model, system, prompt, schema)
            except (LLMError, subprocess.TimeoutExpired, OSError, ValueError) as e:
                last = e
                with self._lock:
                    self.stats["failures"] += 1
                # A network outage or rate limit is waited out (up to TRANSIENT_MAX_WAIT) without using up a retry,
                # so a run never records a pile of errors for a few minutes without internet.
                if TRANSIENT.search(str(e)) and waited < TRANSIENT_MAX_WAIT and self.backoff:
                    pause = TRANSIENT_STEPS[min(outages, len(TRANSIENT_STEPS) - 1)]
                    time.sleep(pause)
                    waited, outages = waited + pause, outages + 1
                    continue
                attempt += 1
                if attempt <= self.retries and self.backoff:
                    time.sleep(self.backoff * attempt)
                continue
            with self._lock:
                self.stats["calls"] += 1
                self.stats["input_tokens"] += usage["input_tokens"]
                self.stats["output_tokens"] += usage["output_tokens"]
                self.stats["cost_usd"] += usage["cost_usd"]
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps({"model": model, "system": system, "prompt": prompt, "schema": schema,
                                       "nonce": nonce, "output": out, "usage": usage, "backend": self.backend},
                                      ensure_ascii=False))
            tmp.replace(path)
            return out
        raise LLMError(f"{model}: failed after {self.retries + 1} attempts: {last}")

    def _call(self, model, system, prompt, schema):
        if self.backend == "api":
            return self._call_api(model, system, prompt, schema)
        with tempfile.TemporaryDirectory(prefix="membench-llm-") as cwd:
            cfg = Path(cwd) / ".config"
            cfg.mkdir()
            proc = self.run(command(model, system, schema), input=prompt, capture_output=True, text=True,
                            timeout=self.timeout, cwd=cwd, env=child_env(cfg))
        if proc.returncode != 0:
            raise LLMError(f"exit {proc.returncode}: {(proc.stderr or '')[-500:]} {(proc.stdout or '')[-500:]}")
        data = json.loads(proc.stdout)
        if data.get("is_error"):
            raise LLMError(f"error result: {str(data.get('result'))[:500]}")
        if schema is not None:
            out = data.get("structured_output")
            if not isinstance(out, dict):
                raise LLMError("no structured_output in the response")
        else:
            out = data.get("result")
            if not isinstance(out, str):
                raise LLMError("no result text in the response")
        u = data.get("usage") or {}
        usage = {"input_tokens": int(u.get("input_tokens", 0)) + int(u.get("cache_creation_input_tokens", 0))
                 + int(u.get("cache_read_input_tokens", 0)),
                 "output_tokens": int(u.get("output_tokens", 0)),
                 "cost_usd": float(data.get("total_cost_usd") or 0.0)}
        return out, usage

    def _call_api(self, model, system, prompt, schema):
        kw = {}
        if schema is not None:
            kw["output_config"] = {"format": {"type": "json_schema", "schema": api_schema(schema)}}
        try:
            with self._lock:
                if self.client is None:
                    self.client = anthropic.Anthropic(timeout=self.timeout)
            resp = self.client.messages.create(model=model, max_tokens=API_MAX_TOKENS, system=system,
                                               messages=[{"role": "user", "content": prompt}], **kw)
        except anthropic.APIStatusError as e:
            # Status, reason and error type (rate_limit_error, overloaded_error) are what TRANSIENT matches.
            reason = getattr(e.response, "reason_phrase", "") or ""
            msg = f"API error {e.status_code} {reason} {e.type}: {e.api_message or e.message}"
            raise LLMError(msg[:600]) from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"network error: {e.message}") from e
        except (anthropic.AnthropicError, TypeError) as e:  # the SDK raises TypeError when it finds no credentials
            raise LLMError(f"API client error: {e}") from e
        if resp.stop_reason in ("refusal", "max_tokens"):
            raise LLMError(f"stop_reason {resp.stop_reason}")
        texts = [b.text for b in resp.content if b.type == "text"]
        if not texts:
            raise LLMError("no text in the response")
        out = "".join(texts)
        if schema is not None:
            try:
                out = json.loads(out)
            except ValueError:
                out = None
            if not isinstance(out, dict):
                raise LLMError("no structured output in the response")
        u = resp.usage
        created = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
        read = int(getattr(u, "cache_read_input_tokens", 0) or 0)
        inp, outp = int(u.input_tokens or 0), int(u.output_tokens or 0)
        usage = {"input_tokens": inp + created + read, "output_tokens": outp,
                 "cost_usd": api_cost(model, inp, outp, created, read)}
        return out, usage


class FakeLLM:
    """Stand-in for tests: fn(model, system, prompt, schema) -> str | dict."""

    def __init__(self, fn):
        self.fn = fn
        self.stats = {"calls": 0, "cache_hits": 0, "failures": 0, "input_tokens": 0, "output_tokens": 0,
                      "cost_usd": 0.0}
        self.log = []

    def complete(self, model, system, prompt, schema=None, nonce=""):
        self.stats["calls"] += 1
        self.log.append((model, system, prompt, schema, nonce))
        return self.fn(model, system, prompt, schema)
