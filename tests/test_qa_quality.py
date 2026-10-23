"""问答拆分、数字核对、审阅和页脚旧提示。不调用 OpenRouter。"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "meta_qa_brian_eric.txt"


def _exchanges():
    from pipeline.analyze.qa import _exchange_worth_structuring, _split_exchanges
    from pipeline.sources.transcripts import split_prepared_and_qa

    _, qa = split_prepared_and_qa(FIXTURE.read_text(encoding="utf-8"))
    exchanges = _split_exchanges(qa)
    # 文字稿在第一位 Operator 之前还有「Question-and-Answer Session」标题，那一块没有分析师。
    assert not any(ex.get("analyst") is None and _exchange_worth_structuring(ex) for ex in exchanges)
    by_name = {ex["analyst"]: ex for ex in exchanges if ex.get("analyst")}
    return by_name["Brian Nowak"], by_name["Eric Sheridan"]


def test_fixture_splits_brian_and_eric_and_counts_two_questions():
    from pipeline.analyze.qa_facts import analyst_subquestion_count, management_answer_text, extract_facts

    brian, eric = _exchanges()
    assert brian["analyst"] == "Brian Nowak"
    assert eric["analyst"] == "Eric Sheridan"
    assert analyst_subquestion_count(brian["text"], brian["analyst"]) >= 2
    facts = extract_facts(management_answer_text(brian["text"], brian["analyst"]))
    assert ("count", 10_000_000) in facts
    assert ("weeks", 6) in facts
    assert ("count", 1_000_000_000) in facts
    assert ("count", 500_000_000) in facts
    assert "Reality Labs" not in eric["text"]
    assert "how reality evolves" in eric["text"]


def test_chinese_summary_covers_english_facts_and_flags_dropped_weeks():
    from pipeline.analyze.qa_facts import fact_label, false_friend_flags, missing_answer_facts

    brian, eric = _exchanges()
    covered = [
        "Mark（CEO）：仅美加少数市场，六周超过1000万用户。算力要能服务十亿以上用户。",
        "Susan（CFO）：已有5亿商家。",
    ]
    assert missing_answer_facts(brian["text"], covered, brian["analyst"]) == []
    dropped = missing_answer_facts(
        brian["text"],
        ["Mark（CEO）：美国和加拿大已有超过1000万用户，十亿以上。5亿商家。"],
        brian["analyst"],
    )
    assert ("weeks", 6) in dropped
    assert fact_label(("weeks", 6)) == "6 weeks"
    flags = false_friend_flags(eric["text"], "Susan（CFO）：代理会成为现实实验室生态的重要部分。")
    assert flags and "现实实验室" in flags[0]
    assert false_friend_flags(eric["text"], "Susan（CFO）：代理会成为现实会怎么演变的重要部分。") == []


def test_structure_exchange_retries_split_and_reviews():
    import pipeline.analyze.qa as qa_mod
    from pipeline.schemas import QAItem

    brian = _exchanges()[0]
    calls: list[str] = []

    class Fake:
        def load_prompt(self, name, **kwargs):
            text = (ROOT / "prompts" / name).read_text(encoding="utf-8")
            for key, value in kwargs.items():
                text = text.replace("{" + key + "}", value)
            return text

        def complete_json_list(self, prompt_name, user, schema):
            calls.append(prompt_name)
            if prompt_name == "review_qa.md":
                body = user.split("已有摘要：\n", 1)[1].split("\n\n规则检查：", 1)[0]
                return [schema.model_validate(item) for item in json.loads(body)]
            if calls.count("structure_qa.md") == 1:
                return [
                    QAItem(
                        exchange_id="1",
                        question_summary="把 Muse 和企业并成一个问题",
                        answer_summary="Mark（CEO）：美国和加拿大已有超过1000万用户。\nSusan（CFO）：已有5亿商家。",
                    )
                ]
            return [
                QAItem(
                    exchange_id="1",
                    question_summary="Muse 用户增长卡在哪里",
                    answer_summary="Mark（CEO）：仅美加少数市场，六周超过1000万用户。算力要服务十亿以上用户。免费层 Muse Spark，付费层 Muse Wonder。",
                ),
                QAItem(
                    exchange_id="1",
                    question_summary="企业业务的愿景",
                    answer_summary=(
                        "Mark（CEO）：近期是模型 API、Muse Code 和负责销售与客服的商家 agent，三者增长快。"
                        "长期是商业 Muse agent 网络，Muse 之间可以协作。\n"
                        "Susan（CFO）：传统企业软件没有锁定市场。已有5亿商家。订阅、按用量 API 和消息。"
                        "按已验证的需求和产品市场匹配扩大投入。"
                    ),
                ),
            ]

    items, warnings = qa_mod._structure_exchange(Fake(), "prompt", brian)  # type: ignore[arg-type]
    assert calls == ["structure_qa.md", "structure_qa.md", "review_qa.md"]
    assert len(items) == 2
    assert "模型 API" in items[1]["answer_summary"]
    assert "Susan（CFO）" in items[1]["answer_summary"]
    assert warnings == []
    assert all(item["exchange_id"] == brian["exchange_id"] for item in items)


def test_review_that_drops_a_timeframe_keeps_the_earlier_summary():
    import pipeline.analyze.qa as qa_mod
    from pipeline.schemas import QAItem

    brian = _exchanges()[0]
    good = [
        QAItem(
            exchange_id=brian["exchange_id"],
            question_summary="Muse 用户增长卡在哪里",
            answer_summary="Mark（CEO）：仅美加少数市场，六周超过1000万用户。算力要服务十亿以上用户。",
        ),
        QAItem(
            exchange_id=brian["exchange_id"],
            question_summary="企业业务的愿景",
            answer_summary="Mark（CEO）：近期是模型 API。\nSusan（CFO）：已有5亿商家。",
        ),
    ]

    class Fake:
        def load_prompt(self, name, **kwargs):
            return (ROOT / "prompts" / name).read_text(encoding="utf-8")

        def complete_json_list(self, prompt_name, user, schema):
            if prompt_name == "structure_qa.md":
                return good
            body = user.split("已有摘要：\n", 1)[1].split("\n\n规则检查：", 1)[0]
            items = json.loads(body)
            items[0]["answer_summary"] = "Mark（CEO）：仅美加少数市场，超过1000万用户。算力要服务十亿以上用户。"
            return [schema.model_validate(item) for item in items]

    items, warnings = qa_mod._structure_exchange(Fake(), "prompt", brian)  # type: ignore[arg-type]
    assert "六周" in items[0]["answer_summary"]
    assert any("审阅漏掉了" in flag and "6 weeks" in flag for flag in items[0]["review_flags"])
    assert warnings == []


def test_review_flags_reality_labs_mistranslation():
    import pipeline.analyze.qa as qa_mod
    from pipeline.schemas import QAItem

    eric = _exchanges()[1]

    class Fake:
        def load_prompt(self, name, **kwargs):
            return (ROOT / "prompts" / name).read_text(encoding="utf-8")

        def complete_json_list(self, prompt_name, user, schema):
            if prompt_name == "structure_qa.md":
                return [
                    QAItem(
                        exchange_id=eric["exchange_id"],
                        question_summary="代理商务的摩擦",
                        answer_summary="Mark（CEO）：商家总体上很积极。\nSusan（CFO）：代理会成为现实实验室生态的重要部分。",
                    )
                ]
            body = user.split("已有摘要：\n", 1)[1].split("\n\n规则检查：", 1)[0]
            items = json.loads(body)
            return [schema.model_validate(item) for item in items]

    items, warnings = qa_mod._structure_exchange(Fake(), "prompt", eric)  # type: ignore[arg-type]
    assert any("现实实验室" in flag for flag in items[0]["review_flags"])
    assert any("现实实验室" in warning for warning in warnings)


def test_reconcile_drops_stage1_llm_note_and_rewrites_alpha_vantage():
    from pipeline.status_notes import reconcile_stage2_warnings

    warnings = [
        "问答是从 Motley Fool 文字稿逐轮摘出的，没有调用模型。仍待 Stage 2：变动原因、语气、模型总结和论点复核。当前环境没有 OPENROUTER_API_KEY。",
        "未找到 Obsidian 收集箱/财报/META。公司指引以仓库里的 SEC Exhibit 99.1 为准。",
        "没有来自 TradingView 或 Alpha Vantage 的 GAAP 一致预期；Yahoo 约 $5.46 未采用。",
        "Alpha Vantage EARNINGS_ESTIMATES 没有调用：没有 API key。分析师人数、高低值、7/30/60/90 日前预期和上调下调次数为空。",
    ]
    called = reconcile_stage2_warnings(
        warnings,
        qa_model_called=True,
        alpha_vantage={"called": True, "status": "ok", "reason": ""},
    )
    assert all("没有调用模型" not in item and "OPENROUTER_API_KEY" not in item for item in called)
    assert all("没有 API key" not in item for item in called)
    assert any("Obsidian" in item for item in called)
    assert any("GAAP 一致预期" in item for item in called)

    missing_key = reconcile_stage2_warnings(
        warnings,
        qa_model_called=True,
        alpha_vantage={"called": False, "status": "not_called", "reason": "没有 API key"},
    )
    assert any(item == "Alpha Vantage EARNINGS_ESTIMATES 没有调用：没有 API key。" for item in missing_key)

    failed = reconcile_stage2_warnings(
        warnings,
        qa_model_called=False,
        alpha_vantage={"called": True, "status": "error", "reason": "rate limit"},
    )
    assert any("没有调用模型" in item for item in failed)
    assert any("已调用但失败：rate limit" in item for item in failed)


def test_alpha_vantage_status_follows_this_run(monkeypatch):
    from pipeline.sources.alpha_vantage import fetch_earnings_estimates

    monkeypatch.delenv("ALPHAVANTAGE_KEY", raising=False)
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    skipped = fetch_earnings_estimates("META", get=lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应发请求")))
    assert skipped["called"] is False and skipped["status"] == "not_called"
    assert "没有 API key" in skipped["reason"]

    monkeypatch.setenv("ALPHAVANTAGE_KEY", "test-av-key")
    seen = {}

    def fake_get(url, params):
        seen["url"] = url
        seen["symbol"] = params["symbol"]
        seen["has_key"] = params["apikey"] == "test-av-key"
        return {"estimates": [{"horizon": "next fiscal year"}]}

    called = fetch_earnings_estimates("meta", get=fake_get)
    assert called["called"] is True and called["status"] == "ok"
    assert seen["symbol"] == "META" and seen["has_key"]
    assert "estimates" not in called


def test_qa_model_falls_back_to_summary_model(monkeypatch):
    import pipeline.analyze.qa as qa_mod
    from pipeline.config import load_settings
    from pipeline.llm import LLMClient

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key-for-unit")
    monkeypatch.delenv("PIPELINE_LLM_MODEL", raising=False)
    raw = (ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")
    assert "claude-sonnet" not in raw
    assert "anthropic/" not in raw
    settings = load_settings()["llm"]
    assert settings["model"] == "deepseek/deepseek-v4-pro-0813"
    assert settings["qa_model"] == "deepseek/deepseek-v4-pro-0813"
    assert "qa_reasoning_effort" not in settings
    assert "qa_max_tokens" not in settings
    client = qa_mod._qa_client()
    summary = LLMClient()
    assert client.model == "deepseek/deepseek-v4-pro-0813"
    assert summary.model == "deepseek/deepseek-v4-pro-0813"
    assert client.model == summary.model
    assert client.max_tokens == summary.max_tokens == 4096
    assert client.reasoning_effort == summary.reasoning_effort == "off"
    assert client.request_timeout_sec == summary.request_timeout_sec

    monkeypatch.setenv("PIPELINE_LLM_MODEL", "anthropic/claude-sonnet-5.5")
    pinned = qa_mod._qa_client()
    assert pinned.model == "deepseek/deepseek-v4-pro-0813"
    monkeypatch.delenv("PIPELINE_LLM_MODEL")

    monkeypatch.setattr(qa_mod, "load_settings", lambda: {"llm": {}})
    fallback = qa_mod._qa_client()
    assert fallback.model == summary.model


def test_workflows_pass_alphavantage_key():
    manual = (ROOT / ".github" / "workflows" / "manual.yml").read_text(encoding="utf-8")
    poll = (ROOT / ".github" / "workflows" / "poll.yml").read_text(encoding="utf-8")
    daily = (ROOT / ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8")
    secret = "ALPHAVANTAGE_KEY: ${{ secrets.ALPHAVANTAGE_KEY }}"
    assert secret in manual and secret in poll and secret in daily
    assert "python -m pipeline av-estimates" in manual
    assert "python -m pipeline av-estimates" in poll
    assert "python -m pipeline av-estimates" in daily


def test_review_prompt_checks_omitted_answers_and_mistranslation():
    text = (ROOT / "prompts" / "review_qa.md").read_text(encoding="utf-8")
    assert "现实实验室" in text
    assert "six weeks" in text
    assert "Enterprise" in text
    assert "不能丢" in text or "丢掉" in text
    assert "忠实原文" in text and "逻辑清晰" in text and "通俗易懂" in text
    assert "数字" in text and "时间" in text
