from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from pipeline.analyze.qa import _structure_batch
from pipeline.commands.run_cmd import run_stage2
from pipeline.llm import LLMClient, LLMError, LLMTimeoutError


ROOT = Path(__file__).resolve().parents[1]


def test_post_chat_sends_max_tokens(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key-for-unit")
    captured: dict = {}

    def fake_post(self, url, headers=None, json=None):  # noqa: A002
        captured["json"] = json
        req = httpx.Request("POST", url)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
            request=req,
        )

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    client = LLMClient(require_key=True, use_cache=False)
    client._pricing = None
    content, usage, _latency = client._post_chat(
        [{"role": "user", "content": "x"}],
        json_object=True,
    )
    assert content
    assert captured["json"]["max_tokens"] == client.max_tokens
    assert captured["json"]["max_tokens"] <= 8192


def test_post_chat_hard_timeout(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key-for-unit")

    def slow_post(self, url, headers=None, json=None):  # noqa: A002
        import time

        time.sleep(2.0)
        req = httpx.Request("POST", url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]}, request=req)

    monkeypatch.setattr(httpx.Client, "post", slow_post)
    client = LLMClient(require_key=True, use_cache=False)
    client.request_timeout_sec = 0.2
    client._pricing = None
    with pytest.raises(LLMTimeoutError):
        client._post_chat([{"role": "user", "content": "x"}], json_object=False)


def test_qa_batch_failure_does_not_per_item_retry(monkeypatch):
    calls = {"n": 0}

    class Boom(LLMClient):
        def __init__(self):  # noqa: D107
            pass

        def complete_json_list(self, *args, **kwargs):  # noqa: ANN002, ANN003
            calls["n"] += 1
            raise LLMError("Expecting value")

    batch = [
        {"exchange_id": "1", "analyst": "A", "firm": "F", "text": "Q1"},
        {"exchange_id": "2", "analyst": "B", "firm": "G", "text": "Q2"},
        {"exchange_id": "3", "analyst": "C", "firm": "H", "text": "Q3"},
    ]
    out = _structure_batch(Boom(), "prompt", batch)  # type: ignore[arg-type]
    assert calls["n"] == 1  # 整批一次，不再逐条
    assert len(out) == 3
    assert all(x.get("parse_failed") for x in out)


def test_stage2_skips_when_already_complete(tmp_path, monkeypatch):
    doc = {
        "meta": {"fiscal_period": "FY2026Q4", "release_at_utc": "2026-09-30T20:00:00Z", "transcript_source": "motley_fool"},
        "status": {"stage": "stage3_done", "warnings": [], "needs_review": False},
        "qa": {"items": [{"exchange_id": "1", "topic": "其他", "question_summary": "x", "answer_summary": "y"}]},
        "drivers": {"stage": 2, "metrics": [{"metric": "revenue", "drivers": []}]},
        "summary": {"headline": "已有标题", "key_findings": [], "next_watchlist": []},
        "guidance": {"items": []},
        "financials": {},
        "scorecard": [],
    }

    monkeypatch.setattr("pipeline.commands.run_cmd.load_period_json", lambda *a, **k: doc)
    monkeypatch.setattr("pipeline.commands.run_cmd.load_ticker_config", lambda *_: {"name": "MU"})

    called = {"fetch": 0}

    def boom_fetch(*a, **k):
        called["fetch"] += 1
        raise AssertionError("不应再拉文字稿/LLM")

    monkeypatch.setattr("pipeline.commands.run_cmd.fetch_transcript", boom_fetch)
    out = run_stage2("MU", "FY2026Q4", force=False)
    assert out["status"]["stage"] == "stage3_done"
    assert called["fetch"] == 0
