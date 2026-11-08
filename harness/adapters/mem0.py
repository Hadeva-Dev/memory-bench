"""Competitor `mem0`: Mem0 open source (`mem0ai`, `mem0.Memory`) with the benchmark's brains.

Needs the optional dependencies: `uv sync --extra competitors` (or `uv run --extra competitors ...`).

Configured as Mem0's docs describe for a self-hosted personal-assistant memory, with two swaps that keep brains
equal across systems (spec 1.6): the LLM is the `openai` provider pointed at tools/llm_proxy.py (one isolated
claude-sonnet-5-5 call per request), and the embedder is the `openai` provider pointed at the same proxy, which
serves BAAI/bge-small-en-v1.5 (384 dimensions). The vector store is Qdrant in local on-disk mode and the history
database is SQLite, both under the workdir. Telemetry is off and Mem0's home directory is results/.cache/mem0.

- seed(facts): each fact's text added with infer=False (stored as given, no LLM), metadata with its times.
- ingest(session): Mem0's normal path, add(messages, user_id="principal", metadata=..., infer=True): one LLM
  extraction call per session over all its turns. The principal's turns are `user` messages and the assistant's
  `assistant` messages; anyone else (a contact, a bystander, an email sender) is a `user` message prefixed with the
  name the device shows for them ("Sam Haddad: ...", speaker_name from harness.data.session_view), since Mem0 has
  no third-party role.
- context(): search(prompt, filters={"user_id": "principal"}) with Mem0's default top_k (20) and threshold, rendered
  one memory per line ("- text") as in Mem0's README. Mem0 has no channel scoping, so every channel gets the same.
- maintain(): nothing (Mem0 does its work on add).

Time: Mem0 OSS has no clock parameter (add(timestamp=...) is platform-only and raises). It reads the wall clock for
created_at and for the Observation Date and Current Date lines of its extraction prompt. During every Mem0 call the
adapter makes that wall clock read the benchmark clock (set_clock), which is what live use would have seen. Session
times also go into each memory's metadata. See harness/adapters/COMPETITORS.md for every choice and approximation.
"""
import contextlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from harness.adapter import Adapter, ContextResult
from harness.adapters import approx_tokens, speaker_label
from harness.data import REPO
from tools.llm_proxy import DEFAULT_MODEL, EMBED_MODEL, LLMProxy

# Must be set before mem0 is first imported: both are read at import time.
os.environ.setdefault("MEM0_TELEMETRY", "False")
os.environ.setdefault("MEM0_DIR", str(REPO / "results" / ".cache" / "mem0"))

USER_ID = "principal"
DEFAULT_TOP_K = 20  # Memory.search's default
EMBED_DIMS = 384


def turn_message(turn):
    """One session turn as a Mem0 message."""
    sp, text = turn["speaker"], turn["text"]
    if sp == "principal":
        return {"role": "user", "content": text}
    if sp == "assistant":
        return {"role": "assistant", "content": text}
    name = speaker_label(turn)
    return {"role": "user", "content": f"{name}: {text}", "name": name}


class _Clock:
    """Stands in for `datetime` inside Mem0's modules so its wall clock reads the benchmark clock."""
    now_ts = None

    @classmethod
    def install(cls):
        import mem0.configs.prompts as prompts
        import mem0.memory.main as main

        class BenchDatetime(datetime):
            @classmethod
            def now(cls_, tz=None):
                if _Clock.now_ts is None:
                    return datetime.now(tz)
                return datetime.fromtimestamp(_Clock.now_ts, tz)

            @classmethod
            def utcnow(cls_):
                return BenchDatetime.now(timezone.utc).replace(tzinfo=None)

        for mod in (prompts, main):
            if getattr(mod, "datetime", None) is datetime:
                mod.datetime = BenchDatetime

    @classmethod
    @contextlib.contextmanager
    def at(cls, ts):
        old, cls.now_ts = cls.now_ts, ts
        try:
            yield
        finally:
            cls.now_ts = old


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class Mem0Adapter(Adapter):
    name = "mem0"

    def __init__(self, llm=None, embed=None, model=DEFAULT_MODEL, top_k=DEFAULT_TOP_K):
        self.proxy = LLMProxy(llm=llm, embed=embed, model=model)
        self.model, self.top_k = model, top_k
        self.m = None
        self.now = None

    # --- lifecycle ------------------------------------------------------------------------------------------------

    def config(self):
        url = self.proxy.start()
        return {
            "llm": {"provider": "openai", "config": {"model": self.model, "openai_base_url": url,
                                                     "api_key": "unused-local-proxy"}},
            "embedder": {"provider": "openai", "config": {"model": EMBED_MODEL, "openai_base_url": url,
                                                          "api_key": "unused-local-proxy"}},
            "vector_store": {"provider": "qdrant", "config": {"collection_name": "mem0", "path": str(self.wd / "qdrant"),
                                                              "embedding_model_dims": EMBED_DIMS, "on_disk": True}},
            "history_db_path": str(self.wd / "history.db"),
        }

    def _open(self):
        import mem0.memory.main as main
        import mem0.memory.telemetry as telemetry
        from mem0 import Memory
        # In case mem0 was imported before this module set MEM0_TELEMETRY: no anonymous usage events, ever.
        telemetry.MEM0_TELEMETRY = main.MEM0_TELEMETRY = False
        _Clock.install()
        self.m = Memory.from_config(self.config())

    def reset(self, workdir):
        self._release()
        self.wd = Path(workdir)
        self.wd.mkdir(parents=True, exist_ok=True)
        self.proxy.start()
        self.proxy.reset_stats()
        self._open()

    def _release(self):
        if self.m is not None:
            with contextlib.suppress(Exception):
                self.m.vector_store.client.close()
            with contextlib.suppress(Exception):
                self.m.close()
            self.m = None

    def close(self):
        self._release()

    def reopen(self):
        self._open()

    def set_clock(self, ts):
        self.now = ts

    def _clock(self):
        return _Clock.at(self.now)

    # --- writing --------------------------------------------------------------------------------------------------

    def seed(self, facts):
        with self._clock():
            for f in facts:
                meta = {"fact_id": f["fact_id"], "valid_from": f.get("valid_from"), "valid_to": f.get("valid_to"),
                        "channel": f.get("channel")}
                self.m.add(f["text"], user_id=USER_ID, infer=False,
                           metadata={k: v for k, v in meta.items() if v is not None})

    def ingest(self, session):
        messages = [turn_message(t) for t in session["turns"]]
        meta = {"session_id": session["session_id"], "channel": session["channel"],
                "session_start": session["start_ts"], "session_end": session["end_ts"]}
        with self._clock():
            self.m.add(messages, user_id=USER_ID, metadata=meta, infer=True)

    # --- reading --------------------------------------------------------------------------------------------------

    def context(self, prompt, channel, session_id):
        t0 = time.perf_counter()
        with self._clock():
            res = self.m.search(prompt, filters={"user_id": USER_ID}, top_k=self.top_k)
        items = res.get("results", [])
        text = "\n".join(f"- {r['memory']}" for r in items)
        return ContextResult(text=text, item_ids=[str(r["id"]) for r in items],
                             latency_ms=(time.perf_counter() - t0) * 1000, tokens=approx_tokens(text))

    def cost(self):
        return self.proxy.cost()

    def dump(self):
        """Every stored memory (for inspection, not used by the harness)."""
        res = self.m.get_all(filters={"user_id": USER_ID}, top_k=100000)
        return json.loads(json.dumps(res.get("results", []), default=str))
