"""成本护栏与无 LLM 数据补全的回归测试（不联网）。"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


# ---- 新 8-K 过滤：历史 8-K 不能被 poll 当成新财报 --------------------------------

def _filing(acc: str, accepted: datetime) -> dict:
    return {"accessionNumber": acc, "acceptanceDateTime": accepted.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "form": "8-K", "items": "2.02"}


def test_detect_new_filings_ignores_old_and_failed(monkeypatch, tmp_path):
    from pipeline.commands import run_cmd
    from pipeline import state

    now = datetime(2026, 10, 1, 21, 0, tzinfo=timezone.utc)
    filings = [
        _filing("new-1", now - timedelta(hours=2)),
        _filing("failed-1", now - timedelta(hours=3)),
        _filing("old-1", now - timedelta(days=90)),
    ]
    monkeypatch.setattr(run_cmd, "load_ticker_config", lambda t: {"cik": "0000000001"})
    monkeypatch.setattr(run_cmd.SecEdgarClient, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(run_cmd.SecEdgarClient, "find_earnings_8k", lambda self, cik: filings)
    monkeypatch.setattr(state, "data_dir", lambda t=None: tmp_path)
    for _ in range(3):
        state.record_filing_failure("X", "failed-1")
    monkeypatch.setattr(run_cmd, "load_processed", state.load_processed)
    monkeypatch.setattr(run_cmd, "load_failures", state.load_failures)
    out = run_cmd.detect_new_filings("X", now=now)
    assert [f["accessionNumber"] for f in out] == ["new-1"]

    state.add_processed("X", "new-1")
    assert run_cmd.detect_new_filings("X", now=now) == []
    data = json.loads((tmp_path / "processed.json").read_text())
    assert data["accessions"] == ["new-1"] and data["failures"] == {"failed-1": 3}


def test_near_earnings_gate(monkeypatch):
    from pipeline.commands import poll_cmd

    monkeypatch.setattr(poll_cmd, "known_earnings_dates", lambda t: [date(2026, 9, 30), date(2026, 12, 23)])
    assert poll_cmd.near_earnings("MU", date(2026, 9, 30))
    assert poll_cmd.near_earnings("MU", date(2026, 10, 2))
    assert not poll_cmd.near_earnings("MU", date(2026, 11, 10))
    monkeypatch.setattr(poll_cmd, "known_earnings_dates", lambda t: [])
    assert poll_cmd.near_earnings("MU", date(2026, 11, 10))  # 无日期时保守放行


def test_poll_stops_stage2_after_transcript_deadline(monkeypatch, tmp_path):
    from pipeline.commands import poll_cmd

    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "FY2026Q4.json").write_text(json.dumps({"stage": "stage1_done", "fiscal_period": "FY2026Q4"}))
    (tmp_path / "FY2026Q4.json").write_text(json.dumps({"meta": {"release_at_utc": "2026-09-28T20:00:00Z"}}))
    called = []
    monkeypatch.setattr(poll_cmd, "list_tickers", lambda: ["MU"])
    monkeypatch.setattr(poll_cmd, "load_ticker_config", lambda t: {"release_timing": "amc", "cik": None})
    monkeypatch.setattr(poll_cmd, "data_dir", lambda t=None: tmp_path)
    monkeypatch.setattr(poll_cmd, "run_stage2", lambda *a, **k: called.append(a))
    out = poll_cmd.poll_once(datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc))
    assert called == []
    assert out["did_work"] is False and out["actions"][0]["skipped"] is True


# ---- 新闻稿对比列 → 同比 / 环比 ----------------------------------------------------

def test_press_release_comparatives_mu():
    from pipeline.compute.comparatives import apply_press_comparatives, detect_layout

    extracted = json.loads((ROOT / "data" / "MU" / "raw" / "FY2026Q4" / "extracted_financials.json").read_text())
    assert detect_layout(extracted) == {"yoy": 2, "qoq": 1}
    fin = {
        "revenue": {"value": 54229e6, "yoy_pct": None, "qoq_pct": None},
        "gross_margin_nongaap": {"value": 47204 / 54229, "yoy_pp": None, "qoq_pp": None},
        "eps_nongaap": {"value": 33.42, "yoy_pct": None, "qoq_pct": None},
        "fcf": {"value": 32863e6, "yoy_pct": None, "qoq_pct": None},
    }
    filled = apply_press_comparatives(fin, extracted)
    assert set(filled) == set(fin)
    assert fin["revenue"]["qoq_pct"] == pytest.approx(54229 / 41456 - 1)
    assert fin["revenue"]["yoy_pct"] == pytest.approx(54229 / 11315 - 1)
    assert fin["gross_margin_nongaap"]["qoq_pp"] == pytest.approx(47204 / 54229 - 35199 / 41456)
    assert fin["eps_nongaap"]["prior_q"] == pytest.approx(25.11)
    assert fin["fcf"]["prior_q"] == pytest.approx((25388 - 7826) * 1e6)


def test_comparatives_two_column_layout_and_existing_values_kept():
    from pipeline.compute.comparatives import apply_press_comparatives

    extracted = {
        "revenue": {"raw": "94,930", "unit": "millions", "source_quote": "Total net sales 94,930 89,498 391,035 383,285"},
        "prior_year_comparables": {"revenue": {"raw": "89,498"}},
        "operating_income_gaap": {"raw": "29,591", "unit": "millions", "source_quote": "Operating income 29,591 (1,200) 123,216"},
    }
    fin = {
        "revenue": {"value": 94930e6, "yoy_pct": 0.5, "qoq_pct": None},
        "operating_margin_gaap": {"value": 29591 / 94930, "yoy_pp": None, "qoq_pp": None},
    }
    apply_press_comparatives(fin, extracted)
    assert fin["revenue"]["yoy_pct"] == 0.5  # 已有值不覆盖
    assert fin["revenue"]["qoq_pct"] is None  # 两列版式没有上季
    # 去年同季亏损要保留负号
    assert fin["operating_margin_gaap"]["year_ago"] == pytest.approx(-1200 / 89498)


def test_comparatives_reject_row_without_anchor():
    from pipeline.compute.comparatives import press_release_comparatives

    extracted = {
        "revenue": {"raw": "100", "unit": "millions", "source_quote": "Revenue 120 110 90"},
        "prior_year_comparables": {"revenue": {"raw": "90"}},
    }
    assert press_release_comparatives(extracted) == {}


# ---- 指引期间归一 / 合并 -------------------------------------------------------------

def test_guidance_merge_normalizes_period_and_keeps_directional():
    from pipeline.commands.run_cmd import merge_guidance_items
    from pipeline.extract.guidance import normalize_period

    assert normalize_period("FQ1-27") == normalize_period("Q1 Fiscal 2027") == "FY2027Q1"
    assert normalize_period("first quarter fiscal 2027") == "FY2027Q1"
    press = [{"metric_key": "revenue", "period": "FQ1-27", "mid": 61.5e9, "source": "press_release"},
             {"metric_key": "opex_gaap", "period": "FQ1-27", "mid": 2.31e9, "source": "press_release"}]
    call = [{"metric_key": "revenue", "period": "Q1 Fiscal 2027", "mid": 61.5e9, "source": "prepared_remarks"},
            {"metric_key": "opex_gaap", "period": "first quarter fiscal 2027", "mid": None, "direction": "up", "source": "qa"}]
    warnings: list[str] = []
    out = merge_guidance_items(press, call, warnings)
    assert len(out) == 3
    assert out[0]["confirmed_by"] == "prepared_remarks"
    assert warnings == []


# ---- 问答 ----------------------------------------------------------------------------

def test_clean_firm_and_failed_items_not_evasive():
    from pipeline.analyze.qa import _fallback_item, clean_firm
    from pipeline.compute.topics import compute_topic_stats

    assert clean_firm("Operator: the line of Atif Malik from Citi.\nAtif Malik: hi") == "Citi"
    assert clean_firm("Operator: Next is Joseph Moore with Morgan Stanley. Your line is open.") == "Morgan Stanley"
    item = _fallback_item({"exchange_id": "1", "text": "x"}, "解析失败")
    assert compute_topic_stats([item])["evasive_list"] == []


# ---- 页面 ----------------------------------------------------------------------------

def test_change_formatting():
    from builder.build import _change

    assert _change(0.308)["text"] == "+30.8%"
    assert _change(455.4)["text"] == "456 倍"
    assert _change(0.021, pp=True)["text"] == "+2.1pp"
    assert _change(None)["cls"] == "na"


def test_other_guidance_uses_raw_text():
    from builder.build import _guidance_value

    rng, mid = _guidance_value({"metric_key": "other", "mid": 75.0, "point_raw": "more than 75%"})
    assert rng == "more than 75%" and "$" not in rng


def test_build_site_renders(tmp_path, monkeypatch):
    import builder.build as b

    monkeypatch.setattr(b, "site_dir", lambda: tmp_path)
    pages = b.build_site()
    html = (tmp_path / "stocks" / "MU" / "FY2026Q4.html").read_text(encoding="utf-8")
    assert "预期差" in html and "增长质量" in html and "{{" not in html
    # 明细默认折叠在证据层，图表随「财务」块展开时绘制
    assert '<details class="ev" id="fin">' in html and "revChart" in html
    assert (tmp_path / "static" / "vendor" / "chart.umd.min.js").exists()
    assert any(p.name == "index.html" for p in pages)


def test_guidance_view_tiles_and_outlook_categories():
    from builder.build import _guidance_view, _outlook_category

    doc = json.loads((ROOT / "data" / "MU" / "FY2026Q4.json").read_text(encoding="utf-8"))
    v = _guidance_view(doc)
    labels = [t["label"] for t in v["tiles"]]
    assert labels[:2] == ["营收", "毛利率"] and "每股收益" in labels
    eps = next(t for t in v["tiles"] if t["label"] == "每股收益")
    assert eps["value"] == "$38.15" and "GAAP $37.84" in eps["sub"] and eps["vs"]["text"] == "+11.1%"
    # 每条指引只出现一次：要么在格子里，要么在展望里
    n_items = len(doc["guidance"]["items"])
    assert v["outlook_count"] < n_items
    assert _outlook_category({"metric_key": "other", "metric_label": "HBM Pricing", "statement": "盈利差距"}) == "pricing"
    assert _outlook_category({"metric_key": "other", "metric_label": "1-delta DRAM ramp", "statement": "产能爬坡"}) == "tech"
    assert _outlook_category({"metric_key": "capex", "statement": "洁净室建设"}) == "invest"
    assert _outlook_category({"metric_key": "other", "metric_label": "Market conditions", "statement": "市场状况将保持紧张"}) == "supply"


def test_judgment_layer_rules():
    from builder.build import _gap_rows, _market_view, _quality_checks, _verdict_chips

    doc = json.loads((ROOT / "data" / "MU" / "FY2026Q4.json").read_text(encoding="utf-8"))
    gaps = _gap_rows(doc)
    by = {g["label"]: g for g in gaps}
    assert by["本季营收"]["has"] is False  # 缺一致预期不能当成符合预期
    assert by["下季 EPS 指引"]["gap"]["text"] == "+11.1%" and by["下季 EPS 指引"]["pos"] == pytest.approx(87.1, abs=0.1)
    market = _market_view(doc)
    assert market["stance"]["text"] == "市场认可" and market["revision"]["label"] == "T+1"
    from pipeline.config import load_ticker_config

    qc = _quality_checks(doc, load_ticker_config("MU"))
    checks = {c["name"]: (c["status"], c["layer"]) for c in qc}
    # 公司层覆盖了通用层的现金转化阈值；口径差仍走通用层
    assert checks["自由现金流 / 净利润"] == ("ok", "本公司")
    assert checks["Non-GAAP 比 GAAP EPS 高出"] == ("ok", "通用")
    assert checks["下季毛利率指引 − 本季毛利率"][0] == "warn"
    assert checks["资本开支环比 − 营收环比"][0] == "warn"
    chips = [c["text"] for c in _verdict_chips(doc, gaps, market, checks=qc)]
    assert any("缺一致预期" in c for c in chips) and any(c.startswith("市场认可") for c in chips)


def test_gap_row_clips_outside_scale():
    from builder.build import _gap_rows

    doc = {"scorecard": [{"metric": "eps", "actual": 1.5, "verdict": "beat",
                          "benchmark": {"value": 1.0, "diff_pct": 0.5}}]}
    row = _gap_rows(doc)[0]
    assert row["pos"] == 100 and row["clipped"] is True


def test_market_view_flags_disagreement():
    from builder.build import _market_view

    doc = {"price_reaction": {"next_day_pct": -0.04},
           "guidance": {"analyst_revisions": {"t_minus_1": {"eps": 2.0}, "t_plus_1": {"eps": 2.2, "eps_chg": 0.1}}}}
    assert _market_view(doc)["stance"]["text"] == "市场分歧"


def test_benchmark_reaction_excess(monkeypatch):
    from pipeline.commands import run_cmd
    from pipeline.sources import yfinance_src

    def fake(self, release, release_timing="amc"):
        pct = 0.01 if self.ticker == "SOXX" else 0.04
        return {"next_day_pct": pct, "before_date": "2026-09-30", "after_date": "2026-10-01"}

    monkeypatch.setattr(yfinance_src.YFinanceSource, "__init__", lambda self, t: setattr(self, "ticker", t.upper()))
    monkeypatch.setattr(yfinance_src.YFinanceSource, "next_day_reaction", fake)
    own = fake(type("X", (), {"ticker": "MU"})(), "")
    out = run_cmd._benchmark_reaction({"benchmark": "SOXX"}, "2026-09-30T20:00:00Z", "amc", own)
    assert out["benchmark"] == "SOXX" and out["excess_pct"] == pytest.approx(0.03)
    # 交易日对不上时不给超额
    own2 = {**own, "after_date": "2026-10-02"}
    assert run_cmd._benchmark_reaction({}, "x", "amc", own2)["excess_pct"] is None


# ---- 仓位档案 -------------------------------------------------------------------------

def test_load_position_stages():
    from pipeline.config import load_position

    mu, sndk, mcd = load_position("MU"), load_position("SNDK"), load_position("MCD")
    assert (mu["stage"], mu["stage_label"]) == ("watch", "观察仓") and mu["questions"] and not mu["theses"]
    assert sndk["stage"] == "hold" and len(sndk["theses"]) == 2 and {t["name"] for t in sndk["triggers"]} == {"加仓", "卖出"}
    assert mcd["stage"] == "wait" and mcd["triggers"][0]["name"] == "买入"
    assert sndk["draft"] and not mu["draft"]  # 还有「【请填写】」的档案提示未定稿
    assert load_position("NOPE") is None


def test_position_view_watch_shows_questions_and_facts():
    from builder.build import _position_view

    doc = json.loads((ROOT / "data" / "MU" / "FY2026Q4.json").read_text(encoding="utf-8"))
    doc.pop("thesis_review", None)
    v = _position_view("MU", doc, date(2026, 10, 2))
    assert v["stage"] == "watch" and len(v["questions"]) == 3 and not v["reviewed"]
    assert any("75%" in f["number"] for f in v["facts"])  # 问答里管理层新给出的数字，不调用 LLM


def test_position_view_hold_with_review(monkeypatch):
    from builder import build
    from builder.build import _position_view, _verdict_chips

    pos = {"stage": "hold", "stage_label": "持仓", "updated": "", "draft": False,
           "theses": [{"bull": "a", "bear": "b", "falsify": "营收 < 60B（2026-12-23）"}, {"bull": "c", "bear": "d", "falsify": "e"}],
           "triggers": [{"name": "加仓", "text": "x"}, {"name": "卖出", "text": "y"}], "questions": []}
    monkeypatch.setattr(build, "load_position", lambda t: pos)
    doc = {"thesis_review": {"stage": "hold", "fingerprint": "f", "reviewed_at": "2026-10-02T00:00:00",
                             "theses": [{"index": 0, "status": "strengthened", "evidence": "ok"},
                                        {"index": 1, "status": "weakened", "falsified": True, "quote": "q", "quote_unverified": True}],
                             "triggers": [{"name": "卖出", "state": "triggered", "reason": "证伪出现"}]}}
    v = _position_view("X", doc, date(2026, 10, 2))
    assert [t["status"] for t in v["theses"]] == ["强化", "已证伪"]
    assert v["theses"][0]["deadline"]["left"] == "还有 82 天" and v["theses"][1]["quote"] == ""
    sell = next(t for t in v["triggers"] if t["name"] == "卖出")
    assert sell["state"] == "已触发" and sell["cls"] == "bad"
    chips = [c["text"] for c in _verdict_chips({}, [], {"stance": None, "price": {"text": "—"}}, [], v)]
    assert "论点：1 强化 · 1 已证伪" in chips and "卖出条件已触发" in chips
    # 旧格式评估（没有 stage）不算已评估
    doc["thesis_review"].pop("stage")
    assert _position_view("X", doc, date(2026, 10, 2))["reviewed"] is False


def test_position_review_skips_when_unchanged_or_nothing_to_ask(monkeypatch):
    from pipeline.analyze import thesis as th
    from pipeline.commands import run_cmd
    from pipeline import config

    doc = json.loads((ROOT / "data" / "MU" / "FY2026Q4.json").read_text(encoding="utf-8"))
    calls = []
    monkeypatch.setattr(th, "review_position", lambda pos, d: calls.append(1) or {"fingerprint": th.review_fingerprint(pos, d)})
    assert run_cmd._apply_thesis_review("MU", doc) is True
    assert run_cmd._apply_thesis_review("MU", doc) is False  # 档案和数据都没变：不调用
    assert run_cmd._apply_thesis_review("MU", doc, force=True) is True
    # 观察仓没写问题：不调用
    monkeypatch.setattr(config, "load_position", lambda t: {"stage": "watch", "theses": [], "triggers": [], "questions": []})
    assert run_cmd._apply_thesis_review("MU", doc, force=True) is False
    assert len(calls) == 2


def test_review_position_filters_and_checks_quotes(monkeypatch):
    from pipeline.analyze import thesis as th
    from pipeline.config import load_position
    from pipeline.schemas import PositionReviewResult

    doc = json.loads((ROOT / "data" / "MU" / "FY2026Q4.json").read_text(encoding="utf-8"))
    real_quote = next(q["answer_quote"] for q in doc["qa"]["items"] if q.get("answer_quote"))
    captured = {}

    class FakeLLM:
        model = "fake"

        def load_prompt(self, name, **kw):
            return "P " + kw["position"]

        def complete_json(self, name, user, schema):
            captured["user"] = user
            return PositionReviewResult.model_validate({
                "answers": [
                    {"index": 0, "answered": True, "answer": "a", "quote": real_quote},
                    {"index": 1, "answered": True, "answer": "b", "quote": "完全编造的引文不在原文里"},
                    {"index": 9, "answered": True, "answer": "越界"},
                ],
                "triggers": [{"name": "不存在的条件", "state": "triggered"}],
            })

    monkeypatch.setattr(th, "LLMClient", FakeLLM)
    out = th.review_position(load_position("MU"), doc)
    assert [a["index"] for a in out["answers"]] == [0, 1]
    assert out["answers"][0]["quote_unverified"] is False and out["answers"][1]["quote_unverified"] is True
    assert out["triggers"] == [] and out["stage"] == "watch"
    assert "观察仓" in captured["user"] and len(captured["user"]) < 60000

