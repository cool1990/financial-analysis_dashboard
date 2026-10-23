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


def test_press_release_matches_exhibit991_filename():
    """META 最新 8-K 的新闻稿文件名是 exhibit991.htm，不是 EX-99.1。"""
    from pipeline.sources.sec_edgar import SecEdgarClient

    client = SecEdgarClient.__new__(SecEdgarClient)
    client.filing_index = lambda cik, accession: {
        "directory": {
            "item": [
                {"name": "meta-20261019.htm"},
                {"name": "meta10202026-ex101.htm"},
                {"name": "meta-09302026xexhibit991.htm"},
                {"name": "ex99_1.htm"},
            ]
        }
    }
    assert client.find_press_release_files("0001326801", "0001628280-26-066700") == [
        "ex99_1.htm",
        "meta-09302026xexhibit991.htm",
    ]


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
    assert out["reported"] == []


def test_poll_did_work_only_when_stage_runs(monkeypatch, tmp_path):
    from pipeline.commands import poll_cmd

    monkeypatch.setattr(poll_cmd, "list_tickers", lambda: ["NVDA"])
    monkeypatch.setattr(poll_cmd, "load_ticker_config", lambda t: {"release_timing": "amc", "cik": "0001045810"})
    monkeypatch.setattr(poll_cmd, "data_dir", lambda t=None: tmp_path)
    monkeypatch.setattr(poll_cmd, "_in_window", lambda *a, **k: True)
    monkeypatch.setattr(poll_cmd, "near_earnings", lambda *a, **k: True)
    monkeypatch.setattr(poll_cmd, "detect_new_filings", lambda *a, **k: [])
    monkeypatch.setattr(poll_cmd, "run_stage1", lambda *a, **k: (_ for _ in ()).throw(AssertionError("没有新 8-K 不应跑 Stage1")))
    monkeypatch.setattr(poll_cmd, "run_stage2", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应跑 Stage2")))
    quiet = poll_cmd.poll_once(datetime(2026, 10, 23, 21, 0, tzinfo=timezone.utc))
    assert quiet["did_work"] is False and quiet["reported"] == []
    assert quiet["actions"][0]["detail"] == "no new 8-K"

    monkeypatch.setattr(poll_cmd, "detect_new_filings", lambda *a, **k: [{"accessionNumber": "new"}])
    monkeypatch.setattr(poll_cmd, "run_stage1", lambda ticker: {"meta": {"fiscal_period": "FY2027Q3"}})
    reported = poll_cmd.poll_once(datetime(2026, 10, 23, 21, 0, tzinfo=timezone.utc))
    assert reported["did_work"] is True and reported["reported"] == ["NVDA"]

    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "FY2027Q3.json").write_text(
        json.dumps({"stage": "stage1_done", "fiscal_period": "FY2027Q3"}), encoding="utf-8"
    )
    (tmp_path / "FY2027Q3.json").write_text(
        json.dumps({"meta": {"release_at_utc": "2026-10-23T20:00:00Z"}}), encoding="utf-8"
    )
    stage2 = []
    monkeypatch.setattr(poll_cmd, "detect_new_filings", lambda *a, **k: [])
    monkeypatch.setattr(poll_cmd, "run_stage1", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应跑 Stage1")))
    monkeypatch.setattr(poll_cmd, "run_stage2", lambda *a, **k: stage2.append(a))
    retried = poll_cmd.poll_once(datetime(2026, 10, 23, 22, 0, tzinfo=timezone.utc))
    assert stage2 == [("NVDA", "FY2027Q3")]
    assert retried["did_work"] is True and retried["reported"] == []


def test_stage1_missing_period_end_uses_quarter_before_release_and_keeps_zero_eps():
    from pipeline.commands.run_cmd import resolve_stage1_period_end, select_eps_actual
    from pipeline.compute.periods import build_period_info

    kept, inferred, label = resolve_stage1_period_end("2026-09-30", "2026-10-21T20:00:00Z", 12)
    assert (kept, inferred, label) == ("2026-09-30", False, None)

    # 11 月 20 日发布的仍是 9 月 30 日那季；直接拿发布日会标成 Q4
    end, inferred, label = resolve_stage1_period_end(None, "2026-11-20T21:00:00Z", 12)
    assert inferred is True and end == "2026-09-30" and label == "FY2026Q3"
    assert build_period_info("2026-11-20", 12).fiscal_period == "FY2026Q4"
    assert end != "2026-11-20"

    # MU：8 月财年末，9 月 30 日发布对应 8 月 31 日结束的 Q4，不是发布日
    end, inferred, label = resolve_stage1_period_end("", "2026-09-30T20:00:00Z", 8)
    assert inferred is True and end == "2026-08-31" and label == "FY2026Q4"

    try:
        resolve_stage1_period_end(None, "", 12)
    except ValueError as exc:
        assert "period_end_date" in str(exc)
    else:
        raise AssertionError("推不出季末时应失败")

    actual, warnings = select_eps_actual("unknown", 1.25, 0.0)
    assert actual == 0.0 and warnings == ["eps_basis=unknown，记分卡待核对"]
    actual, warnings = select_eps_actual("non_gaap", 1.25, 0.0)
    assert actual == 0.0 and warnings == []
    actual, warnings = select_eps_actual("gaap", 0.0, None)
    assert actual == 0.0 and warnings == []
    actual, warnings = select_eps_actual("non_gaap", 1.25, None)
    assert actual == 1.25 and warnings == ["本季未披露 Non-GAAP EPS，改用 GAAP"]


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
    assert normalize_period("fourth quarter 2026") == normalize_period("Q4 FY2026") == "FY2026Q4"
    assert normalize_period("full year 2026") == normalize_period("fiscal 2026") == "FY2026"
    assert normalize_period("2027") == normalize_period("fiscal 2027") == "FY2027"
    assert normalize_period("beyond 2027") != "FY2027"
    assert normalize_period("calendar 2027") != "FY2027"
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

def test_structure_qa_prompt_asks_for_a_full_answer():
    text = (ROOT / "prompts" / "structure_qa.md").read_text(encoding="utf-8")
    assert "不超过两句" not in text
    assert "30 个英文单词" not in text
    assert "输出务必紧凑" not in text
    assert "3 到 6 句" not in text
    assert "可以到 8 句" not in text
    assert "最多 8 句" not in text
    assert "不要设总句数上限" in text
    assert "不要用「文本不足」" in text
    # 每段点名发言人；职务已知才写。能对上名字时不用「管理层表示」带过。
    assert "Mark（CEO）" in text and "Susan（CFO）" in text
    assert "能对上名字时不要写成「管理层表示」" in text
    assert "用换行符 \\n" in text
    assert "每个独立问题" in text or "互不相关的问题" in text
    assert "现实实验室" in text
    assert "six weeks" in text
    # 投资者能读的复述，不要硬译 compute-intensive。
    assert "不要把英文术语硬译" in text
    assert "忠实原文" in text and "逻辑清晰" in text and "通俗易懂" in text
    assert "服务计算密集" in text
    assert "算力消耗大" in text
    assert "商业化是否和降本同步" in text
    assert "不要编造数字、产品或表态" in text
    from pipeline.analyze.qa import _is_placeholder_qa

    assert _is_placeholder_qa({"question_summary": "文本不足", "answer_summary": "文本不足"})
    assert not _is_placeholder_qa({"question_summary": "问成本", "answer_summary": "今天没有具体数字，降本要靠闲置算力和更小的模型。"})


def test_qa_template_shows_full_speaker_lines():
    """多位发言人的摘要按行完整展示，不截断、不丢掉名字。"""
    from builder.build import _env, _qa_answer_lines

    text = (
        "Mark（CEO）：最大约束之一是产品算力消耗大，所以还没有全球上线。"
        "一部分要针对各地优化接入，一部分要先把运行效率做上去才能放量。"
        "算力要能服务十亿以上用户，这是未来一年的限制因素之一。\n"
        "- Susan（CFO）补充：商业化要和降本同步，目前没有单位成本数字。"
    )
    lines = _qa_answer_lines(text)
    assert lines == [
        "Mark（CEO）：最大约束之一是产品算力消耗大，所以还没有全球上线。一部分要针对各地优化接入，一部分要先把运行效率做上去才能放量。算力要能服务十亿以上用户，这是未来一年的限制因素之一。",
        "Susan（CFO）补充：商业化要和降本同步，目前没有单位成本数字。",
    ]
    assert _qa_answer_lines("  ") == []
    assert _qa_answer_lines("管理层：只有一位，整段都要留下，包括 1000 万用户。") == [
        "管理层：只有一位，整段都要留下，包括 1000 万用户。"
    ]

    tpl = (ROOT / "builder" / "templates" / "period.html").read_text(encoding="utf-8")
    qa_body = tpl.split('class="qa-body"', 1)[1].split("{% if q.note %}", 1)[0]
    assert "qa_lines" in qa_body
    assert "truncate" not in qa_body
    assert "[:" not in qa_body
    css = (ROOT / "builder" / "static" / "style.css").read_text(encoding="utf-8")
    qa_css = css.split("/* ---- 问答 ---- */", 1)[1].split("/* ---- 跟踪 ---- */", 1)[0]
    assert "qa-speakers" in qa_css
    assert "line-clamp" not in qa_css

    start = tpl.index("{% set answer_lines")
    end = tpl.index("{% endif %}", start) + len("{% endif %}")
    snippet = tpl[start:end]
    render = _env().from_string(snippet).render
    html = render(q={"answer_summary": text})
    assert "Mark（CEO）" in html and "Susan（CFO）" in html
    assert "十亿以上用户" in html and "商业化要和降本同步" in html
    assert "<li>" in html
    assert "…" not in html and "服务计算密集" not in html
    one = render(q={"answer_summary": "Mark（CEO）：整段回答都要显示，不要截成两句。"})
    assert "Mark（CEO）：整段回答都要显示，不要截成两句。" in one
    assert "<li>" not in one
    assert one.endswith("</p>") or "</p>" in one
    flag_at = tpl.index("{% if q.review_flags %}")
    flag_end = tpl.index("{% endif %}", flag_at) + len("{% endif %}")
    flagged = _env().from_string(tpl[flag_at:flag_end]).render(q={"review_flags": ["漏了 6 weeks"]})
    assert "核对：" in flagged and "6 weeks" in flagged
    assert "核对" not in render(q={"answer_summary": "Mark（CEO）：只有一段。", "review_flags": []})


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


def _position(stage: str) -> dict:
    return {
        "stage": stage,
        "stage_label": {"hold": "持仓", "wait": "等待仓", "watch": "观察仓"}[stage],
        "updated": "",
        "theses": [],
        "triggers": [],
        "questions": [],
        "group": "",
        "kind": "",
        "target_value": "",
        "draft": False,
    }


def _render_fixture_site(monkeypatch, tmp_path):
    """用冻结夹具建站，不读 data/ 里会随 daily 变化的现网文案。"""
    import builder.build as b
    import pipeline.commands.poll_cmd as poll_cmd
    import pipeline.sources.av_estimates as av_estimates

    data = tmp_path / "data"
    (data / "FIX").mkdir(parents=True)
    (data / "AAA").mkdir()
    doc = json.loads((ROOT / "tests" / "fixtures" / "mu_fy2025q4_mock.json").read_text(encoding="utf-8"))
    doc["meta"]["ticker"] = "FIX"
    doc["meta"]["fiscal_period"] = "FY2026Q1"
    doc["meta"]["release_at_utc"] = None
    (data / "FIX" / "FY2026Q1.json").write_text(json.dumps(doc), encoding="utf-8")
    # 已披露 FY2026Q1、财年末 12 月 → 当前季结束日 2026-06-30。horizon 用真实缓存里的 fiscal quarter。
    # 下修为 null，页面应显示 2 / 0。fetched_at 远早于今天，格子标旧。
    av = {
        "symbol": "FIX",
        "function": "EARNINGS_ESTIMATES",
        "fetched_at": "2020-01-01T00:00:00+00:00",
        "estimates": [
            {
                "date": "2024-06-30",
                "horizon": "fiscal quarter",
                "eps_estimate_average": "0.1000",
                "eps_estimate_analyst_count": "3",
            },
            {
                "date": "2026-06-30",
                "horizon": "fiscal quarter",
                "eps_estimate_average": "1.5000",
                "eps_estimate_analyst_count": "8",
                "eps_estimate_revision_up_trailing_30_days": "2",
                "eps_estimate_revision_down_trailing_30_days": None,
            },
        ],
    }
    consensus = data / "FIX" / "consensus"
    consensus.mkdir()
    (consensus / "av_estimates.json").write_text(json.dumps(av), encoding="utf-8")

    def data_dir(ticker=None):
        return data if not ticker else data / str(ticker).upper()

    configs = {
        "FIX": {"name": "Fixture Co", "fiscal_year_end_month": 12, "release_timing": "amc", "eps_basis": "non_gaap"},
        "AAA": {"name": "Alpha Hold", "fiscal_year_end_month": 12, "release_timing": "amc", "eps_basis": "gaap"},
    }
    positions = {"FIX": _position("watch"), "AAA": _position("hold")}
    for mod in (b, poll_cmd, av_estimates):
        monkeypatch.setattr(mod, "data_dir", data_dir)
    monkeypatch.setattr(b, "list_tickers", lambda: ["FIX", "AAA"])
    monkeypatch.setattr(b, "load_ticker_config", lambda ticker: configs[ticker])
    monkeypatch.setattr(b, "load_position", lambda ticker: positions[ticker])
    for key in ("ALPHAVANTAGE_API_KEY", "ALPHAVANTAGE_KEY", "ALPHA_VANTAGE_API_KEY", "HERMES_AV_ESTIMATES_DIR"):
        monkeypatch.delenv(key, raising=False)

    out = tmp_path / "site"
    pages = b.build_site(out_dir=out)
    return out, pages


def test_build_site_renders(tmp_path, monkeypatch):
    out, pages = _render_fixture_site(monkeypatch, tmp_path)
    html = (out / "stocks" / "FIX" / "FY2026Q1.html").read_text(encoding="utf-8")
    assert "盈利预期" in html and "{{" not in html
    assert "Fixture Co" in html and "$11.31B" in html
    assert "增长质量" not in html and "下季验证清单" not in html and "观察仓" not in html
    assert 'id="quality"' not in html and 'id="thesis"' not in html
    assert "市场怎么判" not in html and "次日涨跌" not in html
    assert '<details class="ev" id="fin" open>' in html and 'data-series="level"' in html
    assert "财报分析" in html and "指引展望" in html and "问答交流" in html and "数据来源" in html
    assert "revChart" not in html and "marginChart" not in html and "fcfChart" not in html
    assert 'id="epsSurprise"' not in html
    assert "再点同一行就收起" in html and "function collapseFin()" in html
    assert "财务数据" not in html and "业务拆分" not in html
    assert "估值与技术" not in html and 'class="card quote-card"' not in html
    assert "超预期 $0.86" not in html and "$697.52" not in html
    assert "ALPHAVANTAGE_API_KEY" not in html
    assert (out / "static" / "vendor" / "chart.umd.min.js").exists()
    assert any(p.name == "index.html" for p in pages)


def test_build_cli_writes_only_the_out_dir(tmp_path, monkeypatch):
    from pipeline.__main__ import main

    out, _pages = _render_fixture_site(monkeypatch, tmp_path)
    dest = tmp_path / "cli-out"
    assert main(["build", "--out", str(dest)]) == 0
    assert (dest / "index.html").is_file()
    assert (dest / "stocks" / "FIX" / "FY2026Q1.html").is_file()
    assert dest != out


def test_meta_quote_card_divides_tradingview_columns():
    from builder.build import _forward_pe_view, _header_quotes
    from pipeline.config import load_ticker_config

    snap = json.loads((ROOT / "data" / "META" / "consensus" / "tradingview_scan.json").read_text(encoding="utf-8"))
    cols = snap["quote"]["columns"]
    assert cols["price_earnings_forward"] is None and cols["dividend_yield_ttm"] is None
    doc = json.loads((ROOT / "data" / "META" / "FY2026Q3.json").read_text(encoding="utf-8"))
    header = _header_quotes("META", doc, "FY2026Q3", True)
    by = {item["label"]: item for item in header["items"]}
    assert by["股价"]["value"] == f"${cols['close']:.2f}"
    assert by["股息率"]["value"] == f"{cols['dividends_yield']:.2f}%"
    assert by["RSI"]["value"] == f"{cols['RSI']:.1f}"
    assert by["市值"]["value"] == "$1794.7B"
    forward = _forward_pe_view("META", doc, "FY2026Q3", load_ticker_config("META"), active=True)
    assert forward["price"] == f"${cols['close']:.2f}"
    rows = {row["label"]: row for row in forward["rows"]}
    # NTM 按剩余月份加权表内两个财年，不再用 close ÷ eps_estimate_ntm 单独占一格。
    assert rows["未来 12 个月（NTM）"]["eps_raw"] == pytest.approx(
        3 / 12 * rows["FY26E"]["eps_raw"] + 9 / 12 * rows["FY27E"]["eps_raw"]
    )
    assert "vs" not in rows["近 4 季（TTM）"]
    assert rows["近 4 季（TTM）"]["pe"] == "22.5×"
    assert "FY28E" not in rows
    assert abs(rows["未来 12 个月（NTM）"]["eps_raw"] - cols["eps_estimate_ntm"]) < 1.5


def test_header_eps_guidance_uses_only_management_nongaap():
    """页头两格只认管理层 Non-GAAP EPS。META 没给就写「无」，不用一致预期，也不用 GAAP。"""
    from builder.build import _header_quotes, _management_eps_cells

    meta = json.loads((ROOT / "data" / "META" / "FY2026Q3.json").read_text(encoding="utf-8"))
    cells = {c["label"]: c for c in _management_eps_cells(meta, "FY2026Q3")}
    assert cells["下季度 EPS 指引"]["value"] == "无"
    assert cells["年度 EPS 指引"]["value"] == "无"
    assert cells["下季度 EPS 指引"]["record"]["provided"] is False
    assert cells["下季度 EPS 指引"]["record"]["source"] is None
    assert cells["下季度 EPS 指引"]["record"]["period"] == "FY2026Q4"
    assert cells["年度 EPS 指引"]["record"]["period"] == "FY2026"
    assert "8.29" not in cells["下季度 EPS 指引"]["value"]
    assert "32.03" not in cells["下季度 EPS 指引"]["source"]
    header = _header_quotes("META", meta, "FY2026Q3", True)
    by = {item["label"]: item for item in header["items"]}
    assert by["股价"]["value"] == "$697.52"
    assert by["下季度 EPS 指引"]["value"] == "无" and by["年度 EPS 指引"]["value"] == "无"
    assert "32.03" not in by["下季度 EPS 指引"]["value"] and "8.29" not in by["下季度 EPS 指引"]["value"]
    assert header["market"] is True
    assert header["line"] == "数据时间 2026-10-21 · 来源 TradingView"

    mu = json.loads((ROOT / "data" / "MU" / "FY2026Q4.json").read_text(encoding="utf-8"))
    mu_cells = {c["label"]: c for c in _management_eps_cells(mu, "FY2026Q4")}
    nq = mu_cells["下季度 EPS 指引"]
    assert nq["value"] == "$38.15"
    assert nq["record"]["provided"] is True
    assert nq["record"]["source"] == "press_release"
    assert nq["record"]["metric_key"] == "eps_nongaap"
    assert nq["record"]["period"] == "FY2027Q1"
    assert nq["record"]["source_quote"] == "Diluted earnings per share $38.15 ± $1.00"
    assert "Exhibit 99.1" in nq["source"] and "电话会" in nq["source"]
    assert "$37.84" not in nq["value"]
    assert mu_cells["年度 EPS 指引"]["value"] == "无"
    assert mu_cells["年度 EPS 指引"]["record"]["period"] == "FY2027"
    mu_header = _header_quotes("MU", mu, "FY2026Q4", True)
    assert mu_header["market"] is False
    assert mu_header["line"] == "数据时间 2026-09-30 · 来源 新闻稿 / 电话会"
    assert meta["guidance"]["management_eps"]["next_quarter"]["display"] == "无"
    assert meta["guidance"]["management_eps"]["next_quarter"]["source"] is None
    assert mu["guidance"]["management_eps"]["next_quarter"]["display"] == "$38.15"
    assert mu["guidance"]["management_eps"]["next_quarter"]["source"] == "press_release"
    assert mu["guidance"]["management_eps"]["next_quarter"]["source_quote"].startswith("Diluted earnings per share")
    assert mu["guidance"]["management_eps"]["full_year"]["display"] == "无"

    # 只有 GAAP、或只有营收区间、或只有一致预期，两格都是「无」
    gaap_only = {
        "guidance": {"items": [
            {"metric_key": "eps_gaap", "period": "FY2026Q4", "basis": "GAAP", "mid": 9.5, "point_raw": "$9.50", "source": "press_release"},
            {"metric_key": "revenue", "period": "FY2026", "basis": "gaap", "low_raw": "$1 billion", "high_raw": "$2 billion", "low": 1e9, "high": 2e9, "mid": 1.5e9, "source": "press_release"},
        ]},
        "scorecard": [{"metric": "next_q_eps_guidance", "benchmark": {"value": 8.29, "source": "consensus"}}],
    }
    blank = {c["label"]: c for c in _management_eps_cells(gaap_only, "FY2026Q3")}
    assert blank["下季度 EPS 指引"]["value"] == "无" and blank["年度 EPS 指引"]["value"] == "无"

    # 电话会里的全年 Non-GAAP 区间记来源，不改写成中值以外的数字
    call_year = {
        "guidance": {"items": [{
            "metric_key": "eps",
            "period": "full year 2026",
            "type": "numeric",
            "basis": "adjusted",
            "low_raw": "$7.10",
            "high_raw": "$7.40",
            "low": 7.10,
            "high": 7.40,
            "mid": 7.25,
            "source": "prepared_remarks",
            "source_quote": "Adjusted EPS of $7.10 to $7.40",
        }]}
    }
    year = {c["label"]: c for c in _management_eps_cells(call_year, "FY2026Q3")}["年度 EPS 指引"]
    assert year["value"] == "$7.10 – $7.40"
    assert year["record"]["source"] == "prepared_remarks"
    assert year["record"]["period"] == "FY2026"
    assert "电话会" in year["source"] and "原文：Adjusted EPS of $7.10 to $7.40" in year["source"]
    assert {c["label"]: c for c in _management_eps_cells(call_year, "FY2026Q3")}["下季度 EPS 指引"]["value"] == "无"


def test_guide_section_splits_performance_and_industry():
    """指引展望的两半：有数字的管理层指引进表，资本开支有数字才占行；行业展望用已写入的正文。"""
    from builder.build import _guide_section, _load_docs

    meta = json.loads((ROOT / "data" / "META" / "FY2026Q3.json").read_text(encoding="utf-8"))
    view = _guide_section(meta, _load_docs("META"))
    assert [row["label"] for row in view["rows"]] == ["营收", "资本开支"]
    revenue = view["rows"][0]
    assert revenue["value"] == "$73.00B – $76.00B"
    assert revenue["qoq"]["text"] == "+15.3%" and revenue["qoq"]["cls"] == "up"
    assert revenue["yoy"]["text"] == "+24.4%"
    assert revenue["vs"]["text"] == "+0.5%"
    capex = view["rows"][1]
    assert capex["period"] == "FY2026全年"
    assert capex["value"] == "$135.00B – $145.00B"
    assert capex["qoq"]["text"] == "" and capex["vs"]["text"] == ""
    assert capex["yoy"]["text"] == "+100.9%" and capex["yoy"]["cls"] == "up"
    titles = [block["title"] for block in view["outlook"]]
    assert titles == ["外部环境（技术变化）", "需求变化", "供给变化", "竞争变化", "商业模式", "公司战略"]
    demand = next(block for block in view["outlook"] if block["title"] == "需求变化")
    demand_text = "".join(piece.get("text") or "" for piece in demand["blocks"])
    assert "1000 万人" in demand_text and "25%" in demand_text
    strategy = next(block for block in view["outlook"] if block["title"] == "公司战略")
    assert any(piece.get("type") == "ol" and len(piece.get("items") or []) == 7 for piece in strategy["blocks"])
    assert "青少年" not in json.dumps(view["outlook"], ensure_ascii=False)

    mu = json.loads((ROOT / "data" / "MU" / "FY2026Q4.json").read_text(encoding="utf-8"))
    mu_view = _guide_section(mu, _load_docs("MU"))
    mu_rows = {row["label"]: row for row in mu_view["rows"]}
    assert mu_rows["营收"]["value"] == "$61.50B" and mu_rows["营收"]["qoq"]["text"] == "+13.4%"
    assert mu_rows["营收"]["yoy"]["text"] == "" and mu_rows["营收"]["vs"]["text"] == ""
    assert mu_rows["毛利率（Non-GAAP）"]["value"] == "86.25%" and mu_rows["毛利率（Non-GAAP）"]["qoq"]["text"] == "-0.8pp"
    assert mu_rows["调整后摊薄 EPS"]["value"] == "$38.15"
    assert mu_rows["调整后摊薄 EPS"]["qoq"]["text"] == "+14.2%"
    assert mu_rows["调整后摊薄 EPS"]["vs"]["text"] == "+11.1%"
    assert "净利润率" not in mu_rows and "营业利润率" not in mu_rows and "资本开支" not in mu_rows
    assert "供给变化" in {block["title"] for block in mu_view["outlook"]}
    assert "industry_outlook" not in (mu.get("guidance") or {})


def test_capex_guidance_row_skipped_when_not_numeric():
    from builder.build import _guide_section

    doc = {
        "meta": {"fiscal_period": "FY2026Q3"},
        "guidance": {"items": [{
            "metric_key": "capex",
            "type": "qualitative",
            "period": "FY2027",
            "statement": "资本开支将继续增加",
        }]},
        "scorecard": [],
        "financials": {},
    }
    assert _guide_section(doc, {})["rows"] == []


def test_industry_outlook_prompt_teaches_depth_and_stage2_calls_deepseek_path():
    """行业展望由 Stage2 的 DeepSeek 按提示词写，不在构建时另写。"""
    prompt = (ROOT / "prompts" / "extract_industry_outlook.md").read_text(encoding="utf-8")
    assert "忠实原文" in prompt and "通俗易懂" in prompt and "不要乱编造" in prompt
    assert "有材料就写细" in prompt and "整题省略" in prompt
    assert "数字" in prompt and "因果" in prompt
    assert "一句" in prompt and "1000 万人" in prompt and "10 倍" in prompt and "150 亿" in prompt
    stage2 = (ROOT / "pipeline" / "commands" / "run_cmd.py").read_text(encoding="utf-8")
    assert "extract_industry_outlook" in stage2 and "outlook_is_thin" in stage2
    assert "行业展望过短" in stage2


def test_extract_industry_outlook_uses_longer_deepseek_budget(monkeypatch):
    from pipeline.analyze.outlook import extract_industry_outlook, outlook_is_thin
    from pipeline.schemas import IndustryOutlookResult

    captured = {}

    class Fake:
        def load_prompt(self, name, **kwargs):
            assert name == "extract_industry_outlook.md"
            return "PROMPT"

        def complete_json(self, name, user, schema, **kwargs):
            captured["name"] = name
            captured["user"] = user
            captured["max_tokens"] = kwargs.get("max_tokens")
            captured["timeout"] = kwargs.get("timeout_sec")
            return IndustryOutlookResult.model_validate({"topics": [{
                "title": "供给变化",
                "blocks": [{"type": "p", "text": "他说算力是限制，所以还没有全球上线，要先把效率做上去。" * 20}],
            }]})

    text = "Mark -- Chief Executive Officer\n算力还不够。\n\nQuestion-and-Answer Session\nMark: 先提效率。"
    topics = extract_industry_outlook(text, Fake())  # type: ignore[arg-type]
    assert captured["name"] == "extract_industry_outlook.md"
    assert captured["max_tokens"] == 8192 and captured["timeout"] == 150
    assert "电话会正文（prepared remarks）" in captured["user"]
    assert "问答" in captured["user"] and "算力还不够" in captured["user"]
    assert topics[0]["title"] == "供给变化"
    assert outlook_is_thin([{"blocks": [{"text": "监管可能影响"}]}]) is True
    assert outlook_is_thin(topics) is False


def test_guidance_view_tiles_and_outlook_categories():
    from builder.build import _guidance_view, _guide_dimension

    doc = json.loads((ROOT / "data" / "MU" / "FY2026Q4.json").read_text(encoding="utf-8"))
    v = _guidance_view(doc)
    labels = [t["label"] for t in v["tiles"]]
    assert labels[:2] == ["营收", "毛利率"] and "每股收益" in labels
    eps = next(t for t in v["tiles"] if t["label"] == "每股收益")
    assert eps["value"] == "$38.15" and "GAAP $37.84" in eps["sub"] and eps["vs"]["text"] == "+11.1%"
    # 同一指标并成一张卡，所以卡片数少于原始句子数
    n_items = len(doc["guidance"]["items"])
    assert v["outlook_count"] < n_items
    assert v["card_count"] == v["outlook_count"]
    dims = [d["key"] for d in v["dimensions"]]
    assert dims[0] == "results" and "capex" in dims and "operating" in dims
    assert all(d["cards"] for d in v["dimensions"])
    assert _guide_dimension({"metric_key": "other", "metric_label": "HBM Pricing", "statement": "盈利差距"}) == "operating"
    assert _guide_dimension({"metric_key": "other", "metric_label": "1-delta DRAM ramp", "statement": "产能爬坡"}) == "other"
    assert _guide_dimension({"metric_key": "capex", "statement": "洁净室建设"}) == "capex"
    assert _guide_dimension({"metric_key": "other", "metric_label": "Market conditions", "statement": "市场状况将保持紧张"}) == "other"
    assert _guide_dimension({"metric_key": "other", "metric_label": "buyback", "statement": "加大回购"}) == "returns"

    meta = json.loads((ROOT / "data" / "META" / "FY2026Q3.json").read_text(encoding="utf-8"))
    view = _guidance_view(meta)
    meta_dims = {d["key"]: d for d in view["dimensions"]}
    assert [k for k in ("results", "operating", "capex", "returns", "other") if k in meta_dims] == [
        "results", "operating", "capex", "returns", "other",
    ]
    cards = [c for d in view["dimensions"] for c in d["cards"]]
    assert view["card_count"] == len(cards) < len(meta["guidance"]["items"])
    rev = [c for c in cards if c["label"] == "营收"]
    assert len(rev) == 1 and rev[0]["value"] == "$73.00B – $76.00B" and rev[0]["source"] == "新闻稿 · 电话会"
    expenses = [c for c in cards if c["label"] == "全年费用"]
    assert len(expenses) == 1 and expenses[0]["value"] == "$175.00B – $179.00B"
    capex = [c for c in cards if c["label"] == "资本开支"]
    assert len(capex) == 1 and capex[0]["value"] == "$135.00B – $145.00B" and capex[0]["source"] == "新闻稿 · 电话会"
    tax = [c for c in cards if c["label"] == "税率"]
    assert len(tax) == 1 and tax[0]["value"] == "15% – 17%"
    rl = [c for c in cards if c["label"] == "Reality Labs 亏损"]
    assert {c["value"] for c in rl} >= {"$18.00B – $19.00B", "约 $15.00B"}
    fx = [c for c in cards if c["label"] == "汇率"]
    assert len(fx) == 1 and fx[0]["value"] == "约 1% 逆风"
    eff = [c for c in cards if c["label"] == "Muse 算力效率"]
    assert len(eff) == 1 and eff[0]["value"] == "超过 10×" and eff[0]["source"] == "电话会"
    assert all(c["source"] in {"新闻稿", "电话会", "新闻稿 · 电话会"} for c in cards)
    assert any(c["label"] == "拆股" and c["value"] == "1 拆 2" and c["source"] == "电话会" for c in meta_dims["returns"]["cards"])
    assert "本季已披露" not in json.dumps(cards, ensure_ascii=False)


def test_judgment_layer_rules():
    from builder.build import _gap_rows, _market_view, _quality_checks, _verdict_chips

    doc = json.loads((ROOT / "data" / "MU" / "FY2026Q4.json").read_text(encoding="utf-8"))
    gaps = _gap_rows(doc)
    by = {g["label"]: g for g in gaps}
    assert by["本季营收"]["has"] is False  # 缺一致预期不能当成符合预期
    assert by["下季 EPS 指引"]["gap"]["text"] == "+11.1%" and by["下季 EPS 指引"]["pos"] == pytest.approx(87.1, abs=0.1)
    market = _market_view(doc)
    assert market["stance"]["text"] == "市场认可" and market["revision"]["label"] == "T+7"
    from pipeline.config import load_ticker_config

    qc = _quality_checks(doc, load_ticker_config("MU"))
    checks = {c["name"]: (c["status"], c["layer"]) for c in qc}
    # 公司层覆盖了通用层的现金转化阈值；口径差仍走通用层
    assert checks["自由现金流 / 净利润"] == ("ok", "本公司")
    assert checks["Non-GAAP 比 GAAP EPS 高出"] == ("ok", "通用")
    assert checks["下季毛利率指引 − 本季毛利率"][0] == "warn"
    assert checks["资本开支环比 − 营收环比"][0] == "warn"
    chips = [c["text"] for c in _verdict_chips(doc, gaps, checks=qc)]
    assert any("缺一致预期" in c for c in chips)
    assert any(c == "本季 EPS beat 3.0%" for c in chips)
    assert all("市场认可" not in c and "次日" not in c and "Non-GAAP" not in c for c in chips)


def test_meta_eps_uses_ex_charge_against_street_consensus():
    """TradingView $6.54 is street/adjusted. Compare it to press ex-charge $7.40, not GAAP $3.83."""
    from builder.build import _fin_rows, _gap_rows, _index_row, _score_lines
    from pipeline.config import load_ticker_config

    doc = json.loads((ROOT / "data" / "META" / "FY2026Q3.json").read_text(encoding="utf-8"))
    eps = next(row for row in doc["scorecard"] if row["metric"] == "eps")
    assert eps["actual"] == 7.40 and eps["basis"] == "non_gaap" and eps["verdict"] == "beat"
    assert eps["benchmark"]["value"] == 6.54 and eps["benchmark"]["source"] == "consensus"
    assert doc["financials"]["eps_gaap"]["value"] == 3.83
    assert doc["financials"]["eps_gaap"]["benchmark"]["value"] is None
    assert doc["financials"]["eps_nongaap"]["value"] == 7.40
    assert doc["financials"]["eps_nongaap"]["display_label"] == "稀释每股收益（Non-GAAP）"

    lines = [c["text"] for c in _score_lines("META", doc, {"price": {"text": "—", "cls": "na"}}, {"release_timing": "amc"})]
    assert any("每股 EPS beat 13.1%" == c for c in lines)
    assert all("不及预期" not in c and "Non-GAAP" not in c and "次日" not in c for c in lines)
    assert any(c == "下次财报 2027-01-27 盘后" for c in lines)

    gap = next(g for g in _gap_rows(doc) if g["label"].startswith("本季 EPS"))
    assert gap["label"] == "本季 EPS（Non-GAAP）"
    assert gap["detail"] == "Non-GAAP $7.40 · 一致预期 $6.54"
    assert gap["gap"]["text"] == "+13.1%"

    rows = {r["label"]: r for r in _fin_rows(doc)}
    assert rows["稀释每股收益（GAAP）"]["value"] == "$3.83"
    assert rows["稀释每股收益（GAAP）"]["vs"]["text"] == "—"
    assert rows["稀释每股收益（Non-GAAP）"]["value"] == "$7.40"
    assert rows["稀释每股收益（Non-GAAP）"]["vs"]["text"] == "+13.1%"

    index = _index_row("META", load_ticker_config("META"), "FY2026Q3", doc)
    assert index["eps"]["verdict"] == "beat"
    assert index["eps"]["label"] == "beat 13.1%"
    assert index["revenue"]["label"] == "beat 2.0%"
    # 首页下季指引只看 EPS，META 没有管理层 EPS 指引，不用营收区间 $73–76B 填上
    assert index["guide"]["label"] == "—"

    from builder.build import _quality_checks

    names = [c["name"] for c in _quality_checks(doc, load_ticker_config("META"))]
    assert "Non-GAAP 比 GAAP EPS 高出" in names

    from builder.build import _eps_chart

    chart = _eps_chart(doc)
    assert chart is not None and [q["label"] for q in chart["quarters"]] == [
        "FQ4 2025", "FQ1 2026", "FQ2 2026", "FQ3 2026",
        "FQ4 2026", "FQ1 2027", "FQ2 2027", "FQ3 2027",
    ]
    by_q = {q["label"]: q for q in chart["quarters"]}
    assert by_q["FQ3 2026"]["actual"] == 7.40 and by_q["FQ3 2026"]["consensus"] == 6.54
    assert by_q["FQ3 2026"]["note"] == "超预期 $0.86" and by_q["FQ3 2026"]["basis"] == "Non-GAAP"
    assert by_q["FQ1 2026"]["actual"] == 7.31 and by_q["FQ1 2026"]["basis"] == "Non-GAAP"
    assert by_q["FQ4 2025"]["actual"] == 8.88 and by_q["FQ4 2025"]["basis"] == "Non-GAAP"
    q2 = by_q["FQ2 2026"]
    derived = (15848 + (2400 + 1180) * (1 - 0.16)) / 2566
    assert q2["actual"] == round(derived, 2) == 7.35
    assert q2["note"] == "超预期 $0.16" and q2["basis"] == "Non-GAAP"
    assert by_q["FQ4 2026"]["actual"] is None and by_q["FQ4 2026"]["consensus"] == 8.29
    assert by_q["FQ2 2027"]["yoy_pct"] == pytest.approx((7.867106 / 7.35 - 1) * 100)
    assert all(q["actual"] != 3.83 for q in chart["quarters"])
    assert all(q["basis"] == "Non-GAAP" for q in chart["quarters"] if q["actual"] is not None)

    from builder.build import _history

    hist_docs = {
        period: json.loads((ROOT / "data" / "META" / f"{period}.json").read_text(encoding="utf-8"))
        for period in ("FY2025Q3", "FY2025Q4", "FY2026Q1", "FY2026Q2", "FY2026Q3")
    }
    hist = {point["period"]: point for point in _history(hist_docs)}
    assert hist["FY2026Q2"]["eps_nongaap"] == 7.35
    assert hist["FY2025Q3"]["eps_nongaap"] == 7.25
    assert hist["FY2026Q1"]["eps_nongaap"] == 7.31
    assert hist["FY2026Q3"]["eps"] == 3.83
    assert hist["FY2026Q3"]["eps_nongaap"] == 7.40
    assert any("营收" in s["text"] for s in chart["sides"])
    assert all("EPS" not in s["text"] for s in chart["sides"])
    revenue = chart["revenue"]
    assert [q["label"] for q in revenue["quarters"]] == [
        "FQ4 2025", "FQ1 2026", "FQ2 2026", "FQ3 2026", "FQ4 2026", "FQ1 2027",
    ]
    rev_q3 = next(q for q in revenue["quarters"] if q["label"] == "FQ3 2026")
    assert rev_q3["actual"] == 64.6 and rev_q3["consensus"] == 63.34
    assert rev_q3["note"] == "超预期 $1.26B" and rev_q3["basis"] == "营收"
    rev_q4 = next(q for q in revenue["quarters"] if q["label"] == "FQ4 2026")
    assert rev_q4["actual"] is None and rev_q4["consensus"] == 74.1
    rev_nq = next(q for q in revenue["quarters"] if q["label"] == "FQ1 2027")
    assert rev_nq["actual"] is None and rev_nq["consensus"] == 68.24
    horizons = {item["label"]: item["value"] for item in revenue["horizons"]}
    assert horizons["近 4 季（TTM）"] == "$241.61B"
    assert horizons["未来 12 个月（NTM）"] == "$296.78B"
    assert horizons["FY26E"] == "$255.82B" and horizons["FY27E"] == "$310.44B"
    assert "FY28E" not in horizons
    assert all(q["actual"] != 3.83 for q in revenue["quarters"])
    assert any("EPS" in s["text"] for s in revenue["sides"])


def test_fin_rows_pair_margins_with_absolute_profit():
    """营业成本在毛利率上面；营业利润额仍跟在营业利润率下面，再跟净利润。"""
    from builder.build import _fin_rows

    meta = json.loads((ROOT / "data" / "META" / "FY2026Q3.json").read_text(encoding="utf-8"))
    labels = [row["label"] for row in _fin_rows(meta)]
    assert labels.index("营业收入") < labels.index("营业成本") < labels.index("毛利率（GAAP）")
    assert "毛利润" not in labels
    op = labels.index("营业利润率（GAAP）")
    assert labels.index("营业费用") < op
    assert labels[op + 1 : op + 4] == ["营业利润（GAAP）", "净利润率（GAAP）", "净利润（GAAP）"]
    assert labels.index("净利润率（GAAP）") > labels.index("营业利润率（Non-GAAP）")
    by = {row["label"]: row for row in _fin_rows(meta)}
    assert by["营业费用"]["value"] == "$38.24B"
    assert by["营业费用"]["yoy"]["text"] == "+77.9%"
    assert by["营业费用"]["qoq"]["text"] == "+24.6%"
    assert by["营业成本"]["value"] == "$13.37B"
    assert by["营业成本"]["yoy"]["text"] == "+45.2%" and by["营业成本"]["qoq"]["text"] == "+18.0%"
    assert "没有再拆开" in by["营业成本"]["summary"]
    assert by["营业利润（GAAP）"]["value"] == "$12.99B"
    assert by["营业利润（GAAP）"]["yoy"]["text"] == "-36.8%" and by["营业利润（GAAP）"]["qoq"]["text"] == "-30.8%"
    assert by["净利润率（GAAP）"]["nested"] is False and by["净利润率（GAAP）"]["value"] == "15.3%"
    assert by["净利润率（GAAP）"]["yoy"]["text"] == "+10.0pp" and by["净利润率（GAAP）"]["qoq"]["text"] == "-10.8pp"
    assert by["净利润（GAAP）"]["nested"] is False and by["净利润（GAAP）"]["value"] == "$9.86B"
    assert by["净利润（GAAP）"]["yoy"]["text"] == "+264.0%" and by["净利润（GAAP）"]["qoq"]["text"] == "-37.8%"
    assert by["净利润（GAAP）"]["vs"]["text"] == "—"

    mu = json.loads((ROOT / "data" / "MU" / "FY2026Q4.json").read_text(encoding="utf-8"))
    mu_labels = [row["label"] for row in _fin_rows(mu)]
    assert mu_labels.index("营业收入") < mu_labels.index("营业成本") < mu_labels.index("毛利率（Non-GAAP）")
    assert mu_labels.index("营业成本") < mu_labels.index("毛利率（GAAP）")
    assert "毛利润" not in mu_labels
    assert mu_labels[mu_labels.index("营业利润率（Non-GAAP）") + 1] == "营业利润（Non-GAAP）"
    assert mu_labels[mu_labels.index("营业利润率（GAAP）") + 1] == "营业利润（GAAP）"
    after_oi = mu_labels.index("营业利润（GAAP）")
    assert mu_labels[after_oi + 1 : after_oi + 3] == ["净利润率（GAAP）", "净利润（GAAP）"]
    mu_by = {row["label"]: row for row in _fin_rows(mu)}
    assert mu_by["营业成本"]["value"] == "$7.18B"
    assert mu_by["营业成本"]["yoy"]["text"] == "+14.7%" and mu_by["营业成本"]["qoq"]["text"] == "+12.2%"
    assert mu_by["毛利率（GAAP）"]["value"] == "86.8%"
    assert mu_by["毛利率（Non-GAAP）"]["value"] == "87.0%"
    assert mu_by["营业利润（Non-GAAP）"]["value"] == "$44.64B"
    assert mu_by["营业利润（GAAP）"]["value"] == "$43.75B"
    assert mu_by["净利润（GAAP）"]["value"] == "$37.70B"
    assert mu_by["净利润（GAAP）"]["yoy"]["text"] != "—" and mu_by["净利润（GAAP）"]["qoq"]["text"] != "—"


def test_cost_charts_keep_total_and_skip_empty_mix():
    """只有营业成本合计时不画空的占比图；有两项以上的拆分才加占比，收入地区不算成本地区。"""
    from builder.build import _cost_charts, _fin_view

    total_only = [
        {"period": "FY2024Q1", "cost_of_revenue": 10_000_000_000, "geo": [
            {"key": "us", "name": "美国", "value": 4_000_000_000},
            {"key": "row", "name": "美国以外", "value": 6_000_000_000},
        ]},
        {"period": "FY2024Q2", "cost_of_revenue": 12_000_000_000, "cogs_lines": [
            {"key": "cogs", "name": "营业成本", "value": 12_000_000_000},
        ]},
    ]
    assert [chart["title"] for chart in _cost_charts(total_only, "FY2024Q2")] == ["营业成本"]

    split = [
        {"period": "FY2024Q1", "cost_of_revenue": 10_000_000_000, "cogs_lines": [
            {"key": "infra", "name": "基础设施", "value": 6_000_000_000},
            {"key": "other", "name": "其他成本", "value": 4_000_000_000},
        ]},
        {"period": "FY2024Q2", "cost_of_revenue": 10_000_000_000, "cogs_lines": [
            {"key": "infra", "name": "基础设施", "value": 7_000_000_000},
            {"key": "other", "name": "其他成本", "value": 3_000_000_000},
        ], "cost_geo": [
            {"key": "us", "name": "美国", "value": 4_000_000_000},
            {"key": "row", "name": "美国以外", "value": 6_000_000_000},
        ]},
    ]
    charts = _cost_charts(split, "FY2024Q2")
    assert [chart["title"] for chart in charts] == ["营业成本", "成本构成", "按地区"]
    items = charts[1]["points"][1]["items"]
    assert [item["name"] for item in items] == ["基础设施", "其他成本"]
    assert items[1]["legend"] == "其他"
    assert abs(sum(item["share"] for item in items) - 100) < 0.2

    meta = _fin_view("META", "FY2026Q3")
    assert [chart["title"] for chart in meta["by_key"]["revenue"]["charts"]] == ["营业收入", "按业务", "按地区"]
    cost = meta["by_key"]["cost_of_revenue"]["charts"]
    assert [chart["title"] for chart in cost] == ["营业成本"]
    assert cost[0]["amount"][-1] == 13.37
    assert "毛利" not in meta["by_key"]

    mu = _fin_view("MU", "FY2026Q4")
    mu_cost = mu["by_key"]["cost_of_revenue"]["charts"]
    assert [chart["kind"] for chart in mu_cost] == ["amount_yoy"]
    assert mu_cost[0]["labels"][-1] == "FY2026Q4" and mu_cost[0]["amount"][-1] == 7.18


def test_activity_rows_and_business_split_read_press_release():
    """股本、周转天数和业务拆分只读已保存的新闻稿。META 没有存货和预收；MU 用合同负债。"""
    from builder.build import _activity_rows, _business_split

    meta = json.loads((ROOT / "data" / "META" / "FY2026Q3.json").read_text(encoding="utf-8"))
    rows = {row["label"]: row for row in _activity_rows("META", "FY2026Q3", meta, {})}
    assert rows["股份数量"]["value"] == "2,573M 股"
    assert rows["股份数量"]["yoy"]["text"] == "+1M 股"
    assert rows["股份数量"]["qoq"]["text"] == "+7M 股"
    assert "潜在稀释" in rows["股份数量"]["summary"]
    assert rows["存货周转天数"]["value"] == "未披露"
    assert rows["应收账款周转天数"]["value"] == "32.4天"
    assert rows["应收账款周转天数"]["yoy"]["text"] == "+1.3天"
    assert rows["应收账款周转天数"]["qoq"]["text"] == "-0.2天"
    assert rows["预收账款周转天数"]["value"] == "未披露"
    assert any(item["type"] == "结构" for item in rows["应收账款周转天数"]["drivers"])
    split = _business_split("META", "FY2026Q3", meta, {})
    assert [row["name"] for row in split["business"]] == ["应用家族", "Reality Labs"]
    assert [child["name"] for child in split["business"][0]["children"]] == ["广告", "其他收入"]
    assert split["business"][0]["children"][0]["revenue"] == "$63.06B"
    assert "广告占营收 97.6%" in split["note"]
    assert split["geo"] == [] and "没有按地区" in split["geo_note"]

    mu = json.loads((ROOT / "data" / "MU" / "FY2026Q4.json").read_text(encoding="utf-8"))
    mu_rows = {row["label"]: row for row in _activity_rows("MU", "FY2026Q4", mu, {})}
    assert mu_rows["股份数量"]["value"] == "1,147M 股"
    assert mu_rows["存货周转天数"]["value"] == "132.9天" and mu_rows["存货周转天数"]["yoy"]["text"] == "+10.1天"
    assert mu_rows["应收账款周转天数"]["value"] == "61.4天" and mu_rows["应收账款周转天数"]["yoy"]["text"] == "-13.9天"
    assert "合同负债周转天数" in mu_rows and mu_rows["合同负债周转天数"]["value"] == "21.9天"
    assert "预收账款周转天数" not in mu_rows
    mu_split = _business_split("MU", "FY2026Q4", mu, {})
    assert [row["name"] for row in mu_split["business"]] == ["云存储", "核心数据中心", "移动与客户端", "汽车与嵌入式"]
    assert mu_split["business"][0]["revenue"] == "$16.28B" and mu_split["business"][0]["yoy"]["text"] == "+258.4%"
    assert "各业务收入相加等于公司营收" in mu_split["note"]


def test_forward_pe_labels_follow_fiscal_year():
    """财年列随公司财年滚动，模板里不写死 FY26–FY28。"""
    from builder.build import _current_fiscal_year, _forward_pe_view, _fy_estimate_label, _months_remaining
    from pipeline.config import load_ticker_config

    assert _current_fiscal_year(date(2026, 10, 23), 12) == 2026
    assert _current_fiscal_year(date(2026, 10, 23), 8) == 2027
    assert _current_fiscal_year(date(2027, 1, 15), 12) == 2027
    assert _months_remaining(date(2026, 10, 23), 12) == 3
    assert _months_remaining(date(2026, 10, 1), 8) == 11
    assert _fy_estimate_label(2026) == "FY26E" and _fy_estimate_label(2027) == "FY27E"
    template = (ROOT / "builder" / "templates" / "period.html").read_text(encoding="utf-8")
    assert "FY26E" not in template and "FY2026E" not in template

    mu = json.loads((ROOT / "data" / "MU" / "FY2026Q4.json").read_text(encoding="utf-8"))
    shifted = _forward_pe_view("MU", mu, "FY2027Q1", load_ticker_config("MU"), active=True)
    labels = [row["label"] for row in shifted["rows"] if row["kind"] == "fy"]
    assert labels == ["FY27E", "FY28E"]
    assert "FY26E" not in labels


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
    meta = load_position("META")
    assert (mu["stage"], mu["stage_label"]) == ("watch", "观察仓") and mu["questions"] and not mu["theses"]
    assert meta["stage"] == "watch" and len(meta["questions"]) == 3 and not meta["theses"] and not meta["draft"]
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
    chips = [c["text"] for c in _verdict_chips({}, [], [], v)]
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


def test_watchlist_row_uses_saved_fields_and_eps_guidance(monkeypatch):
    """首页列只读已有配置和 scorecard。缺目标估值、涨跌、分组时留空，不拿营收指引充数。"""
    import builder.build as build
    from pipeline.config import load_position, load_ticker_config

    monkeypatch.setattr(
        build,
        "_saved_quote",
        lambda ticker: {
            "columns": {"close": 100, "eps_estimate_ntm": 5, "RSI": 47.35},
            "fetched_at": "2026-10-23T00:00:00+00:00",
        }
        if ticker == "META"
        else {},
    )
    monkeypatch.setattr(build, "_next_earnings", lambda ticker: "")

    def record(ticker, **kwargs):
        rows = {
            # 已披露 FY2026Q3、财年末 12 月 → 2026-12-31。下修 null 当成 0。
            "META": {
                "date": "2026-12-31",
                "horizon": "fiscal quarter",
                "eps_estimate_average": "8.0000",
                "eps_estimate_analyst_count": "10",
                "eps_estimate_revision_up_trailing_30_days": "4",
                "eps_estimate_revision_down_trailing_30_days": None,
            },
            # MU 财年末 8 月，FY2026Q4 的下一季是 2026-11-30。
            "MU": {
                "date": "2026-11-30",
                "horizon": "fiscal quarter",
                "eps_estimate_average": "1.2500",
                "eps_estimate_analyst_count": "6",
                "eps_estimate_revision_up_trailing_30_days": "1",
                "eps_estimate_revision_down_trailing_30_days": None,
            },
        }
        row = rows.get(ticker)
        if row is None:
            return {"estimates": [], "fetched_at": ""}
        return {"estimates": [row], "fetched_at": "2020-01-01T00:00:00+00:00"}

    monkeypatch.setattr("pipeline.sources.av_estimates.load_earnings_record", record)

    meta = {
        "meta": {"release_at_utc": "2026-10-21T20:03:00Z", "eps_basis": "non_gaap"},
        "status": {"stage": "stage2_done"},
        "scorecard": [
            {"metric": "revenue", "actual": 100, "verdict": "beat", "benchmark": {"diff_pct": 0.02}},
            {"metric": "eps", "actual": 1.2, "verdict": "beat", "benchmark": {"diff_pct": 0.131}},
            {"metric": "next_q_revenue_guidance", "actual": 70, "verdict": "beat", "benchmark": {"diff_pct": 0.05}},
        ],
    }
    row = build._index_row("META", load_ticker_config("META"), "FY2026Q3", meta, today=date(2026, 10, 23))
    assert row["status_label"] == "观察" and row["status_key"] == "watch"
    assert row["group"] == "—" and row["kind"] == ""
    assert row["target"] == "—" and row["triggered"]["text"] == "—"
    assert row["quote_price"] == "$100.00" and row["rsi"] == "47.4" and row["forward_pe"] == "20.0×"
    assert row["change"]["text"] == "—"
    assert row["revenue"]["label"] == "beat 2.0%" and row["eps"]["label"] == "beat 13.1%"
    assert row["guide"]["label"] == "—"
    assert row["reported"]["text"] == "2026-10-21"
    assert "16:03" in row["reported"]["title"]
    assert row["next_text"] == "下次财报日待定"
    assert row["quarter"]["analysts"] == "10" and row["quarter"]["eps"] == "$8.00"
    assert row["quarter"]["revisions"] == "4 / 0"
    assert row["quarter"]["stale"] is True
    assert row["quarter"]["stale_title"] == "旧 2020-01-01T00:00:00+00:00"

    mu = {
        "meta": {"release_at_utc": "2026-09-30T20:00:00Z"},
        "status": {"stage": "stage1_done"},
        "scorecard": [
            {"metric": "revenue", "actual": 1, "verdict": "unknown", "benchmark": {}},
            {"metric": "eps", "actual": 2, "verdict": "beat", "benchmark": {"diff_pct": 0.03}},
            {"metric": "next_q_eps_guidance", "actual": 3, "verdict": "beat", "benchmark": {"diff_pct": 0.111}},
            {"metric": "next_q_revenue_guidance", "actual": 9, "verdict": "beat", "benchmark": {"diff_pct": 0.5}},
        ],
    }
    mu_row = build._index_row("MU", load_ticker_config("MU"), "FY2026Q4", mu, today=date(2026, 10, 23))
    assert mu_row["revenue"]["label"] == "待核对"
    assert mu_row["eps"]["label"] == "beat 3.0%"
    assert mu_row["guide"]["label"] == "beat 11.1%"
    assert mu_row["quote_price"] == "—" and mu_row["forward_pe"] == "—" and mu_row["rsi"] == "—"
    assert mu_row["reported"]["text"] == "2026-09-30"
    assert mu_row["quarter"]["eps"] == "$1.25" and mu_row["quarter"]["analysts"] == "6"
    assert mu_row["quarter"]["revisions"] == "1 / 0"
    assert mu_row["quarter"]["stale"] is True

    labeled = build._index_row(
        "MU",
        {**load_ticker_config("MU"), "分组": "存储", "类型": "AI硬件", "目标估值": "12×"},
        "FY2026Q4",
        mu,
        today=date(2026, 10, 23),
    )
    assert labeled["group"] == "存储" and labeled["kind"] == "AI硬件" and labeled["target"] == "12×"

    sndk = load_position("SNDK")
    assert build._trigger_view(sndk, {})["text"] == "—"
    reviewed = {
        "thesis_review": {
            "stage": "hold",
            "triggers": [
                {"name": "加仓", "state": "not_triggered"},
                {"name": "卖出", "state": "not_triggered"},
            ],
        }
    }
    assert build._trigger_view(sndk, reviewed)["text"] == "否"
    reviewed["thesis_review"]["triggers"][1]["state"] = "triggered"
    assert build._trigger_view(sndk, reviewed)["text"] == "是"
    assert build._trigger_view(load_position("MU"), reviewed)["text"] == "—"

    quote = build._quote_view({"close": 100, "eps_estimate_ntm": 5, "RSI": 47.35, "day_change_pct": 0.012})
    assert quote["price"] == "$100.00" and quote["pe"] == "20.0×" and quote["rsi"] == "47.4"
    assert quote["change"]["text"] == "+1.2%"
    assert build._next_text("", "盘后") == "下次财报日待定"


def test_watchlist_homepage_html(tmp_path, monkeypatch):
    out, _pages = _render_fixture_site(monkeypatch, tmp_path)
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "<h1>观察池</h1>" in html
    assert "指标对比" in html and "Forward PE 默认升序" not in html
    assert "红色公告" not in html and "公告与新闻" not in html and "财报日历" not in html and "最新发布" not in html
    assert 'data-filter="status"' in html and ">持仓</button>" in html and ">等待</button>" in html
    assert 'aria-label="按类型筛选"' in html and ">全部</button>" in html
    # 夹具记分卡：营收 +1.0%、EPS +5.9%、下季 EPS 指引 +4.5%。不写现网的 beat 2.0% / $697.52。
    assert "beat 1.0%" in html and "beat 5.9%" in html and "beat 4.5%" in html
    assert "beat 2.0%" not in html and "$697.52" not in html and "$73–76B" not in html
    assert "$1.50" in html and "$0.10" not in html and "2 / 0" in html
    assert 'title="旧 2020-01-01T00:00:00+00:00"' in html
    assert "当前季EPS (30日%)" in html and "覆盖人数" in html and "上修 / 下修" in html and ">评价</th>" in html
    assert "Yahoo 0q" not in html and "第一个未公布" not in html
    head = html.split("</thead>", 1)[0]
    assert head.index(">是否触发</th>") < head.index(">评价</th>") < head.index(">最近财报</th>")
    assert 'class="mid name"' in head and 'class="mid">评价</th>' in head and 'class="mid">下次财报</th>' in head
    assert "下季度" not in html and "温和上修" not in html and "温和下修" not in html
    assert "ALPHAVANTAGE_API_KEY" not in html
    assert 'class="pool-name"' in html
    body = html.split("<tbody>", 1)[1]
    assert body.index(">AAA<") < body.index(">FIX<")
    kind_bar = html.split('aria-label="按类型筛选"', 1)[1].split("</div>", 1)[0]
    assert kind_bar.count("<button") == 1

