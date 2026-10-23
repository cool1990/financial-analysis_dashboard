from __future__ import annotations

import json
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
        assert json["reasoning"] == {"enabled": False}
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": None}, "finish_reason": "length"}],
                  "usage": {"prompt_tokens": 10, "completion_tokens": 4096}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    client = LLMClient(require_key=True, use_cache=False)
    client.reasoning_effort = "off"
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

    def fake_exchange(llm, prompt, ex):  # noqa: ANN001
        seen["ids"] = [ex["exchange_id"]]
        return [{"exchange_id": ex["exchange_id"], "question_summary": "fixed"}], []

    monkeypatch.setattr(qa_mod, "_structure_exchange", fake_exchange)
    monkeypatch.setattr(qa_mod, "_qa_client", lambda: None)
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


def test_truncation_splits_down_to_single_rounds():
    from pipeline.llm import LLMTruncatedError
    from pipeline.schemas import QAItem

    calls = []

    class Trunc(LLMClient):
        def __init__(self):  # noqa: D107
            pass

        def complete_json_list(self, prompt_name, user, item_schema):  # noqa: ANN001
            import json as _j

            batch = _j.loads(user.split("\n\n", 1)[1])
            calls.append(len(batch))
            if len(batch) > 1:
                raise LLMTruncatedError("cut")
            return [QAItem(exchange_id=batch[0]["exchange_id"], question_summary="q")]

    batch = [{"exchange_id": str(i), "text": "x"} for i in range(1, 5)]
    out = _structure_batch(Trunc(), "prompt", batch)  # type: ignore[arg-type]
    assert not any(o.get("parse_failed") for o in out) and len(out) == 4
    assert len(calls) <= 2 * len(batch)


def test_reasoning_param_mapping():
    from pipeline.llm import _reasoning_param

    assert _reasoning_param("off") == {"enabled": False}
    assert _reasoning_param(False) == {"enabled": False}  # YAML 里不加引号的 off 会被解析成 False
    assert _reasoning_param("high") == {"effort": "high", "exclude": True}
    assert _reasoning_param(None) is None


def test_drivers_budget_is_raised_above_global_cap():
    from pipeline.config import load_settings

    settings = load_settings()["llm"]
    assert settings["max_tokens"] == 4096
    assert "qa_max_tokens" not in settings
    assert settings["drivers_max_tokens"] >= 16384
    assert settings["drivers_request_timeout_sec"] >= 180


def test_analyze_drivers_asks_for_full_json_and_splits_when_truncated(monkeypatch):
    import pipeline.analyze.drivers as drivers
    from pipeline.llm import LLMTruncatedError
    from pipeline.schemas import DriversResult

    calls: list[dict] = []

    class Fake:
        def load_prompt(self, name):
            assert name == "analyze_drivers.md"
            return "PROMPT"

        def complete_json(self, name, user, schema, **kwargs):
            assert schema is DriversResult
            calls.append({"max_tokens": kwargs.get("max_tokens"), "timeout": kwargs.get("timeout_sec"), "user": user})
            table = json.loads(user.split("指标表:\n", 1)[1].split("\n\n新闻稿:\n", 1)[0])
            if len(table) > 1:
                raise LLMTruncatedError("truncated")
            metric = next(iter(table))
            return schema.model_validate({
                "metrics": [{"metric": metric, "summary": "原因", "drivers": [], "conflicts": []}],
            })

    monkeypatch.setattr(drivers, "LLMClient", Fake)
    out = drivers.analyze_drivers(
        {"revenue": {"value": 1}, "eps_gaap": {"value": 2}},
        "新闻稿原文",
        "管理层发言",
        "问答",
        stage=2,
    )
    assert calls[0]["max_tokens"] == 16384
    assert calls[0]["timeout"] == 240
    assert len(calls) == 3
    assert {item["metric"] for item in out["metrics"]} == {"revenue", "eps_gaap"}
    assert out["stage"] == 2
    assert any("完整 JSON" in warning for warning in out["warnings"])

    calls.clear()

    class Ok(Fake):
        def complete_json(self, name, user, schema, **kwargs):
            calls.append(kwargs)
            return schema.model_validate({
                "metrics": [{"metric": "revenue", "summary": "原因", "drivers": [], "conflicts": []}],
            })

    monkeypatch.setattr(drivers, "LLMClient", Ok)
    done = drivers.analyze_drivers({"revenue": {"value": 1}}, "新闻稿", "", "")
    assert len(calls) == 1 and calls[0]["max_tokens"] == 16384
    assert done["warnings"] == []
    assert done["metrics"][0]["metric"] == "revenue"

    class Boom(Fake):
        def complete_json(self, name, user, schema, **kwargs):
            raise LLMTruncatedError("still truncated")

    monkeypatch.setattr(drivers, "LLMClient", Boom)
    with pytest.raises(LLMTruncatedError):
        drivers.analyze_drivers({"revenue": {"value": 1}}, "新闻稿", "", "")


