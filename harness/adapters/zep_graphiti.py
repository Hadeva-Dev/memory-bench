"""Competitor `zep_graphiti`: Graphiti (`graphiti-core`), the open-source temporal knowledge-graph engine behind Zep,
with the benchmark's brains.

Needs the optional dependencies: `uv sync --extra competitors` (or `uv run --extra competitors ...`).

Graph store: Kuzu, embedded in the process (graphiti-core's KuzuDriver), one database file under the workdir; no
server and no Docker. LLM: Graphiti's OpenAIGenericClient (the client its docs give for OpenAI-compatible endpoints;
native json_schema structured output) pointed at tools/llm_proxy.py, so every call is one isolated claude-sonnet-5-5
call; the same model serves Graphiti's "small" model slot. Embedder: Graphiti's OpenAIEmbedder at the same proxy,
which serves BAAI/bge-small-en-v1.5 (384 dimensions). Cross-encoder: OpenAIRerankerClient at the proxy (constructed
because Graphiti requires one; the default search below does not use it). Telemetry is off.

- ingest(session): one add_episode per session, as Zep's docs recommend for chat history: source=message, the body
  is the transcript one "speaker: text" line per turn (the principal as "user", the assistant as "assistant", anyone
  else by the name the device shows, speaker_name from harness.data.session_view), reference_time = the session's
  start, source_description names the channel. Graphiti is bi-temporal: it dates facts (valid_at, invalid_at) from
  these reference times and invalidates contradicted ones.
- seed(facts): add_episode_bulk over the facts in order of valid_from, one JSON episode per fact (subject,
  predicate, value, valid_from, valid_to, stated_by; the principal as "user") with reference_time = valid_from, in
  chunks (the bulk path still extracts, dedupes, dates and invalidates). JSON is Zep's documented episode type for
  structured data; the one-line text rendering ("principal dentist appointment: ...") made Graphiti's conservative
  entity extraction return nothing.
- context(): Graphiti's hybrid search for the prompt over edges (facts) and entity nodes, each BM25 + cosine with
  RRF and Graphiti's default limit of 10 (the edge half is exactly what Graphiti.search() returns), rendered as the
  context block in Zep's docs: FACTS with their date ranges ("fact (valid_at - invalid_at or present)") and
  ENTITIES with their summaries. item_ids are the edge uuids, then the node uuids. Graphiti has no channel scoping,
  so every channel gets the same.
- maintain(): nothing (communities are optional in Graphiti and not built by default).

See harness/adapters/COMPETITORS.md for every choice and approximation.
"""
import asyncio
import contextlib
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from harness.adapter import Adapter, ContextResult
from harness.adapters import approx_tokens, speaker_label
from tools.llm_proxy import DEFAULT_MODEL, EMBED_MODEL, LLMProxy

os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")

GROUP_ID = "principal"
EMBED_DIMS = 384
SEED_CHUNK = 20
DEFAULT_LIMIT = 10  # graphiti_core.search.search_config.DEFAULT_SEARCH_LIMIT, the default of search() and search_()
DB_FILE = "graph.kuzu"
SPEAKER = {"principal": "user", "assistant": "assistant"}


def transcript(session):
    return "\n".join(f"{SPEAKER.get(t['speaker']) or speaker_label(t)}: {t['text']}" for t in session["turns"])


def fact_json(fact):
    """A seeded fact as a JSON episode (Zep's documented path for structured data); the principal is "user", as in
    the session transcripts."""
    who = lambda x: SPEAKER.get(x, x) if x else x  # noqa: E731
    rec = {"subject": who(fact.get("subject")), "predicate": (fact.get("predicate") or "").replace("_", " "),
           "value": fact.get("value"), "valid_from": fact.get("valid_from"), "valid_to": fact.get("valid_to"),
           "stated_by": who(fact.get("stated_by"))}
    return json.dumps({k: v for k, v in rec.items() if v is not None}, ensure_ascii=False)


