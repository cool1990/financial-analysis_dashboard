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
    assert normalize_period("2026年第一季度") == normalize_period("2026年第1季度") == "FY2026Q1"
    assert normalize_period("2026年第一季度末") == "FY2026Q1"
    assert normalize_period("2026年第二季度") == "FY2026Q2"
    assert normalize_period("2026年全年") == "FY2026"
    assert not normalize_period("上线首月").startswith("FY")
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
    assert "五档指引" not in html and "guidance.dimensions" not in html
    assert '<details class="ev" id="fin" open>' in html and 'data-series="level"' in html
    assert "财报分析" in html and "指引展望" in html and "问答交流" in html and "数据来源" in html
    assert "revChart" not in html and "marginChart" not in html and "fcfChart" not in html
    assert 'id="epsSurprise"' in html and 'data-expect="eps"' in html and 'data-expect="revenue"' in html
    assert "再点同一行就收起" in html
    assert "function collapseFin()" not in html
    assert 'data-fin-grain="quarter"' in html and 'data-fin-grain="year"' in html
    assert "var history =" not in html
    assert "static/charts.js" in html
    charts_js = (out / "static" / "charts.js").read_text(encoding="utf-8")
    assert "function collapseFin()" in charts_js
    assert "没有完整财年" in charts_js and "data-fin-bound" in charts_js
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
    from builder.build import _diluted_shares, _fmt_cap, _forward_pe_view, _header_quotes
    from pipeline.config import load_ticker_config

    snap = json.loads((ROOT / "data" / "META" / "consensus" / "tradingview_scan.json").read_text(encoding="utf-8"))
    cols = snap["quote"]["columns"]
    assert "close" in cols and "RSI" in cols and "dividends_yield" in cols
    assert "day_change_pct" in cols or "change_pct" in cols
    # scanner 字段是 price_earnings_fwd；写入时同步别名 price_earnings_forward
    assert "price_earnings_fwd" in cols or "price_earnings_forward" in cols
    doc = json.loads((ROOT / "data" / "META" / "FY2026Q3.json").read_text(encoding="utf-8"))
    header = _header_quotes("META", doc, "FY2026Q3", True)
    by = {item["label"]: item for item in header["items"]}
    assert by["股价"]["value"] == f"${cols['close']:.2f}"
    assert by["股息率"]["value"] == f"{cols['dividends_yield']:.2f}%"
    assert by["RSI"]["value"] == f"{cols['RSI']:.1f}"
    shares = _diluted_shares("META", "FY2026Q3")
    assert shares
    assert cols.get("market_cap_basic")
    assert by["市值"]["value"] == _fmt_cap(cols["market_cap_basic"])
    assert by["市值"]["sub"] == "TradingView"
    assert "未接入" not in by["市值"]["value"] and "缺股价" not in by["市值"]["sub"]
    forward = _forward_pe_view("META", doc, "FY2026Q3", load_ticker_config("META"), active=True)
    assert forward["price"] == f"${cols['close']:.2f}"
    rows = {row["label"]: row for row in forward["rows"]}
    # NTM 按剩余月份加权表内两个财年，不再用 close ÷ eps_estimate_ntm 单独占一格。
    assert rows["未来 12 个月（NTM）"]["eps_raw"] == pytest.approx(
        3 / 12 * rows["FY26E"]["eps_raw"] + 9 / 12 * rows["FY27E"]["eps_raw"]
    )
    assert "vs" not in rows["近 4 季（TTM）"]
    assert rows["近 4 季（TTM）"]["pe"].endswith("×")
    # TV 未来季够拼出 FY28 时允许出现；没有也不强制
    assert "FY26E" in rows and "FY27E" in rows
    assert abs(rows["未来 12 个月（NTM）"]["eps_raw"] - cols["eps_estimate_ntm"]) < 1.5


