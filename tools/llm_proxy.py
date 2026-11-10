"""A local OpenAI-compatible server, so third-party memory systems run on the benchmark's own brains.

Competitor libraries (Mem0, Graphiti) speak the OpenAI API. This server answers that API on 127.0.0.1 with:

    POST /v1/chat/completions   one isolated `claude -p` call through harness/llm.py (same isolation recipe and
                                disk cache as every other model call in the benchmark)
    POST /v1/embeddings         BAAI/bge-small-en-v1.5 on CPU through fastembed (the model naive_rag uses)
    GET  /v1/models             the one model it serves

Chat requests: system (and developer) messages become the system prompt, followed by the harness's isolation
sentence (harness/prompts.py: runtime details the CLI attaches are not about the user). The other messages become the
prompt: a lone user message verbatim, otherwise a transcript with one labelled block per message. Whatever model
name is asked for is served by `model` (default claude-sonnet-5-5). `response_format` json_schema passes the schema
as --json-schema and returns the structured output, serialised, as the message content. json_object adds one sentence
to the system prompt (JSON_MODE: answer with one JSON object only) and returns the JSON object found in the reply; a
bare {"type": "object"} schema is not used because the CLI then tends to encode nested values as JSON strings. `tools` (or legacy `functions`) are mapped to one
JSON schema whose `call` is one of the offered functions' argument objects (or, when the model may decline, a plain
reply); a chosen function comes back as a tool_calls message. `stream: true` gets the finished completion as
server-sent events: one chunk with the whole message, a usage chunk if stream_options.include_usage, then [DONE].

Embedding requests: input is a string or a list of strings; the response is float lists or, when the client asks for
encoding_format=base64 (the OpenAI Python client does by default), little-endian float32 bytes in base64. The model
cannot tell queries from passages through this API, so every text is embedded as a passage (no query prefix).

Counting: cost() returns, since the last reset_stats(), the chat requests served (`calls`), how many reached the
model (`model_calls`; the rest were answered from the disk cache), the input and output tokens the model reported for
those, failures, and the embedding requests and texts.

    proxy = LLMProxy()            # or LLMProxy(llm=FakeLLM(...), embed=fake_embed) in tests
    base_url = proxy.start()      # "http://127.0.0.1:<port>/v1"
    ...
    proxy.stop()
"""
import base64
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from harness import llm as llm_mod
from harness.data import REPO
from harness.prompts import _ISOLATION

DEFAULT_MODEL = llm_mod.DEFAULT_ANSWERER  # claude-sonnet-5-5
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
# How many model calls run at once. Graphiti fans out up to 20 coroutines; each call is a `claude` process, so the
# proxy queues beyond this many. It changes wall time only, never what a system is asked.
DEFAULT_CONCURRENCY = int(os.environ.get("MEMBENCH_PROXY_CONCURRENCY", "8"))


