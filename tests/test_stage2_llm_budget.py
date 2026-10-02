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

    batch = [{"exchange_id": str(i), "analyst": "A", "firm": "F", "text": f"Q{i}"} for i in range(1, 6)]
    out = _structure_batch(Boom(), "prompt", batch)  # type: ignore[arg-type]
    # 整批 1 次 + 拆半各 1 次，上限 3 次，不随批次大小逐条增长
    assert calls["n"] == 3
    assert len(out) == 5
    assert all(x.get("parse_failed") for x in out)
    assert all(x["directness"] is None for x in out)


def test_qa_batch_split_recovers_half(monkeypatch):
    class Half(LLMClient):
        def __init__(self):  # noqa: D107
            pass

        def complete_json_list(self, prompt_name, user, item_schema):  # noqa: ANN001
            import json as _j
            from pipeline.schemas import QAItem

            batch = _j.loads(user.split("\n\n", 1)[1])
            if len(batch) > 2:
                raise LLMError("Expecting value")
            return [QAItem(exchange_id=b["exchange_id"], question_summary="q") for b in batch]

    batch = [{"exchange_id": str(i), "text": "x"} for i in range(1, 5)]
    out = _structure_batch(Half(), "prompt", batch)  # type: ignore[arg-type]
    assert [o["exchange_id"] for o in out] == ["1", "2", "3", "4"]
    assert not any(o.get("parse_failed") for o in out)


def test_truncated_output_not_retried(monkeypatch):
    from pipeline.llm import LLMTruncatedError
    from pipeline.schemas import SummaryResult

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key-for-unit")
    calls = {"n": 0}

    def fake_post(self, url, headers=None, json=None):  # noqa: A002
        calls["n"] += 1
        assert json["reasoning"]["effort"] == "low"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": None}, "finish_reason": "length"}],
                  "usage": {"prompt_tokens": 10, "completion_tokens": 4096}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    client = LLMClient(require_key=True, use_cache=False)
    client.reasoning_effort = "low"
    with pytest.raises(LLMTruncatedError):
        client.complete_json("summarize.md", "x", SummaryResult)
    assert calls["n"] == 1


def test_repair_only_failed_qa(monkeypatch):
    import pipeline.analyze.qa as qa_mod

    transcript = (
        "Prepared remarks here.\n\nQuestion-and-Answer Session\n\n"
        "Operator: Our first question comes from Ann Lee with Citi.\nAnn Lee: q1\nCEO: a1\n"
        "Operator: Next is Bob Ray with UBS.\nBob Ray: q2\nCEO: a2\n"
    )
    seen = {}

    def fake_batch(llm, prompt, batch, split=True):  # noqa: ANN001
        seen["ids"] = [b["exchange_id"] for b in batch]
        return [{"exchange_id": b["exchange_id"], "question_summary": "fixed"} for b in batch]

    monkeypatch.setattr(qa_mod, "_structure_batch", fake_batch)
    monkeypatch.setattr(qa_mod, "LLMClient", lambda: None)
    monkeypatch.setattr(qa_mod, "_prompt", lambda llm, nums: "p")
    _, qa_text = qa_mod.split_prepared_and_qa(transcript)
    ids = [e["exchange_id"] for e in qa_mod._split_exchanges(qa_text)]
    items = [{"exchange_id": ids[0], "question_summary": "ok"}, {"exchange_id": ids[-1], "parse_failed": True}]
    out, repaired = qa_mod.repair_failed_items(transcript, items)
    assert seen["ids"] == [ids[-1]]
    assert repaired == 1 and out[0]["question_summary"] == "ok" and out[1]["question_summary"] == "fixed"


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


def test_complete_json_accepts_fenced_output_without_retry(monkeypatch):
    """模型包了 ```json 围栏时直接解析，不应再付费重试。"""
    from pipeline.schemas import SummaryResult

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key-for-unit")
    calls = {"n": 0}

    def fake_post(self, messages, *, json_object=True):
        calls["n"] += 1
        return '```json\n{"headline": "h", "key_findings": []}\n```', {"prompt_tokens": 1, "completion_tokens": 1}, 1.0

    monkeypatch.setattr(LLMClient, "_post_chat", fake_post)
    client = LLMClient(require_key=True, use_cache=False)
    out = client.complete_json("summarize.md", "x", SummaryResult)
    assert out.headline == "h"
    assert calls["n"] == 1


def test_complete_json_list_attempts_bounded(monkeypatch):
    from pipeline.schemas import QAItem

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key-for-unit")
    calls = {"n": 0}

    def fake_post(self, messages, *, json_object=True):
        calls["n"] += 1
        return "not json", {"prompt_tokens": 1, "completion_tokens": 1}, 1.0

    monkeypatch.setattr(LLMClient, "_post_chat", fake_post)
    client = LLMClient(require_key=True, use_cache=False)
    with pytest.raises(LLMError):
        client.complete_json_list("structure_qa.md", "x", QAItem)
    assert calls["n"] == client.max_retries + 1