def test_market_cap_prefers_scan_then_stored_shares_and_does_not_invent(tmp_path, monkeypatch):
    """有 market_cap_basic 就用它。没有时先用 close × TV 总股本，再退回已落盘股本。都没有就留空。"""
    import builder.build as build

    direct = build._market_cap_view(
        {
            "market_cap_basic": 1_652_554_253_500,
            "currency": "USD",
            "close": 600,
            "total_shares_outstanding": 2_205_130_000,
        },
        price=600,
        shares=2_500_000_000,
        currency="USD",
    )
    assert direct["value"] == build._fmt_cap(1_652_554_253_500, "USD")
    assert direct["sub"] == "TradingView"
    assert direct["value"] != build._fmt_cap(600 * 2_205_130_000)
    assert direct["value"] != build._fmt_cap(600 * 2_500_000_000)

    fallback = build._market_cap_view(
        {},
        price=400,
        shares=9_000_000_000,
        currency="HKD",
        share_note="股价 × 稀释加权股本",
    )
    assert fallback["value"] == "HK$3600.0B"
    assert fallback["sub"] == "股价 × 稀释加权股本"

    empty = build._market_cap_view({"close": 10}, price=10, shares=None, currency="USD")
    assert empty["value"] == "—" and empty["sub"] == ""
    assert build._market_cap_view({"market_cap_basic": 0}, price=None, shares=None, currency="USD")["value"] == "—"

    root = tmp_path / "data"
    (root / "ZZZ" / "raw" / "FY2026Q1").mkdir(parents=True)
    (root / "ZZZ" / "raw" / "FY2026Q1" / "extracted_financials.json").write_text(
        json.dumps({"diluted_shares": {"raw": "2000", "unit": "million"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(build, "data_dir", lambda ticker=None: root if not ticker else root / str(ticker).upper())
    monkeypatch.setattr(build, "load_ticker_config", lambda ticker: {})
    shares, note = build._stored_shares("ZZZ", "FY2026Q2")
    assert shares == 2_000_000_000 and note == "股价 × 已披露股本"
    priced = build._market_cap_view({}, price=50, shares=shares, currency="USD", share_note=note)
    assert priced["value"] == "$100.0B"

    monkeypatch.setattr(build, "load_ticker_config", lambda ticker: {"shares_outstanding": 25})
    (root / "ZZZ" / "raw" / "FY2026Q1" / "extracted_financials.json").unlink()
    assert build._stored_shares("ZZZ", "FY2026Q2")[0] is None
    assert build._market_cap_view({"close": 50}, price=50, shares=None, currency="USD")["value"] == "—"

    monkeypatch.setattr(build, "load_ticker_config", lambda ticker: {"shares_outstanding": 1_000_000_000})
    configured, configured_note = build._stored_shares("ZZZ", "FY2026Q2")
    assert configured == 1_000_000_000 and configured_note == "股价 × 已披露股本"


def test_homepage_uses_tv_total_shares_before_diluted(monkeypatch):
    """首页：market_cap_basic 无效时用 close × total_shares_outstanding，不再用财报稀释股本。

    流通股不参与。历史稀释股本、XBRL 缺口回补不读这个字段。
    """
    import inspect

    import builder.build as build
    from pipeline.config import load_ticker_config

    tv_only = build._market_cap_view(
        {
            "close": 100,
            "total_shares_outstanding": 2_000_000_000,
            "float_shares_outstanding": 1_500_000_000,
            "currency": "USD",
        },
        price=100,
        shares=9_000_000_000,
        currency="USD",
        share_note="股价 × 稀释加权股本",
    )
    assert tv_only["value"] == build._fmt_cap(100 * 2_000_000_000)
    assert tv_only["value"] != build._fmt_cap(100 * 9_000_000_000)
    assert tv_only["sub"] == "股价 × TradingView 总股本"
    assert tv_only["vendor"] == "TradingView"

    invalid = build._market_cap_view(
        {"market_cap_basic": 0, "close": 50, "total_shares_outstanding": 1_000_000_000},
        price=50,
        shares=3_000_000_000,
        currency="USD",
        share_note="股价 × 稀释加权股本",
    )
    assert invalid["value"] == build._fmt_cap(50 * 1_000_000_000)
    assert invalid["sub"] == "股价 × TradingView 总股本"

    float_only = build._market_cap_view(
        {"close": 10, "total_shares_outstanding": 0, "float_shares_outstanding": 2_000_000_000},
        price=10,
        shares=4_000_000_000,
        currency="HKD",
        share_note="股价 × 已披露股本",
    )
    assert float_only["value"] == build._fmt_cap(10 * 4_000_000_000, "HKD")
    assert float_only["sub"] == "股价 × 已披露股本"

    monkeypatch.setattr(
        build,
        "_saved_quote",
        lambda ticker: {
            "columns": {
                "close": 386.0,
                "total_shares_outstanding": 9_008_371_772,
                "float_shares_outstanding": 6_151_096_413,
                "currency": "HKD",
            },
            "fetched_at": "2026-10-23T12:00:00Z",
        },
    )
    monkeypatch.setattr(build, "_next_earnings", lambda ticker: "")
    monkeypatch.setattr(build, "_stored_shares", lambda *args, **kwargs: (1_000_000_000, "股价 × 稀释加权股本"))
    monkeypatch.setattr(
        "pipeline.sources.av_estimates.load_earnings_record",
        lambda ticker, **kwargs: {"estimates": [], "fetched_at": ""},
    )
    row = build._index_row(
        "0700.HK",
        load_ticker_config("0700.HK"),
        "FY2026Q1",
        {"meta": {}},
        today=date(2026, 10, 23),
    )
    assert row["market_cap"] == build._fmt_cap(386.0 * 9_008_371_772, "HKD")
    assert row["market_cap"].startswith("HK$")
    assert row["market_cap"] != "—"
    assert "未接入" not in row["market_cap"]
    assert row["market_cap"] != build._fmt_cap(386.0 * 1_000_000_000, "HKD")

    for fn in (build._diluted_shares, build._stored_shares, build._fill_activity_gaps):
        assert "total_shares_outstanding" not in inspect.getsource(fn)


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
    cols = json.loads((ROOT / "data" / "META" / "consensus" / "tradingview_scan.json").read_text(encoding="utf-8"))["quote"]["columns"]
    assert by["股价"]["value"] == f"${cols['close']:.2f}"
    assert by["下季度 EPS 指引"]["value"] == "无" and by["年度 EPS 指引"]["value"] == "无"
    assert "32.03" not in by["下季度 EPS 指引"]["value"] and "8.29" not in by["下季度 EPS 指引"]["value"]
    assert header["market"] is True
    assert "来源 TradingView" in header["line"]

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
    # tv-scan 写入后页头行情来自 TradingView；指引格仍只认管理层 Non-GAAP。
    assert mu_header["market"] is True
    assert "TradingView" in mu_header["line"]
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
    # 下季指引的同比对照 FY2026Q1 实际营收 136.43 亿美元。
    assert mu_rows["营收"]["yoy"]["text"] == "+350.8%" and mu_rows["营收"]["vs"]["text"] == ""
    assert mu_rows["毛利率（Non-GAAP）"]["value"] == "86.25%" and mu_rows["毛利率（Non-GAAP）"]["qoq"]["text"] == "-0.8pp"
    assert mu_rows["调整后摊薄 EPS"]["value"] == "$38.15"
    assert mu_rows["调整后摊薄 EPS"]["qoq"]["text"] == "+14.2%"
    assert mu_rows["调整后摊薄 EPS"]["vs"]["text"] == "+11.1%"
    assert "净利润率" not in mu_rows and "营业利润率" not in mu_rows and "资本开支" not in mu_rows
    assert "供给变化" in {block["title"] for block in mu_view["outlook"]}
    assert "industry_outlook" not in (mu.get("guidance") or {})


def test_hk_guidance_table_fills_implied_changes_from_prior_actuals():
    """0700 的中文期间和「亿元」要能对上历史实际数。没有共识时较共识留空。"""
    from builder.build import _REPORTING_CCY, _guide_section, _load_docs

    doc = json.loads((ROOT / "data" / "0700.HK" / "FY2026Q1.json").read_text(encoding="utf-8"))
    token = _REPORTING_CCY.set("CNY")
    try:
        rows = {row["label"]: row for row in _guide_section(doc, _load_docs("0700.HK"))["rows"]}
    finally:
        _REPORTING_CCY.reset(token)

    revenue = rows["营收"]
    assert revenue["period"] == "2026财年Q1"
    assert revenue["value"] == "¥196.00B"
    assert revenue["qoq"] == {"text": "+0.8%", "cls": "up"}
    assert revenue["yoy"] == {"text": "+8.9%", "cls": "up"}
    assert revenue["vs"]["text"] == ""

    margin = rows["毛利率（Non-GAAP）"]
    assert margin["value"] == "57.00%"
    assert margin["qoq"]["text"] == "+1.3pp" and margin["yoy"]["text"] == "+1.2pp"
    assert margin["vs"]["text"] == ""

    operating = rows["营业利润率"]
    assert operating["value"] == "38.50%"
    assert operating["qoq"]["text"] == "+2.7pp"
    assert operating["yoy"] == {"text": "0.0pp", "cls": "flat"}
    assert operating["vs"]["text"] == ""

    eps = rows["调整后摊薄 EPS"]
    assert eps["value"] == "¥7.36"
    assert eps["qoq"]["text"] == "+5.7%" and eps["yoy"]["text"] == "+11.9%"
    assert eps["vs"]["text"] == ""

    capex = rows["资本开支"]
    assert capex["value"] == "¥31.20B"
    assert capex["qoq"]["text"] == "+58.9%" and capex["yoy"]["text"] == "+13.6%"
    assert capex["vs"]["text"] == ""


def test_implied_change_uses_consensus_when_present_and_stays_blank_without_it():
    from builder.build import _guide_section

    def row_for(doc, docs):
        rows = _guide_section(doc, docs)["rows"]
        assert len(rows) == 1
        return rows[0]

    current = {
        "meta": {"fiscal_period": "FY2026Q1"},
        "financials": {"revenue": {"value": 196_000_000_000}},
        "scorecard": [{"metric": "revenue", "benchmark": {"value": 190_000_000_000, "diff_pct": 0.99}}],
        "guidance": {"items": [{
            "metric_key": "revenue",
            "period": "2026年第一季度",
            "type": "numeric",
            "low": 1960,
            "mid": 1960,
            "high": 1960,
            "point_raw": "1,960亿元",
        }]},
    }
    history = {
        "FY2025Q4": {"financials": {"revenue": {"value": 194_371_000_000}}},
        "FY2025Q1": {"financials": {"revenue": {"value": 180_022_000_000}}},
    }
    filled = row_for(current, history)
    assert filled["qoq"]["text"] == "+0.8%" and filled["yoy"]["text"] == "+8.9%"
    # 本季共识用指引中值和基准现算，不用 scorecard 里实际数对共识的旧 diff_pct
    assert filled["vs"]["text"] == "+3.2%" and filled["vs"]["cls"] == "up"

    unscaled = {
        "meta": {"fiscal_period": "FY2026Q1"},
        "financials": {"revenue": {"value": 196_000_000_000}},
        "scorecard": [],
        "guidance": {"items": [{
            "metric_key": "revenue",
            "period": "2026年第一季度",
            "type": "numeric",
            "mid": 1960,
            "low": 1960,
            "high": 1960,
            "point_raw": "1960",
        }]},
    }
    blank = row_for(unscaled, history)
    assert blank["qoq"]["text"] == "" and blank["yoy"]["text"] == "" and blank["vs"]["text"] == ""

    forward = {
        "meta": {"fiscal_period": "FY2026Q1"},
        "financials": {"revenue": {"value": 196_000_000_000}},
        "scorecard": [{
            "metric": "next_q_revenue_guidance",
            "benchmark": {"value": 200_000_000_000, "diff_pct": 0.005},
        }],
        "guidance": {"items": [{
            "metric_key": "revenue",
            "period": "2026年第二季度",
            "type": "numeric",
            "mid": 2100,
            "low": 2100,
            "high": 2100,
            "point_raw": "2,100亿元",
        }]},
    }
    nxt = row_for(forward, {})
    assert nxt["period"] == "下季"
    assert nxt["qoq"]["text"] == "+7.1%"
    assert nxt["yoy"]["text"] == ""
    assert nxt["vs"]["text"] == "+0.5%"


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


def _guide_doc(period: str, items: list, **financials) -> dict:
    fin = {key: {"value": value} for key, value in financials.items()}
    return {
        "meta": {"fiscal_period": period},
        "guidance": {"items": items},
        "scorecard": [],
        "financials": fin,
    }


def test_guidance_history_uses_last_prior_guide_and_later_actual():
    """披露前最后一次数字指引对照后来的实际。同一期被改过口，取更晚的那次。本页之后的季度不提前出现。"""
    from builder.build import _guide_section

    def revenue(low, mid, high, period, source="press_release"):
        return {
            "metric_key": "revenue",
            "period": period,
            "type": "numeric",
            "low": low,
            "mid": mid,
            "high": high,
            "source": source,
        }

    docs = {
        "FY2025Q3": _guide_doc("FY2025Q3", [revenue(90, 100, 110, "FY2026Q1")], revenue=80),
        "FY2025Q4": _guide_doc(
            "FY2025Q4",
            [revenue(100, 110, 120, "FY2026Q1", source="prepared_remarks"), revenue(130, 140, 150, "FY2026Q2")],
            revenue=90,
        ),
        "FY2026Q1": _guide_doc("FY2026Q1", [revenue(160, 170, 180, "FY2026Q2")], revenue=125),
    }
    early = {row["family"]: row for row in _guide_section(docs["FY2025Q4"], docs)["rows"]}
    early_points = {point["period"]: point for point in early["revenue"]["track"]["points"]}
    assert list(early_points) == ["FY2026Q1", "FY2026Q2"]
    assert early_points["FY2026Q1"]["guide"] == "$100.00 – $120.00"
    assert early_points["FY2026Q1"]["actual"] == "尚未披露"
    assert early_points["FY2026Q1"]["beat"]["text"] == ""
    assert early["revenue"]["track"]["plot"] is None

    latest = {row["family"]: row for row in _guide_section(docs["FY2026Q1"], docs)["rows"]}
    points = {point["period"]: point for point in latest["revenue"]["track"]["points"]}
    assert list(points) == ["FY2026Q1", "FY2026Q2"]
    # FY2025Q4 改过 FY2026Q1 的口，不用 FY2025Q3 的 100。实际 125 高出 120 的上限。
    hit = points["FY2026Q1"]
    assert hit["issued"] == "2025财年Q4"
    assert hit["mid"] == "$110.00"
    assert hit["actual"] == "$125.00"
    assert hit["beat"] == {"text": "+13.6%", "cls": "up"}
    assert hit["slot"] == {"text": "超上限", "cls": "up"}
    assert points["FY2026Q2"]["actual"] == "尚未披露"
    assert latest["revenue"]["track"]["plot"]["labels"] == ["2026Q1"]
    assert latest["revenue"]["track"]["plot"]["values"] == [13.6]
    assert latest["revenue"]["track"]["plot"]["outside"] == [True]
    assert latest["revenue"]["track"]["plot"]["unit"] == "%"


def test_guidance_history_skips_incomparable_scale_and_same_release():
    from builder.build import _REPORTING_CCY, _guide_section, _load_docs

    mismatched = {
        "FY2026Q1": _guide_doc("FY2026Q1", [{
            "metric_key": "revenue",
            "period": "FY2026Q2",
            "type": "numeric",
            "mid": 1960,
            "low": 1960,
            "high": 1960,
            "point_raw": "1960",
        }]),
        "FY2026Q2": _guide_doc("FY2026Q2", [{
            "metric_key": "revenue",
            "period": "FY2026Q3",
            "type": "numeric",
            "mid": 200_000_000_000,
            "low": 200_000_000_000,
            "high": 200_000_000_000,
        }], revenue=196_000_000_000),
    }
    row = _guide_section(mismatched["FY2026Q2"], mismatched)["rows"][0]
    point = next(item for item in row["track"]["points"] if item["period"] == "FY2026Q2")
    assert point["actual"] == "$196.00B"
    assert point["beat"]["text"] == "" and point["slot"]["text"] == ""
    assert row["track"]["plot"] is None
    assert "数量级" in row["track"]["pending_note"]

    token = _REPORTING_CCY.set("CNY")
    try:
        hk = _guide_section(
            json.loads((ROOT / "data" / "0700.HK" / "FY2026Q1.json").read_text(encoding="utf-8")),
            _load_docs("0700.HK"),
        )
    finally:
        _REPORTING_CCY.reset(token)
    revenue = next(row for row in hk["rows"] if row["label"] == "营收")
    assert revenue["track"]["points"] == []
    assert "没有这项更早的数字指引" in revenue["track"]["empty"]


def test_guidance_history_meta_open_guide_and_tsm_printed_actuals():
    """META 历史季已有数字指引时可对照实际；未披露的下一期仍显示尚未兑现。TSM 用已保存的指引对照后来的营收和毛利率。"""
    from builder.build import _guide_section, _load_docs

    meta = _guide_section(
        json.loads((ROOT / "data" / "META" / "FY2026Q3.json").read_text(encoding="utf-8")),
        _load_docs("META"),
    )
    meta_rows = {row["label"]: row for row in meta["rows"]}
    revenue = meta_rows["营收"]["track"]
    periods = [point["period"] for point in revenue["points"]]
    assert periods[0] == "FY2022Q2"
    assert periods[-1] == "FY2026Q4"
    assert revenue["points"][-1]["actual"] == "尚未披露"
    assert revenue["points"][-1]["guide"] == "$73.00B – $76.00B"
    # 已兑现的最近一期应有实际与相对中值，图才有柱
    q3 = next(point for point in revenue["points"] if point["period"] == "FY2026Q3")
    assert q3["actual"] == "$64.60B"
    assert revenue["plot"] is not None
    assert revenue["plot"]["labels"][-1] == "2026Q3"
    capex = meta_rows["资本开支"]["track"]
    capex_labels = [point["label"] for point in capex["points"]]
    assert capex_labels[0] == "FY2022全年"
    assert capex_labels[-1] == "FY2026全年"
    assert capex["points"][-1]["actual"] == "尚未披露"
    # 全年指引已有更早兑现点时图可有柱；未披露的最后一期不单独要求空图

    docs = _load_docs("TSM")
    latest = {row["family"]: row for row in _guide_section(docs["FY2026Q3"], docs)["rows"]}
    series = {point["period"]: point for point in latest["revenue"]["track"]["points"]}
    periods = list(series)
    assert periods[0] == "FY2022Q2"
    assert periods[-1] == "FY2026Q4"
    for need in (
        "FY2025Q1", "FY2025Q2", "FY2025Q3", "FY2025Q4",
        "FY2026Q1", "FY2026Q2", "FY2026Q3", "FY2026Q4",
    ):
        assert need in series
    first = series["FY2025Q1"]
    assert first["issued"] == "2024财年Q4"
    assert first["guide"] == "$25.00B – $25.80B"
    assert first["mid"] == "$25.40B"
    assert first["actual"] == "$25.53B"
    assert first["beat"] == {"text": "+0.5%", "cls": "up"}
    assert first["slot"] == {"text": "区间内", "cls": "flat"}
    third = series["FY2025Q3"]
    assert third["guide"] == "$31.80B – $33.00B"
    assert third["actual"] == "$33.10B"
    assert third["beat"] == {"text": "+2.2%", "cls": "up"}
    assert third["slot"] == {"text": "超上限", "cls": "up"}
    assert series["FY2026Q4"]["actual"] == "尚未披露"
    plot = latest["revenue"]["track"]["plot"]
    i_q1 = plot["labels"].index("2025Q1")
    i_q3 = plot["labels"].index("2025Q3")
    assert plot["labels"][-1] == "2026Q3"
    assert plot["values"][i_q1] == 0.5 and plot["values"][i_q3] == 2.2
    assert plot["outside"][i_q1] is False and plot["outside"][i_q3] is True
    assert "相对中值 +0.5%" in plot["tips"][i_q1] and "区间内" in plot["tips"][i_q1]
    assert "（实际 − 指引中值）/ 指引中值" in latest["revenue"]["track"]["note"]

    margin = {point["period"]: point for point in latest["gross_margin"]["track"]["points"]}
    q1_margin = margin["FY2025Q1"]
    assert q1_margin["guide"] == "57.00% – 59.00%"
    assert q1_margin["mid"] == "58.00%"
    assert q1_margin["actual"] == "58.80%"
    assert q1_margin["beat"] == {"text": "+0.8pp", "cls": "up"}
    assert q1_margin["slot"]["text"] == "区间内"
    assert latest["gross_margin"]["track"]["plot"]["unit"] == "pp"
    assert "百分点" in latest["gross_margin"]["track"]["note"]

    prior = {row["family"]: row for row in _guide_section(docs["FY2025Q4"], docs)["rows"]}
    prior_periods = [point["period"] for point in prior["revenue"]["track"]["points"]]
    assert prior_periods[0] == "FY2022Q2"
    assert prior_periods[-1] == "FY2026Q1"
    for need in ("FY2025Q1", "FY2025Q2", "FY2025Q3", "FY2025Q4", "FY2026Q1"):
        assert need in prior_periods
    assert prior["revenue"]["track"]["points"][-1]["actual"] == "尚未披露"
    assert "FY2026Q2" not in prior_periods
    capex_points = {point["period"]: point for point in prior["capex"]["track"]["points"]}
    assert capex_points["FY2025"]["actual"] == "无实际"
    assert capex_points["FY2025"]["beat"]["text"] == ""
    assert capex_points["FY2026"]["actual"] == "尚未披露"

    template = (ROOT / "builder" / "templates" / "period.html").read_text(encoding="utf-8")
    guide = template.split('id="guide"', 1)[1].split('id="qa"', 1)[0]
    assert 'data-guide-key="{{ row.family }}"' in guide
    assert "相对中值" in guide and "相对区间" in guide
    assert "<h3>行业展望</h3>" in guide
    assert "经营指标" not in guide and "股东回报" not in guide
    script = (ROOT / "builder" / "static" / "charts.js").read_text(encoding="utf-8")
    assert "drawGuideCharts" in script and "guideBeatLabels" in script


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


def test_guide_section_drops_five_way_split():
    """指引展望只渲染业绩指引和行业展望，不再拆业绩指标 / 经营指标 / 资本开支 / 股东回报 / 其他。"""
    from builder.build import _REPORTING_CCY, _guide_section, _load_docs

    template = (ROOT / "builder" / "templates" / "period.html").read_text(encoding="utf-8")
    guide = template.split('id="guide"', 1)[1].split('id="qa"', 1)[0]
    assert "<h3>业绩指引</h3>" in guide and "<h3>行业展望</h3>" in guide
    assert "五档指引" not in guide and "guidance.dimensions" not in guide
    assert "经营指标" not in guide and "股东回报" not in guide and "市场怎么判" not in guide

    cases = (
        ("META", "FY2026Q3", "USD", ["营收", "资本开支"]),
        ("MU", "FY2026Q4", "USD", ["营收", "毛利率（Non-GAAP）", "调整后摊薄 EPS"]),
        ("TSM", None, "USD", None),
        ("0700.HK", "FY2026Q1", "CNY", ["营收", "毛利率（Non-GAAP）", "营业利润率", "调整后摊薄 EPS", "资本开支"]),
    )
    for ticker, period, ccy, labels in cases:
        docs = _load_docs(ticker)
        if period is None:
            period = max(docs)
        token = _REPORTING_CCY.set(ccy)
        try:
            view = _guide_section(docs[period], docs)
        finally:
            _REPORTING_CCY.reset(token)
        assert set(view) == {"rows", "outlook"}
        if labels is not None:
            assert [row["label"] for row in view["rows"]] == labels


def test_judgment_layer_rules():
    from builder.build import _gap_rows, _market_view, _quality_checks, _verdict_chips

    doc = json.loads((ROOT / "data" / "MU" / "FY2026Q4.json").read_text(encoding="utf-8"))
    gaps = _gap_rows(doc)
    by = {g["label"]: g for g in gaps}
    assert by["本季营收"]["has"] is False  # 缺一致预期不能当成符合预期
    assert by["下季 EPS 指引"]["gap"]["text"] == "+11.1%" and by["下季 EPS 指引"]["pos"] == pytest.approx(87.1, abs=0.1)
    market = _market_view(doc)
    # Stage3 已停用：次日反应与 T+N 分析师修正不再展示
    assert market["stance"] is None and market["revision"] is None
    assert market["price"]["text"] == "—"
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
    scan = json.loads((ROOT / "data" / "META" / "consensus" / "tradingview_scan.json").read_text(encoding="utf-8"))
    forward = scan["future_quarters"]
    assert [item["tv_label"] for item in forward] == ["2026-Q4", "2027-Q1", "2027-Q2", "2027-Q3"]
    from pipeline.compute.metrics import round_eps

    for item in forward:
        label = f"FQ{item['tv_label'][-1]} {item['tv_label'][:4]}"
        assert by_q[label]["actual"] is None
        assert by_q[label]["consensus"] == round_eps(item["eps_est"])
    assert by_q["FQ4 2026"]["consensus"] == round_eps(8.352415) == 8.35
    assert by_q["FQ2 2027"]["yoy_pct"] == pytest.approx((forward[2]["eps_est"] / 7.35 - 1) * 100)
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
    labels = [q["label"] for q in revenue["quarters"]]
    assert labels[:6] == [
        "FQ4 2025", "FQ1 2026", "FQ2 2026", "FQ3 2026", "FQ4 2026", "FQ1 2027",
    ]
    # tv-scan 写入更远未公布季后，窗口可以比手工快照更长
    rev_q3 = next(q for q in revenue["quarters"] if q["label"] == "FQ3 2026")
    assert rev_q3["actual"] == 64.6 and rev_q3["consensus"] == 63.34
    assert rev_q3["note"] == "超预期 $1.26B" and rev_q3["basis"] == "营收"
    rev_by = {q["label"]: q for q in revenue["quarters"]}
    for item in forward:
        label = f"FQ{item['tv_label'][-1]} {item['tv_label'][:4]}"
        assert rev_by[label]["actual"] is None
        assert rev_by[label]["consensus"] == round(item["rev_est"] / 1_000_000_000, 2)
    rev_q4 = rev_by["FQ4 2026"]
    assert rev_q4["consensus"] == round(74882505478.0 / 1_000_000_000, 2)
    rev_nq = rev_by["FQ1 2027"]
    assert rev_nq["consensus"] is not None
    horizons = {item["label"]: item["value"] for item in revenue["horizons"]}
    assert horizons["近 4 季（TTM）"] == "$241.61B"
    assert "未来 12 个月（NTM）" in horizons and "FY26E" in horizons and "FY27E" in horizons
    assert all(q["actual"] != 3.83 for q in revenue["quarters"])
    assert any("EPS" in s["text"] for s in revenue["sides"])


def test_amzn_expect_chart_shows_next_four_tv_quarters():
    """没有手写 eps_surprise_chart 时，盈利预期仍画出扫描里的未来四季。"""
    from builder.build import _eps_chart, _fq_label, _load_docs
    from pipeline.compute.metrics import round_eps

    docs = _load_docs("AMZN")
    latest = sorted(docs)[-1]
    chart = _eps_chart(docs[latest], docs)
    assert chart is not None and chart["revenue"]
    scan = json.loads((ROOT / "data" / "AMZN" / "consensus" / "tradingview_scan.json").read_text(encoding="utf-8"))
    forward = scan["future_quarters"]
    assert len(forward) == 4
    by_eps = {row["label"]: row for row in chart["quarters"]}
    by_rev = {row["label"]: row for row in chart["revenue"]["quarters"]}
    for item in forward:
        year, quarter = item["tv_label"].split("-Q")
        label = _fq_label(f"FY{year}Q{quarter}")
        eps = by_eps[label]
        rev = by_rev[label]
        assert eps["actual"] is None
        assert eps["consensus"] == round_eps(item["eps_est"])
        assert rev["actual"] is None
        if item.get("rev_est") is None:
            assert rev["consensus"] is None
        else:
            assert rev["consensus"] == round(float(item["rev_est"]) / 1_000_000_000, 2)


def test_expect_chart_leaves_missing_scan_fields_empty(monkeypatch):
    """未来季只取扫描已披露的 EPS / 营收。缺营收留空，不用全年 scalar 或更远的行补。"""
    from builder.build import _eps_chart

    tv = {
        "fetched_at": "2026-10-23T12:00:00Z",
        "quote": {"fetched_at": "2026-10-23T12:00:00Z"},
        "scalars": {
            "earnings_per_share_forecast_next_fy": 50.0,
            "revenue_forecast_next_fy": 800_000_000_000.0,
        },
        "future_quarters": [
            {"tv_label": "2026-Q4", "eps_est": 8.352415, "rev_est": 74_882_505_478.0},
            {"tv_label": "2027-Q1", "eps_est": 7.558111, "rev_est": None},
            {"tv_label": "2027-Q2", "eps_est": 7.811894, "rev_est": 73_784_201_435.0},
            {"tv_label": "2027-Q3", "eps_est": 8.355748, "rev_est": 78_152_843_573.0},
        ],
        "rows_used": {
            "earnings_fq_h": [
                {"FiscalPeriod": "2028-Q1", "Estimate": 99.0, "IsReported": False},
            ],
            "revenues_fq_h": [
                {"FiscalPeriod": "2028-Q1", "Estimate": 900_000_000_000.0, "IsReported": False},
            ],
        },
    }
    monkeypatch.setattr("builder.build._tradingview", lambda ticker: tv)
    doc = {
        "meta": {"ticker": "ZZZ", "fiscal_period": "FY2026Q3"},
        "financials": {
            "revenue": {"value": 64_601_000_000.0, "yoy_pct": 0.2},
            "eps_nongaap": {"value": 7.4},
        },
        "scorecard": [
            {"metric": "eps", "actual": 7.4, "basis": "non_gaap", "benchmark": {"source": "consensus", "value": 6.54}},
        ],
    }
    chart = _eps_chart(doc, {"FY2026Q3": doc})
    assert chart is not None and chart["revenue"]
    assert [row["label"] for row in chart["quarters"]] == [
        "FQ3 2026", "FQ4 2026", "FQ1 2027", "FQ2 2027", "FQ3 2027",
    ]
    by_eps = {row["label"]: row for row in chart["quarters"]}
    by_rev = {row["label"]: row for row in chart["revenue"]["quarters"]}
    assert by_eps["FQ4 2026"]["consensus"] == 8.35
    assert by_eps["FQ1 2027"]["consensus"] == 7.56
    assert by_rev["FQ1 2027"]["consensus"] is None
    assert by_rev["FQ1 2027"]["actual"] is None
    assert by_rev["FQ4 2026"]["consensus"] == round(74_882_505_478.0 / 1_000_000_000, 2)
    assert all(row["consensus"] not in {50.0, 99.0, 800.0, 900.0} for row in chart["quarters"])
    assert all(row["consensus"] not in {50.0, 99.0, 800.0, 900.0} for row in chart["revenue"]["quarters"])
    assert "FQ1 2028" not in by_eps and "FQ1 2028" not in by_rev

    gap = {
        **tv,
        "future_quarters": [
            {"tv_label": "2027-Q1", "eps_est": 7.5, "rev_est": 60_000_000_000.0},
            {"tv_label": "2027-Q2", "eps_est": 7.8, "rev_est": 70_000_000_000.0},
        ],
    }
    monkeypatch.setattr("builder.build._tradingview", lambda ticker: gap)
    gapped = _eps_chart(doc, {"FY2026Q3": doc})
    assert gapped is not None
    assert [row["label"] for row in gapped["quarters"]] == ["FQ3 2026", "FQ1 2027", "FQ2 2027"]
    assert "FQ4 2026" not in {row["label"] for row in gapped["quarters"]}
    assert {row["label"]: row["consensus"] for row in gapped["quarters"]}["FQ1 2027"] == 7.5


def test_q4_eps_yoy_uses_same_scale_scan_actual_and_leaves_unaligned_blank(monkeypatch):
    """季报没有 Q4 EPS（累计 EPS 不相减）时，扫描和已保存实际重合才补同比。

    重叠季对得上、但该季数量级离谱的扫描实际不补。去年同季仍然没有实际时，前瞻季同比留空。
    """
    from builder.build import _eps_chart

    tv = {
        "fetched_at": "2026-10-23T12:00:00Z",
        "quote": {"fetched_at": "2026-10-23T12:00:00Z"},
        "future_quarters": [
            {"tv_label": "2026-Q2", "eps_est": 2.2, "rev_est": 20_000_000_000.0},
            {"tv_label": "2026-Q3", "eps_est": 2.4, "rev_est": 21_000_000_000.0},
            {"tv_label": "2026-Q4", "eps_est": 2.5, "rev_est": 22_000_000_000.0},
            {"tv_label": "2027-Q1", "eps_est": 2.1, "rev_est": 19_000_000_000.0},
        ],
        "rows_used": {
            "earnings_fq_h_chart": [
                {"FiscalPeriod": "2024-Q4", "Actual": 1.50, "Estimate": 1.4, "IsReported": True},
                {"FiscalPeriod": "2025-Q1", "Actual": 1.2, "Estimate": 1.1, "IsReported": True},
                {"FiscalPeriod": "2025-Q2", "Actual": 80.0, "Estimate": 1.1, "IsReported": True},
                {"FiscalPeriod": "2025-Q3", "Actual": 1.4, "Estimate": 1.3, "IsReported": True},
                {"FiscalPeriod": "2025-Q4", "Actual": 1.80, "Estimate": 1.7, "IsReported": True},
                {"FiscalPeriod": "2026-Q1", "Actual": 1.6, "Estimate": 1.5, "IsReported": True},
            ],
            "revenues_fq_h": [],
        },
    }

    def quarter(period: str, eps: float | None, revenue: float) -> dict:
        fin = {"revenue": {"value": revenue, "yoy_pct": None}, "eps_gaap": {"value": eps}}
        score = []
        if eps is not None:
            score.append({"metric": "eps", "actual": eps, "basis": "gaap", "benchmark": {"source": "none"}})
        return {"meta": {"ticker": "ZZZ", "fiscal_period": period}, "financials": fin, "scorecard": score}

    docs = {
        "FY2025Q1": quarter("FY2025Q1", 1.2, 10_000_000_000),
        "FY2025Q2": quarter("FY2025Q2", None, 11_000_000_000),
        "FY2025Q3": quarter("FY2025Q3", 1.4, 12_000_000_000),
        "FY2025Q4": quarter("FY2025Q4", None, 13_000_000_000),
        "FY2026Q1": quarter("FY2026Q1", 1.6, 14_000_000_000),
    }
    monkeypatch.setattr("builder.build._tradingview", lambda ticker: tv)
    chart = _eps_chart(docs["FY2026Q1"], docs)
    assert chart is not None
    by = {row["label"]: row for row in chart["quarters"]}
    assert by["FQ4 2025"]["actual"] == 1.80
    assert by["FQ4 2025"]["basis"] == "扫描已公布"
    assert by["FQ4 2025"]["yoy_pct"] == pytest.approx((1.80 / 1.50 - 1) * 100)
    assert by["FQ4 2026"]["actual"] is None
    assert by["FQ4 2026"]["yoy_pct"] == pytest.approx((2.5 / 1.80 - 1) * 100)
    assert by["FQ2 2025"]["actual"] is None
    assert by["FQ2 2026"]["actual"] is None
    assert by["FQ2 2026"]["consensus"] == 2.2
    assert by["FQ2 2026"]["yoy_pct"] is None


def test_scan_fill_does_not_divide_across_a_split(monkeypatch):
    """扫描补上的实际如果和去年季报差出一个数量级，只补柱，不算同比。"""
    from builder.build import _eps_chart

    tv = {
        "fetched_at": "2026-10-23T12:00:00Z",
        "quote": {"fetched_at": "2026-10-23T12:00:00Z"},
        "future_quarters": [],
        "rows_used": {
            "earnings_fq_h_chart": [
                {"FiscalPeriod": "2025-Q1", "Actual": 1.2, "IsReported": True},
                {"FiscalPeriod": "2025-Q3", "Actual": 1.4, "IsReported": True},
                {"FiscalPeriod": "2025-Q4", "Actual": 1.80, "IsReported": True},
                {"FiscalPeriod": "2026-Q1", "Actual": 1.6, "IsReported": True},
            ],
            "revenues_fq_h": [],
        },
    }

    def quarter(period: str, eps: float | None) -> dict:
        fin = {"revenue": {"value": 10_000_000_000, "yoy_pct": None}, "eps_gaap": {"value": eps}}
        score = [{"metric": "eps", "actual": eps, "basis": "gaap", "benchmark": {"source": "none"}}] if eps is not None else []
        return {"meta": {"ticker": "ZZZ", "fiscal_period": period}, "financials": fin, "scorecard": score}

    docs = {
        "FY2024Q4": quarter("FY2024Q4", 20.0),
        "FY2025Q1": quarter("FY2025Q1", 1.2),
        "FY2025Q3": quarter("FY2025Q3", 1.4),
        "FY2025Q4": quarter("FY2025Q4", None),
        "FY2026Q1": quarter("FY2026Q1", 1.6),
    }
    monkeypatch.setattr("builder.build._tradingview", lambda ticker: tv)
    chart = _eps_chart(docs["FY2026Q1"], docs)
    by = {row["label"]: row for row in chart["quarters"]}
    assert by["FQ4 2025"]["actual"] == 1.80
    assert by["FQ4 2025"]["basis"] == "扫描已公布"
    assert by["FQ4 2025"]["yoy_pct"] is None


def test_forward_yoy_stays_empty_when_last_year_is_unreported():
    """去年同季还没有实际（也没有同一口径的扫描实际）时，不拿预期除预期。"""
    from builder.build import _eps_chart, _load_docs

    docs = _load_docs("0700.HK")
    latest = max(key for key in docs if key.startswith("FY"))
    chart = _eps_chart(docs[latest], docs)
    assert chart is not None and chart["revenue"]
    by_eps = {row["label"]: row for row in chart["quarters"]}
    by_rev = {row["label"]: row for row in chart["revenue"]["quarters"]}
    # FY2026Q2 已披露，FQ2 2027 用这一季实际做分母，不拿预期除预期。
    assert latest == "FY2026Q2"
    assert by_eps["FQ2 2026"]["actual"] is not None
    assert by_eps["FQ2 2027"]["consensus"] is not None and by_eps["FQ2 2027"]["actual"] is None
    assert by_eps["FQ2 2027"]["yoy_pct"] is not None
    assert by_rev["FQ2 2026"]["actual"] is not None
    assert by_rev["FQ2 2027"]["consensus"] is not None and by_rev["FQ2 2027"]["yoy_pct"] is not None

    without = {key: value for key, value in docs.items() if key != "FY2026Q2"}
    prior = _eps_chart(without["FY2026Q1"], without)
    prior_eps = {row["label"]: row for row in prior["quarters"]}
    prior_rev = {row["label"]: row for row in prior["revenue"]["quarters"]}
    assert prior_eps["FQ2 2027"]["consensus"] is not None and prior_eps["FQ2 2027"]["yoy_pct"] is None
    assert prior_rev["FQ2 2027"]["consensus"] is not None and prior_rev["FQ2 2027"]["yoy_pct"] is None


def test_amzn_q4_eps_yoy_uses_scan_actual():
    """AMZN 季报 Q4 没有 EPS，扫描里的已公布实际和相邻季同一数量级，同比要接上。"""
    from builder.build import _eps_chart, _load_docs

    docs = _load_docs("AMZN")
    latest = max(key for key in docs if key.startswith("FY"))
    chart = _eps_chart(docs[latest], docs)
    by = {row["label"]: row for row in chart["quarters"]}
    assert by["FQ4 2025"]["actual"] == pytest.approx(1.95)
    assert by["FQ4 2025"]["basis"] == "扫描已公布"
    assert by["FQ4 2025"]["yoy_pct"] == pytest.approx((1.95 / 1.86 - 1) * 100)
    assert by["FQ4 2026"]["actual"] is None
    assert by["FQ4 2026"]["yoy_pct"] == pytest.approx((2.459832 / 1.95 - 1) * 100)


def test_mismatched_scan_eps_does_not_fill_quarter_gap():
    """扫描和季报差出 ±3% 时，不把扫描实际接到季报 EPS 上，前瞻季同比留空。"""
    from builder.build import _eps_chart, _load_docs

    for ticker, hole, forward in (("MCD", "FQ4 2025", "FQ4 2026"), ("MSFT", "FQ4 2026", "FQ4 2027")):
        docs = _load_docs(ticker)
        latest = max(key for key in docs if key.startswith("FY"))
        chart = _eps_chart(docs[latest], docs)
        by = {row["label"]: row for row in chart["quarters"]}
        assert by[hole]["actual"] is None
        assert by[hole]["basis"] != "扫描已公布"
        assert by[forward]["actual"] is None and by[forward]["yoy_pct"] is None


def test_yoy_gap_tooltip_says_missing_prior_year():
    script = (ROOT / "builder" / "static" / "charts.js").read_text(encoding="utf-8")
    assert script.count("同比 缺去年同期") >= 2
    assert "同比 —" not in script


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
    # FY2016 只有 Q2 起，不满一年，不进年度柱。
    assert mu_cost[0]["annual"]["labels"][0] == "FY2017"
    assert mu_cost[0]["annual"]["labels"][-1] == "FY2026"
    assert "FY2016" not in mu_cost[0]["annual"]["labels"]


def test_annual_charts_sum_full_years_and_skip_partial():
    """年度把四个季度的金额加总；未满一年或中间缺年都不拿来算同比。占比用分项合计再算份额。"""
    from builder.build import _amount_yoy_chart, _fin_view, _line_points, _share_chart

    quarters = [
        {"period": "FY2022Q1", "revenue": None},
        {"period": "FY2022Q2", "revenue": 1_000_000_000},
        {"period": "FY2022Q3", "revenue": 1_000_000_000},
        {"period": "FY2022Q4", "revenue": 1_000_000_000},
        {"period": "FY2023Q1", "revenue": 1_000_000_000},
        {"period": "FY2023Q2", "revenue": 1_000_000_000},
        {"period": "FY2023Q3", "revenue": 1_000_000_000},
        {"period": "FY2023Q4", "revenue": 1_000_000_000},
        {"period": "FY2024Q1", "revenue": 2_000_000_000},
        {"period": "FY2024Q2", "revenue": 2_000_000_000},
        {"period": "FY2024Q3", "revenue": 2_000_000_000},
        {"period": "FY2025Q1", "revenue": 3_000_000_000},
        {"period": "FY2025Q2", "revenue": 3_000_000_000},
        {"period": "FY2025Q3", "revenue": 3_000_000_000},
        {"period": "FY2025Q4", "revenue": 3_000_000_000},
    ]
    annual = _amount_yoy_chart(quarters, "revenue", "营业收入", "十亿美元")["annual"]
    assert annual["labels"] == ["FY2023", "FY2025"]
    assert annual["amount"] == [4.0, 12.0]
    assert annual["amount_text"] == ["$4.00B", "$12.00B"]
    assert annual["yoy"] == [None, None]

    both = quarters[:8]
    both += [
        {"period": "FY2024Q1", "revenue": 2_000_000_000},
        {"period": "FY2024Q2", "revenue": 2_000_000_000},
        {"period": "FY2024Q3", "revenue": 2_000_000_000},
        {"period": "FY2024Q4", "revenue": 2_000_000_000},
    ]
    linked = _amount_yoy_chart(both, "revenue", "营业收入", "十亿美元")["annual"]
    assert linked["labels"] == ["FY2023", "FY2024"]
    assert linked["amount"] == [4.0, 8.0]
    assert linked["yoy"] == [None, 100.0]

    def lines(year, quarter, ga):
        items = [
            {"key": "rd", "name": "研发", "value": 100_000_000},
            {"key": "sales", "name": "销售", "value": 100_000_000},
        ]
        if ga:
            items.append({"key": "ga", "name": "管理", "value": 100_000_000})
        return {"period": f"FY{year}Q{quarter}", "opex_lines": items}

    mix_rows = [lines(2024, quarter, quarter == 4) for quarter in (1, 2, 3, 4)]
    mix_rows += [lines(2025, quarter, False) for quarter in (1, 2, 3)]
    mix_rows += [lines(2023, quarter, False) for quarter in (1, 2, 3)]
    mix_rows.append({"period": "FY2023Q4", "opex_lines": []})
    share = _share_chart(_line_points(mix_rows, "opex_lines", denominator="lines"), "费用构成", "占比", "FY2024Q4")
    assert share["points"][0]["items"][0]["value"] == 100_000_000
    assert share["annual"]["labels"] == ["FY2024"]
    items = share["annual"]["points"][0]["items"]
    assert [item["name"] for item in items] == ["研发", "销售", "管理"]
    assert [item["value"] for item in items] == [400_000_000, 400_000_000, 100_000_000]
    assert [item["share"] for item in items] == [44.4, 44.4, 11.1]

    meta = _fin_view("META", "FY2026Q3")
    revenue = meta["by_key"]["revenue"]["charts"]
    amount = revenue[0]["annual"]
    assert amount["labels"] == [f"FY{year}" for year in range(2016, 2026)]
    assert "FY2026" not in amount["labels"]
    assert amount["amount"][0] == 27.64 and amount["yoy"][0] is None
    assert amount["amount"][1] == 40.65 and amount["yoy"][1] is not None
    business = revenue[1]["annual"]
    assert business["labels"][0] == "FY2016" and "FY2018" in business["labels"]
    assert "FY2019" not in business["labels"] and "FY2020" not in business["labels"]
    assert "FY2021" in business["labels"] and business["labels"][-1] == "FY2025"
    assert "FY2026" not in business["labels"]
    early = business["points"][0]["items"]
    assert [item["key"] for item in early] == ["ads", "other"]
    assert abs(sum(item["share"] for item in early) - 100) < 0.2
    later = next(point for point in business["points"] if point["period"] == "FY2021")
    assert [item["key"] for item in later["items"]] == ["ads", "other", "rl"]
    geo = revenue[2]["annual"]
    assert geo["labels"][-1] == "FY2025" and "FY2026" not in geo["labels"]
    opex = meta["by_key"]["opex"]["charts"]
    assert opex[0]["annual"]["labels"][-1] == "FY2025"
    opex_share = opex[1]["annual"]["points"][-1]["items"]
    assert [item["name"] for item in opex_share] == ["研发", "销售", "管理"]
    assert abs(sum(item["share"] for item in opex_share) - 100) < 0.2
    cost = meta["by_key"]["cost_of_revenue"]["charts"]
    assert [chart["title"] for chart in cost] == ["营业成本"]
    assert cost[0]["annual"]["labels"][-1] == "FY2025" and "FY2026" not in cost[0]["annual"]["labels"]


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
    assert labels[0] == "FY27E"
    assert "FY26E" not in labels
    assert "FY28E" in labels  # TV 未来季或 AV 全年都能撑出下一财年


def test_gap_row_clips_outside_scale():
    from builder.build import _gap_rows

    doc = {"scorecard": [{"metric": "eps", "actual": 1.5, "verdict": "beat",
                          "benchmark": {"value": 1.0, "diff_pct": 0.5}}]}
    row = _gap_rows(doc)[0]
    assert row["pos"] == 100 and row["clipped"] is True


def test_market_view_discontinued():
    from builder.build import _market_view

    doc = {"price_reaction": {"next_day_pct": -0.04},
           "guidance": {"analyst_revisions": {"t_minus_1": {"eps": 2.0}, "t_plus_1": {"eps": 2.2, "eps_chg": 0.1}}}}
    view = _market_view(doc)
    assert view["stance"] is None
    assert view["revision"] is None
    assert view["price"]["text"] == "—"


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
    assert row["group"] == "软件与云" and row["kind"] == "软件与云"
    assert row["target"] == "—" and row["triggered"]["text"] == "—"
    assert row["quote_price"] == "$100.00" and row["rsi"] == "47.4" and row["forward_pe"] == "20.0×"
    meta_shares, _note = build._stored_shares("META", "FY2026Q3")
    assert meta_shares and row["market_cap"] == build._fmt_cap(100 * meta_shares)
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
    assert mu_row["market_cap"] == "—"
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
    import builder.build as build

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
    assert ">市值</th>" in html
    assert "缺股价或股本" not in html
    assert "Yahoo 0q" not in html and "第一个未公布" not in html
    head = html.split("</thead>", 1)[0]
    assert head.index(">是否触发</th>") < head.index(">评价</th>") < head.index(">最近财报</th>")
    assert 'class="mid name"' in head and 'class="mid">评价</th>' in head and 'class="mid">下次财报</th>' in head
    assert "下季度" not in html and "温和上修" not in html and "温和下修" not in html
    assert "ALPHAVANTAGE_API_KEY" not in html
    assert 'class="pool-name"' in html
    # 夹具公司名是英文，首页链接用 _display_name 的「代码 (代码)」。持仓 AAA 排在观察 FIX 前面。
    aaa = build._display_name({"name": "Alpha Hold"}, "AAA")
    fix = build._display_name({"name": "Fixture Co"}, "FIX")
    body = html.split("<tbody>", 1)[1]
    assert body.index(f">{aaa}<") < body.index(f">{fix}<")
    kind_bar = html.split('aria-label="按类型筛选"', 1)[1].split("</div>", 1)[0]
    assert kind_bar.count("<button") == 1


def test_fin_series_computed_once_and_focus_follows_period(monkeypatch):
    """金额和占比序列按股票算一次。换财季只改占比图的 focus。"""
    import builder.build as build

    build._MEMO.clear()
    calls = {"n": 0}
    original = build._amount_yoy_chart

    def wrapped(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(build, "_amount_yoy_chart", wrapped)
    later = build._fin_view("META", "FY2026Q3")
    first = calls["n"]
    earlier = build._fin_view("META", "FY2024Q1")
    assert first > 0 and calls["n"] == first
    assert later["by_key"]["revenue"]["charts"][0] is earlier["by_key"]["revenue"]["charts"][0]
    share_later = later["by_key"]["revenue"]["charts"][1]
    share_earlier = earlier["by_key"]["revenue"]["charts"][1]
    assert share_later["focus"] == "FY2026Q3"
    assert share_earlier["focus"] == "FY2024Q1"
    assert share_later["points"] is share_earlier["points"]
    assert share_later["annual"] is share_earlier["annual"]


def test_latest_snapshot_reads_newest_readable_file_once(tmp_path, monkeypatch):
    """快照按文件名从新到旧找第一份能解析的，同一轮不再扫全目录。"""
    import builder.build as build

    folder = tmp_path / "ZZ" / "snapshots"
    folder.mkdir(parents=True)
    (folder / "2020-01-01.json").write_text(
        json.dumps({"snapshot_at": "2020-01-01T00:00:00+00:00", "marker": "old"}),
        encoding="utf-8",
    )
    (folder / "2024-05-01.json").write_text(
        json.dumps({"snapshot_at": "2024-05-01T00:00:00+00:00", "marker": "day"}),
        encoding="utf-8",
    )
    (folder / "2024-05-01T153000.json").write_text(
        json.dumps({"snapshot_at": "2024-05-01T15:30:00+00:00", "marker": "later"}),
        encoding="utf-8",
    )
    (folder / "2024-06-01.json").write_text("{", encoding="utf-8")
    reads: list[str] = []
    original = Path.read_text

    def wrapped(self, *args, **kwargs):
        if self.parent == folder:
            reads.append(self.name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", wrapped)
    monkeypatch.setattr(build, "data_dir", lambda ticker: tmp_path / str(ticker).upper())
    build._MEMO.clear()
    snap = build._latest_snapshot("ZZ")
    assert snap["marker"] == "later"
    assert reads == ["2024-06-01.json", "2024-05-01T153000.json"]
    assert build._latest_snapshot("ZZ")["marker"] == "later"
    assert reads == ["2024-06-01.json", "2024-05-01T153000.json"]


def test_history_json_is_shared_and_not_inlined(tmp_path):
    """全历史写到股票目录一份。季度页和 index 都去 fetch，不再内嵌两遍。"""
    from builder.build import _history, _load_docs, build_site

    out = tmp_path / "site"
    build_site("META", out_dir=out)
    history_path = out / "stocks" / "META" / "history.json"
    script_path = out / "stocks" / "META" / "history.js"
    payload = json.loads(history_path.read_text(encoding="utf-8"))
    assert payload == _history(_load_docs("META"))
    script = script_path.read_text(encoding="utf-8")
    assert script.startswith("window.STOCK_HISTORY=")
    assert json.loads(script.split("=", 1)[1].rstrip().rstrip(";")) == payload
    for name in ("FY2026Q3.html", "index.html"):
        html = (out / "stocks" / "META" / name).read_text(encoding="utf-8")
        assert "var history =" not in html
        assert html.count("history.json") == 1
        assert "static/charts.js" in html
        assert "function collapseFin()" not in html