class ProxyError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _text(content):
    """Message content as plain text (a string, or a list of content parts)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict) and part.get("type") in ("text", "input_text"):
                out.append(part.get("text", ""))
        return "\n".join(out)
    return str(content)


def split_messages(messages):
    """OpenAI chat messages -> (system prompt, prompt) for one print-mode call."""
    system = [_text(m.get("content")) for m in messages if m.get("role") in ("system", "developer")]
    rest = [m for m in messages if m.get("role") not in ("system", "developer")]
    if len(rest) == 1 and rest[0].get("role") == "user":
        prompt = _text(rest[0].get("content"))
    else:
        blocks = []
        for m in rest:
            role = m.get("role", "user")
            label = {"user": "User", "assistant": "Assistant", "tool": "Tool result", "function": "Function result"}
            head = label.get(role, role.capitalize())
            if m.get("name"):
                head += f" ({m['name']})"
            body = _text(m.get("content"))
            for call in m.get("tool_calls") or []:
                fn = call.get("function", {})
                body += f"\n[called {fn.get('name')} with {fn.get('arguments')}]"
            blocks.append(f"{head}:\n{body.strip()}")
        prompt = "\n\n".join(blocks)
    sys_text = "\n\n".join(s for s in system if s.strip())
    sys_text = (sys_text + "\n\n" + _ISOLATION) if sys_text else _ISOLATION
    return sys_text, prompt


def _functions(body):
    """The functions a request offers and whether the model must call one: ([function dicts], forced)."""
    funcs = [t["function"] for t in body.get("tools") or [] if t.get("type", "function") == "function"
             and "function" in t]
    funcs += list(body.get("functions") or [])
    choice = body.get("tool_choice", body.get("function_call", "auto"))
    if not funcs or choice == "none":
        return [], False
    if isinstance(choice, dict):
        name = (choice.get("function") or choice).get("name")
        funcs = [f for f in funcs if f.get("name") == name] or funcs
        return funcs, True
    return funcs, choice == "required"


def tool_schema(funcs, forced):
    """One JSON schema for a choice among functions: {"call": {"tool": name, "arguments": {...}} | {"reply": ...}}."""
    branches = []
    for f in funcs:
        params = f.get("parameters") or {"type": "object", "properties": {}}
        branches.append({"type": "object", "properties": {"tool": {"const": f["name"]}, "arguments": params},
                         "required": ["tool", "arguments"]})
    if not forced:
        branches.append({"type": "object", "properties": {"tool": {"const": "none"}, "reply": {"type": "string"}},
                         "required": ["tool", "reply"]})
    return {"type": "object", "properties": {"call": {"anyOf": branches}}, "required": ["call"]}


def tool_instructions(funcs, forced):
    lines = ["You can call one of these functions. Return the call as structured output: `call.tool` is the "
             "function name and `call.arguments` its arguments."]
    if not forced:
        lines.append("If no function fits, set `call.tool` to \"none\" and put your reply in `call.reply`.")
    for f in funcs:
        lines.append(f"- {f['name']}: {f.get('description', '')}\n  parameters: "
                     f"{json.dumps(f.get('parameters') or {}, sort_keys=True)}")
    return "\n".join(lines)


JSON_MODE = "Respond with one valid JSON object and nothing else: no prose, no code fence."


def extract_json_object(text):
    """The JSON object in a reply (OpenAI's json_object mode returns only JSON): the whole text if it parses, else
    the outermost {...} span that does. Unparseable text is returned as is, for the client to report."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        s = s.rsplit("```", 1)[0].strip()
    for cand in (s, s[s.find("{"): s.rfind("}") + 1] if "{" in s else ""):
        try:
            if isinstance(json.loads(cand), dict):
                return cand
        except ValueError:
            pass
    return text


def _approx_tokens(s):
    return max(1, len(s) // 4)


def stream_chunk(out, empty=False):
    """One chat.completion.chunk carrying a whole completion's message (or nothing, for the usage chunk)."""
    choices = []
    if not empty:
        c = out["choices"][0]
        delta = {"role": "assistant", "content": c["message"].get("content")}
        if c["message"].get("tool_calls"):
            delta["tool_calls"] = [{**tc, "index": i} for i, tc in enumerate(c["message"]["tool_calls"])]
        choices = [{"index": 0, "delta": delta, "finish_reason": c["finish_reason"], "logprobs": None}]
    return {"id": out["id"], "object": "chat.completion.chunk", "created": out["created"], "model": out["model"],
            "choices": choices}


class LLMProxy:
    def __init__(self, llm=None, model=DEFAULT_MODEL, embed=None, cache_dir=None, concurrency=DEFAULT_CONCURRENCY,
                 model_map=None):
        self.llm = llm if llm is not None else llm_mod.LLM(cache_dir or REPO / "results" / ".cache" / "llm")
        self.model = model
        self.model_map = dict(model_map or {})
        self._embed = embed
        self._embed_lock = threading.Lock()
        self._sem = threading.BoundedSemaphore(max(1, concurrency))
        self._lock = threading.Lock()
        self._server = None
        self._thread = None
        self.base_url = None
        self._zero()

    # --- lifecycle ------------------------------------------------------------------------------------------------

    def start(self):
        if self._server is not None:
            return self.base_url
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, status, obj):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_stream(self, out, include_usage):
                """A finished completion as server-sent events: the whole message in one chunk, then [DONE]."""
                events = [stream_chunk(out)]
                if include_usage:
                    events.append({**stream_chunk(out, empty=True), "usage": out["usage"]})
                data = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
                data = data.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.rstrip("/").endswith("/models"):
                    self._send(200, {"object": "list", "data": [{"id": proxy.model, "object": "model",
                                                                 "owned_by": "memory-bench"}]})
                else:
                    self._send(404, {"error": {"message": f"no route {self.path}", "type": "not_found"}})

            def do_POST(self):
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n) or b"{}")
                    path = self.path.split("?", 1)[0].rstrip("/")
                    if path.endswith("/chat/completions"):
                        out = proxy.chat(body)
                        if body.get("stream"):
                            self._send_stream(out, (body.get("stream_options") or {}).get("include_usage"))
                        else:
                            self._send(200, out)
                    elif path.endswith("/embeddings"):
                        self._send(200, proxy.embeddings(body))
                    else:
                        raise ProxyError(404, f"no route {self.path}")
                except ProxyError as e:
                    self._send(e.status, {"error": {"message": str(e), "type": "invalid_request_error"}})
                except llm_mod.LLMError as e:
                    self._send(500, {"error": {"message": str(e), "type": "server_error"}})
                except Exception as e:  # never kill the server thread
                    self._send(500, {"error": {"message": f"{type(e).__name__}: {e}", "type": "server_error"}})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="llm-proxy", daemon=True)
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}/v1"
        return self.base_url

    def stop(self):
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        self._server = self._thread = self.base_url = None

    # --- counting -------------------------------------------------------------------------------------------------

    def _zero(self):
        self.counts = {"calls": 0, "embed_calls": 0, "embedded_texts": 0}
        self._llm0 = dict(getattr(self.llm, "stats", {}) or {})

    def reset_stats(self):
        with self._lock:
            self._zero()

    def cost(self):
        with self._lock:
            out = dict(self.counts)
            now = dict(getattr(self.llm, "stats", {}) or {})
        for src, dst in (("calls", "model_calls"), ("cache_hits", "cache_hits"), ("failures", "failures"),
                         ("input_tokens", "input_tokens"), ("output_tokens", "output_tokens")):
            out[dst] = now.get(src, 0) - self._llm0.get(src, 0)
        return out

    # --- endpoints ------------------------------------------------------------------------------------------------

    def chat(self, body):
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ProxyError(400, "messages must be a non-empty list")
        system, prompt = split_messages(messages)
        model = self.model_map.get(body.get("model"), self.model)
        funcs, forced = _functions(body)
        rf = body.get("response_format") or {}
        if funcs:
            schema = tool_schema(funcs, forced)
            system = system + "\n\n" + tool_instructions(funcs, forced)
        elif rf.get("type") == "json_schema":
            schema = (rf.get("json_schema") or {}).get("schema")
            if not isinstance(schema, dict):
                raise ProxyError(400, "response_format json_schema needs json_schema.schema")
        elif rf.get("type") == "json_object":
            # A bare {"type": "object"} schema makes the CLI's structured output wrap nested values as JSON strings,
            # so JSON mode is a plain call told to answer with one JSON object, which is then extracted.
            schema = None
            system = system + "\n\n" + JSON_MODE
        else:
            schema = None
        with self._lock:
            self.counts["calls"] += 1
        with self._sem:
            out = self.llm.complete(model, system, prompt, schema)

        message = {"role": "assistant", "content": None}
        finish = "stop"
        if funcs:
            call = out.get("call") if isinstance(out, dict) else None
            if not isinstance(call, dict) or call.get("tool") in (None, "none"):
                message["content"] = (call or {}).get("reply", "") if isinstance(call, dict) else json.dumps(out)
            else:
                args = call.get("arguments", {})
                message["tool_calls"] = [{"id": f"call_{uuid.uuid4().hex[:24]}", "type": "function",
                                          "function": {"name": call["tool"], "arguments": json.dumps(args)}}]
                if body.get("functions") and not body.get("tools"):
                    message["function_call"] = message["tool_calls"][0]["function"]
                finish = "tool_calls"
        elif schema is not None:
            message["content"] = json.dumps(out, ensure_ascii=False)
        elif rf.get("type") == "json_object":
            message["content"] = extract_json_object(out)
        else:
            message["content"] = out
        p_tok = _approx_tokens(system + prompt)
        c_tok = _approx_tokens(message["content"] or json.dumps(message.get("tool_calls")))
        return {"id": f"chatcmpl-{uuid.uuid4().hex}", "object": "chat.completion", "created": int(time.time()),
                "model": body.get("model") or model,
                "choices": [{"index": 0, "message": message, "finish_reason": finish, "logprobs": None}],
                "usage": {"prompt_tokens": p_tok, "completion_tokens": c_tok, "total_tokens": p_tok + c_tok}}

    @property
    def embedder(self):
        if self._embed is None:
            from harness.adapters.naive_rag import FastEmbedder
            self._embed = FastEmbedder(EMBED_MODEL)
        return self._embed

    def embed(self, texts):
        with self._embed_lock:  # one ONNX session; calls are short
            v = np.asarray(self.embedder(list(texts), "passage"), dtype=np.float32)
        return v

    def embeddings(self, body):
        inp = body.get("input")
        texts = [inp] if isinstance(inp, str) else inp
        if not isinstance(texts, list) or not texts or not all(isinstance(t, str) for t in texts):
            raise ProxyError(400, "input must be a string or a non-empty list of strings")
        vecs = self.embed(texts)
        with self._lock:
            self.counts["embed_calls"] += 1
            self.counts["embedded_texts"] += len(texts)
        b64 = body.get("encoding_format") == "base64"
        data = []
        for i, v in enumerate(vecs):
            emb = base64.b64encode(v.astype("<f4").tobytes()).decode() if b64 else [float(x) for x in v]
            data.append({"object": "embedding", "index": i, "embedding": emb})
        tok = sum(_approx_tokens(t) for t in texts)
        return {"object": "list", "data": data, "model": body.get("model") or EMBED_MODEL,
                "usage": {"prompt_tokens": tok, "total_tokens": tok}}