def _when(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def date_range(edge):
    """Zep docs' format_edge_date_range."""
    return f"{edge.valid_at if edge.valid_at else 'date unknown'} - {edge.invalid_at if edge.invalid_at else 'present'}"


def context_block(edges, nodes):
    """The context block Zep's docs build from Graphiti search results (compose_context_string)."""
    facts = "\n".join(f"  - {e.fact} ({date_range(e)})" for e in edges)
    entities = "\n".join(f"  - {n.name}: {n.summary}" for n in nodes)
    return ("FACTS and ENTITIES represent relevant context to the current conversation.\n\n"
            "# These are the most relevant facts and their valid date ranges\n"
            "# format: FACT (Date range: from - to)\n"
            f"<FACTS>\n{facts}\n</FACTS>\n\n"
            "# These are the most relevant entities\n"
            "# ENTITY_NAME: entity summary\n"
            f"<ENTITIES>\n{entities}\n</ENTITIES>")


class ZepGraphiti(Adapter):
    name = "zep_graphiti"

    def __init__(self, llm=None, embed=None, model=DEFAULT_MODEL, num_results=DEFAULT_LIMIT):
        self.proxy = LLMProxy(llm=llm, embed=embed, model=model)
        self.model = model
        self.num_results = num_results
        self.loop = asyncio.new_event_loop()
        self.g = None
        self.driver = None
        self.now = None

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    # --- lifecycle ------------------------------------------------------------------------------------------------

    def _open(self, fresh=False):
        logging.getLogger("graphiti_core").setLevel(logging.WARNING)
        import warnings
        from graphiti_core import Graphiti
        from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
        from graphiti_core.driver.kuzu_driver import KuzuDriver
        from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
        from graphiti_core.llm_client.config import LLMConfig
        from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

        url = self.proxy.start()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)  # "the Kuzu backend is deprecated"
            self.driver = KuzuDriver(db=str(self.wd / DB_FILE))
        # graphiti-core 0.30.2: KuzuDriver never sets `_database`, so add_episode(group_id=...) fails with an
        # AttributeError in Graphiti._resolve_request_scope. A Kuzu file holds one database; naming it after the
        # group sends every call down the shared-driver path, which is what Graphiti does for any default database.
        self.driver._database = GROUP_ID
        cfg = LLMConfig(api_key="unused-local-proxy", model=self.model, small_model=self.model, base_url=url)
        self.g = Graphiti(
            graph_driver=self.driver,
            llm_client=OpenAIGenericClient(config=cfg),
            embedder=OpenAIEmbedder(OpenAIEmbedderConfig(api_key="unused-local-proxy", base_url=url,
                                                         embedding_model=EMBED_MODEL, embedding_dim=EMBED_DIMS)),
            cross_encoder=OpenAIRerankerClient(config=cfg),
        )
        if fresh:
            # KuzuDriver.build_indices_and_constraints is a no-op; the full-text indexes its searches query are made
            # by the driver's graph operations.
            self._run(self.driver.graph_ops.build_indices_and_constraints(self.driver))

    def reset(self, workdir):
        self._release()
        self.wd = Path(workdir)
        self.wd.mkdir(parents=True, exist_ok=True)
        self.proxy.start()
        self.proxy.reset_stats()
        self._open(fresh=True)

    def _release(self):
        if self.driver is not None:
            with contextlib.suppress(Exception):
                self._run(self.driver.client.close()) if asyncio.iscoroutinefunction(
                    getattr(self.driver.client, "close", None)) else self.driver.client.close()
            with contextlib.suppress(Exception):
                self.driver.db.close()
        self.g = self.driver = None

    def close(self):
        self._release()

    def reopen(self):
        self._open()

    def set_clock(self, ts):
        self.now = ts

    # --- writing --------------------------------------------------------------------------------------------------

    def seed(self, facts):
        from graphiti_core.nodes import EpisodeType
        from graphiti_core.utils.bulk_utils import RawEpisode
        ordered = sorted(facts, key=lambda f: (_when(f["valid_from"]) if f.get("valid_from") else datetime.min,
                                               f["fact_id"]))
        eps = [RawEpisode(name=f["fact_id"], content=fact_json(f), source=EpisodeType.json,
                          source_description="memory record", reference_time=_when(f["valid_from"]))
               for f in ordered]
        for i in range(0, len(eps), SEED_CHUNK):
            self._run(self.g.add_episode_bulk(eps[i:i + SEED_CHUNK], group_id=GROUP_ID))

    def ingest(self, session):
        from graphiti_core.nodes import EpisodeType
        self._run(self.g.add_episode(
            name=session["session_id"], episode_body=transcript(session),
            source_description=f"{session['channel']} conversation", reference_time=_when(session["start_ts"]),
            source=EpisodeType.message, group_id=GROUP_ID))

    # --- reading --------------------------------------------------------------------------------------------------

    def search_config(self):
        from graphiti_core.search.search_config import SearchConfig
        from graphiti_core.search.search_config_recipes import COMBINED_HYBRID_SEARCH_RRF as R
        return SearchConfig(edge_config=R.edge_config, node_config=R.node_config, limit=self.num_results)

    def context(self, prompt, channel, session_id):
        t0 = time.perf_counter()
        res = self._run(self.g.search_(prompt, config=self.search_config(), group_ids=[GROUP_ID]))
        edges, nodes = res.edges, res.nodes
        if not edges and not nodes:
            return ContextResult(text="", latency_ms=(time.perf_counter() - t0) * 1000)
        text = context_block(edges, nodes)
        return ContextResult(text=text, item_ids=[e.uuid for e in edges] + [n.uuid for n in nodes],
                             latency_ms=(time.perf_counter() - t0) * 1000, tokens=approx_tokens(text))

    def cost(self):
        return self.proxy.cost()
