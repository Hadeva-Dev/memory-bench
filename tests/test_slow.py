"""Slow checks against real models. Skipped by default; run with: uv run pytest -m slow -q"""
import re

import numpy as np
import pytest

from harness import llm
from harness.prompts import ANSWER_SYSTEM, answer_prompt

pytestmark = pytest.mark.slow

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def test_answerer_is_isolated_from_any_user(tmp_path):
    """The answering model, given an empty memory, must show no knowledge of any user: no name, no email, no
    details from settings, CLAUDE.md files, saved memories or the operator's account."""
    client = llm.LLM(tmp_path / "cache")  # fresh cache: a real call
    reply = client.complete(llm.DEFAULT_ANSWERER, ANSWER_SYSTEM,
                            answer_prompt("", "What is my name and what do you know about me?",
                                          "2026-05-01T10:00:00-07:00"))
    print("REPLY:", reply)
    assert client.stats["calls"] == 1
    assert not EMAIL.search(reply), reply
    low = reply.lower()
    assert any(p in low for p in ("don't know", "do not know", "don't have", "do not have", "no information",
                                  "nothing")), reply
    assert not re.search(r"your name is \w", low), reply
    for leak in ("darwin", "macos", "zsh", "working directory", "/users/", "/private/", "claude.md"):
        assert leak not in low, (leak, reply)


def test_naive_rag_with_the_real_embedding_model(tmp_path):
    from harness.adapters.naive_rag import NaiveRag
    a = NaiveRag()
    a.reset(tmp_path)
    a.seed([{"fact_id": "a", "text": "My sister is vegetarian and allergic to cashews."},
            {"fact_id": "b", "text": "The car needs new tires before the trip."},
            {"fact_id": "c", "text": "Team standup moved to 9:30 on Mondays."}])
    r = a.context("what can my sibling not eat?", "typed", "p")
    assert r.item_ids[0] == "a"
    assert isinstance(a.vecs, np.ndarray) and a.vecs.shape[1] == 384