def test_strip_driver_warnings_drops_stale_stage2_failures():
    from pipeline.analyze.drivers import strip_driver_warnings

    kept = strip_driver_warnings(
        [
            "listing=hk，Stage1 按收益表规则抽取，未调用 LLM，也未接一致预期",
            "Stage2 drivers 失败: LLM 输出被 max_tokens=4096 截断（completion_tokens=4096，content 12354 字符）",
            "Stage2 drivers 失败: listing 只能是 domestic 或 adr，收到 'hk'",
            "Stage2 drivers 失败: listing 只能是 domestic 或 adr，收到 'hk'",
            "driver quote 未匹配: 1 条引文未在原文中找到",
        ]
    )
    assert kept == ["listing=hk，Stage1 按收益表规则抽取，未调用 LLM，也未接一致预期"]


def test_bypass_llm_cache_read_calls_model_again(monkeypatch, tmp_path):
    from pipeline.llm import LLMClient, bypass_llm_cache_read
    from pipeline.schemas import SummaryResult

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key-for-unit")
    monkeypatch.setattr("pipeline.llm.cache_dir", lambda: tmp_path)
    calls = {"n": 0}

    def fake_post(self, messages, *, json_object=True):
        calls["n"] += 1
        return '{"headline": "h", "key_findings": []}', {"prompt_tokens": 1, "completion_tokens": 1}, 1.0

    monkeypatch.setattr(LLMClient, "_post_chat", fake_post)
    monkeypatch.setattr(LLMClient, "_load_pricing", lambda self: None)
    client = LLMClient(require_key=True, use_cache=True)
    client._pricing = None
    assert client.complete_json("summarize.md", "same", SummaryResult).headline == "h"
    assert client.complete_json("summarize.md", "same", SummaryResult).headline == "h"
    assert calls["n"] == 1
    with bypass_llm_cache_read():
        assert client.complete_json("summarize.md", "same", SummaryResult).headline == "h"
    assert calls["n"] == 2
    assert client.complete_json("summarize.md", "same", SummaryResult).headline == "h"
    assert calls["n"] == 2


def _hk_stage2_doc() -> dict:
    return {
        "meta": {
            "fiscal_period": "FY2026Q1",
            "release_at_utc": "2026-05-13T05:41:43Z",
            "transcript_source": "local_import",
        },
        "status": {
            "stage": "stage2_done",
            "needs_review": False,
            "warnings": [
                "listing=hk，Stage1 按收益表规则抽取，未调用 LLM，也未接一致预期",
                "Stage2 drivers 失败: LLM 输出被 max_tokens=4096 截断（completion_tokens=4096，content 12354 字符）",
                "Stage2 drivers 失败: listing 只能是 domestic 或 adr，收到 'hk'",
                "Stage2 drivers 失败: listing 只能是 domestic 或 adr，收到 'hk'",
            ],
        },
        "qa": {"items": [{"exchange_id": "1", "question_summary": "旧"}]},
        "drivers": {"stage": 2, "metrics": [{"metric": "revenue", "drivers": []}]},
        "summary": {"headline": "旧标题", "key_findings": [], "next_watchlist": []},
        "guidance": {"items": []},
        "financials": {"revenue": {"value": 1}},
        "scorecard": [],
    }


def _patch_stage2_hk(monkeypatch, doc: dict, *, drivers):
    from pipeline.llm import llm_cache_read_bypassed

    cfg = {
        "listing": "hk",
        "reporting_currency": "CNY",
        "accounting_basis": "hkfrs",
        "sec": None,
        "name": "腾讯",
    }

    def load_doc(_ticker, period):
        return doc if period == "FY2026Q1" else None

    monkeypatch.setattr("pipeline.commands.run_cmd.load_period_json", load_doc)
    monkeypatch.setattr("pipeline.commands.run_cmd.load_ticker_config", lambda *_: cfg)
    monkeypatch.setattr(
        "pipeline.commands.run_cmd.fetch_transcript",
        lambda *_a, **_k: ("电话会正文。", "local_import"),
    )
    monkeypatch.setattr("pipeline.commands.run_cmd.save_period_json", lambda *_a, **_k: None)
    monkeypatch.setattr("pipeline.commands.run_cmd.mark_stage", lambda *_a, **_k: None)
    monkeypatch.setattr("pipeline.commands.run_cmd.maybe_notify", lambda *_a, **_k: None)
    monkeypatch.setattr("pipeline.config.load_position", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "pipeline.sources.alpha_vantage.fetch_earnings_estimates",
        lambda _ticker: {"called": True, "status": "ok", "reason": ""},
    )
    monkeypatch.setattr("pipeline.analyze.outlook.extract_industry_outlook", lambda *_a, **_k: [])

    def fake_qa(_text, _nums=None):
        assert llm_cache_read_bypassed()
        return {
            "items": [{"exchange_id": "1", "question_summary": "新"}],
            "prepared_remarks": "发言",
            "qa_text": "问答",
            "fact_warnings": [],
        }

    monkeypatch.setattr("pipeline.commands.run_cmd.structure_qa", fake_qa)
    monkeypatch.setattr(
        "pipeline.commands.run_cmd.summarize_period",
        lambda *_a, **_k: {"headline": "新标题", "key_findings": [], "next_watchlist": []},
    )
    monkeypatch.setattr("pipeline.commands.run_cmd.analyze_drivers", drivers)

    class GuidanceOnly:
        def load_prompt(self, name, **_kwargs):
            return "PROMPT"

        def complete_json_list(self, *_a, **_k):
            assert llm_cache_read_bypassed()
            return []

    monkeypatch.setattr("pipeline.commands.run_cmd.LLMClient", GuidanceOnly)


def test_stage2_force_hk_clears_stale_driver_warnings(monkeypatch):
    from pipeline.llm import llm_cache_read_bypassed

    doc = _hk_stage2_doc()
    seen = {}

    def drivers(*_a, **_k):
        from pipeline.analyze.drivers import _drivers_budget

        assert llm_cache_read_bypassed()
        seen["tokens"] = _drivers_budget()[0]
        return {
            "stage": 2,
            "metrics": [{"metric": "revenue", "summary": "增值服务增长", "drivers": []}],
            "warnings": [],
        }

    _patch_stage2_hk(monkeypatch, doc, drivers=drivers)
    out = run_stage2("0700.HK", "FY2026Q1", force=True)
    warnings = out["status"]["warnings"]
    assert not any(str(w).startswith("Stage2 drivers 失败") for w in warnings)
    assert not any("max_tokens=4096" in str(w) for w in warnings)
    assert not any("只能是 domestic" in str(w) for w in warnings)
    assert any("Stage1" in str(w) for w in warnings)
    assert seen["tokens"] >= 16384
    assert out["drivers"]["metrics"][0]["summary"] == "增值服务增长"
    assert out["status"]["stage"] == "stage2_done"
    assert llm_cache_read_bypassed() is False


def test_stage2_driver_failure_replaces_stale_warning(monkeypatch):
    from pipeline.llm import LLMTruncatedError

    doc = _hk_stage2_doc()

    def drivers(*_a, **_k):
        raise LLMTruncatedError("LLM 输出被 max_tokens=16384 截断")

    _patch_stage2_hk(monkeypatch, doc, drivers=drivers)
    out = run_stage2("0700.HK", "FY2026Q1", force=True)
    fails = [w for w in out["status"]["warnings"] if str(w).startswith("Stage2 drivers 失败")]
    assert fails == ["Stage2 drivers 失败: LLM 输出被 max_tokens=16384 截断"]


def test_hk_av_skip_is_not_a_failure(monkeypatch):
    from pipeline.commands import av_estimates_cmd as cmd

    monkeypatch.setattr(cmd, "alphavantage_api_key", lambda: "secret-key")
    monkeypatch.setattr(cmd, "load_ticker_config", lambda symbol: {"listing": "hk"})

    def boom(*_a, **_k):
        raise AssertionError("港股不应请求 Alpha Vantage")

    monkeypatch.setattr(cmd, "fetch_earnings_estimates_payload", boom)
    out = cmd.refresh_earnings_estimates(["0700.HK"])
    assert out["ok"] is True
    assert out["written"] == 0
    item = out["results"][0]
    assert item["ok"] is True and item["skipped"] is True
    assert "listing=hk" in item["reason"] and "不是失败" in item["reason"]


def test_settings_disable_reasoning_by_default():
    from pipeline.config import load_settings
    from pipeline.llm import _reasoning_param

    assert _reasoning_param(load_settings()["llm"]["reasoning_effort"]) == {"enabled": False}
