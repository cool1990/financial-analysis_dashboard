"""0700.HK：港股手工导入，不碰 SEC，不把中期或 Non-IFRS 经营盈利写错。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
PRESS = ROOT / "data" / "0700.HK" / "raw" / "FY2026Q1" / "press_release.txt"


def test_q1_table_numbers_stay_cny():
    from pipeline.extract.hk_results import extract_hk_results

    extracted = extract_hk_results(PRESS.read_text(encoding="utf-8"), "FY2026Q1")
    assert extracted["currency"] == "CNY"
    assert extracted["revenue"]["raw"] == "196,458"
    assert extracted["revenue"]["unit"] == "millions"
    assert extracted["cost_of_revenue"]["raw"] == "85,193"
    assert extracted["gross_profit_gaap"]["raw"] == "111,265"
    assert extracted["operating_income_gaap"]["raw"] == "67,375"
    assert extracted["operating_income_nongaap"]["raw"] == "75,627"
    assert extracted["net_income_gaap"]["raw"] == "59,392"
    assert extracted["eps_gaap_diluted"]["raw"] == "6.302"
    assert extracted["eps_nongaap_diluted"]["raw"] == "7.364"
    assert extracted["kpis"]["vas_revenue"]["raw"] == "96,110"
    assert extracted["kpis"]["advertising_revenue"]["raw"] == "38,171"
    assert extracted["kpis"]["fintech_biz_revenue"]["raw"] == "59,885"
    assert extracted["operating_income_nongaap"]["raw"] != extracted["eps_nongaap_diluted"]["raw"]
    assert "usd" not in str(extracted).lower()


def test_half_year_is_not_q2():
    from pipeline.extract.hk_results import HkImportError, extract_hk_results

    text = (
        "腾讯公布二零二六年中期业绩\n"
        "截至二零二六年六月三十日止六个月\n"
        "简明综合收益表\n"
        "收入 400,000 360,000\n"
        "毛利 200,000 180,000\n"
        "期内盈利 80,000 70,000\n"
    )
    with pytest.raises(HkImportError, match="六个月"):
        extract_hk_results(text, "FY2026Q2")


def test_three_month_block_beats_six_month_total():
    from pipeline.extract.hk_results import extract_hk_results

    text = (
        "截至二零二六年六月三十日止三个月\n"
        "截至二零二六年六月三十日止六个月\n"
        "简明综合收益表\n"
        "未经审核截至 2026 年 6 月 30 日止三个月\n"
        "2Q2026 2Q2025 1Q2026\n"
        "收入 204,800 180,000 196,458\n"
        "收入成本 (90,000) (80,000) (85,193)\n"
        "毛利 114,800 100,000 111,265\n"
        "经营盈利 70,000 60,000 67,375\n"
        "期内盈利 60,000 50,000 59,392\n"
        "- 摊薄 6.500 5.200 6.302\n"
        "未经审核截至 2026 年 6 月 30 日止六个月\n"
        "收入 400,000 360,000\n"
        "毛利 220,000 200,000\n"
        "期内盈利 120,000 100,000\n"
    )
    extracted = extract_hk_results(text, "FY2026Q2")
    assert extracted["revenue"]["raw"] == "204,800"
    assert extracted["revenue"]["raw"] != "400,000"


def test_imported_period_json_and_homepage_stage(monkeypatch):
    from pipeline.config import load_position, load_ticker_config
    from pipeline.state import load_period_json

    doc = load_period_json("0700.HK", "FY2026Q1")
    assert doc is not None
    fin = doc["financials"]
    assert fin["revenue"]["value"] == 196_458_000_000
    assert fin["revenue"]["currency"] == "CNY"
    assert fin["eps_gaap"]["value"] == 6.302
    assert fin["eps_nongaap"]["value"] == 7.364
    assert fin["operating_income_nongaap"]["value"] == 75_627_000_000
    assert fin["kpis"]["vas_revenue"]["value"] == 96_110_000_000
    assert fin["kpis"]["advertising_revenue"]["value"] == 38_171_000_000
    assert fin["kpis"]["fintech_biz_revenue"]["value"] == 59_885_000_000
    assert doc["financials"].get("fcf") is None
    assert doc["meta"]["reporting_currency"] == "CNY"
    assert doc["meta"]["transcript_source"] == "local_import"
    assert load_position("0700.HK")["stage_label"] == "持仓"

    from builder.build import _REPORTING_CCY, _fmt_value, _index_row

    token = _REPORTING_CCY.set("CNY")
    try:
        shown = _fmt_value(fin["revenue"]["value"], "revenue")
    finally:
        _REPORTING_CCY.reset(token)
    assert shown.startswith("¥")
    assert "$" not in shown
    row = _index_row("0700.HK", load_ticker_config("0700.HK"), "FY2026Q1", doc)
    assert row["ticker"] == "0700.HK"
    assert row["status_label"] == "持仓"
    assert row["group"] == "软件与云" and row["kind"] == "软件与云"
    # NTM 估计和股价都是港币，直接相除，不因缺少人民币汇率写成待核对。
    assert row["forward_pe"] == "11.9×"
    assert row["forward_pe_fx"] == ""
    # 首页营收 / EPS 仍读记分卡。港股 Stage1 没有一致预期，verdict 是 unknown。
    assert row["revenue"]["label"] == "待核对"
    assert row["eps"]["label"] == "待核对"
    assert row["market_cap"].startswith("HK$") and row["market_cap"] != "—"
    q2_doc = load_period_json("0700.HK", "FY2026Q2")
    assert q2_doc is not None
    assert q2_doc["financials"]["revenue"]["value"] == 204_785_000_000
    assert q2_doc["financials"]["eps_gaap"]["value"] == 6.104
    assert q2_doc["meta"]["extract_mode"] == "disclosed"
    manifest = json.loads((ROOT / "data" / "0700.HK" / "raw" / "FY2026Q2" / "import_manifest.json").read_text(encoding="utf-8"))
    assert manifest["stage1"] == "stage1_done"
    assert manifest["transcript"]["filename"] == "Tencent_2026_Q2.md"
    assert manifest["transcript_source"] == "local_import"


def test_kept_transcript_survives_a_later_pdf_manifest(tmp_path):
    from pipeline.commands.import_cmd import _kept_transcript

    (tmp_path / "transcript.txt").write_text("call text", encoding="utf-8")
    (tmp_path / "transcript_meta.json").write_text(
        json.dumps({"source": "local_import", "source_url": "Tencent_2026_Q2.md", "chars": 9}),
        encoding="utf-8",
    )
    (tmp_path / "import_manifest.json").write_text(
        json.dumps({"stage1": "stage1_done", "transcript": None}),
        encoding="utf-8",
    )
    kept = _kept_transcript(tmp_path, None)
    assert kept["filename"] == "Tencent_2026_Q2.md"
    assert kept["chars"] == 9
    assert _kept_transcript(tmp_path, {"filename": "new.md", "chars": 1})["filename"] == "new.md"


def test_committed_local_import_transcripts_load():
    """Actions checkout 后 Stage2 要能读到已入库的 local_import 文字稿。"""
    import json

    from pipeline.sources.transcripts import load_manual_transcript

    raw_root = ROOT / "data" / "0700.HK" / "raw"
    periods = sorted(
        path.name
        for path in raw_root.glob("FY*")
        if path.is_dir() and (path / "transcript_meta.json").is_file()
    )
    assert periods[0] == "FY2017Q1"
    assert "FY2026Q1" in periods and "FY2026Q2" in periods
    assert (raw_root / "FY2016Q1" / "press_release.txt").is_file()
    assert not (raw_root / "FY2016Q1" / "transcript.txt").exists()
    for period in periods:
        text = load_manual_transcript(raw_root / period)
        assert text and text.strip(), period
        meta = json.loads((raw_root / period / "transcript_meta.json").read_text(encoding="utf-8"))
        assert meta["source"] == "local_import"
        assert meta["chars"] == len(text)
    q1 = load_manual_transcript(raw_root / "FY2026Q1")
    assert "2026Q1" in q1
    q2 = json.loads((raw_root / "FY2026Q2" / "import_manifest.json").read_text(encoding="utf-8"))
    assert q2["transcript"]["filename"] == "Tencent_2026_Q2.md"
    assert q2["transcript_source"] == "local_import"
    assert q2["stage1"] == "stage1_done"


def test_hk_paths_do_not_call_sec(monkeypatch):
    from pipeline.commands import backfill_cmd, init_cmd, poll_cmd, run_cmd
    from pipeline.sources.sec_edgar import SecEdgarClient

    def boom(self, *args, **kwargs):
        raise AssertionError("不应构造 SecEdgarClient")

    monkeypatch.setattr(SecEdgarClient, "__init__", boom)
    with pytest.raises(RuntimeError, match="listing=hk"):
        run_cmd.detect_new_filings("0700.HK")
    with pytest.raises(RuntimeError, match="listing=hk"):
        run_cmd.run_stage1("0700.HK", "FY2026Q1", accession="000")
    with pytest.raises(RuntimeError, match="listing=hk"):
        backfill_cmd.backfill("0700.HK")
    from pipeline.commands import replay_cmd
    from pipeline.commands.period_resolve import resolve_latest_period

    with pytest.raises(RuntimeError, match="listing=hk"):
        replay_cmd.replay_earnings_filters("0700.HK")
    assert resolve_latest_period("0700.HK") == "FY2026Q2"
    assert "0700.HK" not in init_cmd.init_missing()
    monkeypatch.setattr(poll_cmd, "list_tickers", lambda: ["0700.HK"])
    monkeypatch.setattr(poll_cmd, "near_earnings", lambda *a, **k: (_ for _ in ()).throw(AssertionError("poll 不应探测财报日")))
    out = poll_cmd.poll_once(now=datetime(2026, 5, 13, 21, 0, tzinfo=ZoneInfo("America/New_York")))
    assert out["actions"] == []


def test_pe_converts_hkd_price_into_cny_before_dividing():
    from pipeline.compute.valuation import pe_multiple

    same = pe_multiple(100, 5, price_ccy="USD", eps_ccy="USD", fx={"pair": "HKDCNY", "rate": 0.5, "date": "2026-10-23"})
    assert same["text"] == "20.0×" and same["fx"] == ""

    converted = pe_multiple(
        400,
        10,
        price_ccy="HKD",
        eps_ccy="CNY",
        fx={"pair": "HKDCNY", "rate": 0.9, "date": "2026-10-23", "source": "yfinance:HKDCNY=X"},
    )
    assert converted["pe"] == pytest.approx(36.0)
    assert converted["text"] == "36.0×"
    assert converted["fx"] == "HKDCNY 0.9000（2026-10-23）"
    assert "40.0" not in converted["text"]

    missing = pe_multiple(400, 10, price_ccy="HKD", eps_ccy="CNY", fx=None)
    assert missing["pe"] is None and missing["text"] == "待核对"
    stale = pe_multiple(400, 10, price_ccy="HKD", eps_ccy="CNY", fx={"pair": "HKDCNY", "rate": 0.9, "date": ""})
    assert stale["text"] == "待核对"


def test_hkdcny_only_uses_the_same_calendar_day(tmp_path, monkeypatch):
    from pipeline.sources.fx import close_on, fetch_hkd_cny, rate_on

    assert close_on([("2026-10-23", 0.91), ("2026-10-24", 0.5)], "2026-10-22") is None
    assert close_on([("2026-10-23", 0.91)], "2026-10-23") == pytest.approx(0.91)

    calls = {}

    class Fake:
        def __init__(self, ticker):
            calls["ticker"] = ticker

        def daily_closes(self, start, end):
            calls["window"] = (start, end)
            return [("2026-10-23", 0.9123)]

    monkeypatch.setattr("pipeline.sources.yfinance_src.YFinanceSource", Fake)
    monkeypatch.setattr("pipeline.sources.fx.fx_path", lambda: tmp_path / "HKDCNY.json")
    assert fetch_hkd_cny("2026-10-22") is None
    got = fetch_hkd_cny("2026-10-23")
    assert calls["ticker"] == "HKDCNY=X"
    assert calls["window"] == ("2026-10-23", "2026-10-24")
    assert got["rate"] == pytest.approx(0.9123)
    assert got["source"] == "yfinance:HKDCNY=X"
    assert rate_on("2026-10-23")["rate"] == pytest.approx(0.9123)
    assert rate_on("2026-10-24") is None
    assert rate_on("2026-10-23", attached={"pair": "HKDCNY", "rate": 0.5, "date": "2026-10-22"})["rate"] == pytest.approx(0.9123)
    assert rate_on("2026-10-22", attached={"pair": "HKDCNY", "rate": 0.88, "date": "2026-10-22"})["rate"] == pytest.approx(0.88)


def test_rate_for_pe_uses_same_day_then_latest_on_or_before(tmp_path, monkeypatch):
    from pipeline.sources import fx as fxmod

    monkeypatch.setattr(fxmod, "fx_path", lambda: tmp_path / "HKDCNY.json")
    fxmod.store_rate({"pair": "HKDCNY", "rate": 0.88, "date": "2026-10-22", "source": "yfinance:HKDCNY=X"})
    fxmod.store_rate({"pair": "HKDCNY", "rate": 0.5, "date": "2026-10-25", "source": "yfinance:HKDCNY=X"})

    recent = fxmod.rate_for_pe("2026-10-24", fetch=False)
    assert recent["date"] == "2026-10-22"
    assert recent["rate"] == pytest.approx(0.88)
    assert recent["matched"] == "recent"
    assert recent["as_of"] == "2026-10-24"

    fxmod.store_rate({"pair": "HKDCNY", "rate": 0.91, "date": "2026-10-24", "source": "yfinance:HKDCNY=X"})
    same = fxmod.rate_for_pe("2026-10-24", fetch=False)
    assert same["date"] == "2026-10-24"
    assert same["rate"] == pytest.approx(0.91)
    assert same["matched"] == "same_day"

    prior = fxmod.rate_for_pe(
        "2026-10-21",
        attached={"pair": "HKDCNY", "rate": 0.7, "date": "2026-10-20", "source": "manual"},
        fetch=False,
    )
    assert prior["date"] == "2026-10-20"
    assert prior["rate"] == pytest.approx(0.7)
    assert prior["matched"] == "recent"
    assert fxmod.rate_for_pe("2026-10-19", fetch=False) is None


def test_rate_for_pe_fetches_recent_and_ignores_later_closes(tmp_path, monkeypatch):
    from pipeline.sources import fx as fxmod

    monkeypatch.setattr(fxmod, "fx_path", lambda: tmp_path / "HKDCNY.json")
    calls = {"n": 0}

    class Fake:
        def __init__(self, ticker):
            calls["ticker"] = ticker

        def daily_closes(self, start, end):
            calls["n"] += 1
            calls["window"] = (start, end)
            return [("2026-10-20", 0.1), ("2026-10-22", 0.87), ("2026-10-25", 0.2)]

    monkeypatch.setattr("pipeline.sources.yfinance_src.YFinanceSource", Fake)
    got = fxmod.rate_for_pe("2026-10-24", fetch=True)
    assert calls["ticker"] == "HKDCNY=X"
    assert calls["window"] == ("2026-10-10", "2026-10-25")
    assert calls["n"] == 1
    assert got["date"] == "2026-10-22"
    assert got["rate"] == pytest.approx(0.87)
    assert got["matched"] == "recent"
    assert fxmod.rate_on("2026-10-25") is None
    assert fxmod.rate_on("2026-10-22")["rate"] == pytest.approx(0.87)
    again = fxmod.rate_for_pe("2026-10-24", fetch=True)
    assert again["date"] == "2026-10-22"
    assert calls["n"] == 1

    monkeypatch.setattr(fxmod, "fx_path", lambda: tmp_path / "empty.json")

    class Down:
        def __init__(self, ticker):
            pass

        def daily_closes(self, start, end):
            raise RuntimeError("yahoo down")

    monkeypatch.setattr("pipeline.sources.yfinance_src.YFinanceSource", Down)
    fxmod.store_rate({"pair": "HKDCNY", "rate": 0.88, "date": "2026-10-22", "source": "yfinance:HKDCNY=X"})
    kept = fxmod.rate_for_pe("2026-10-24", fetch=True)
    assert kept["date"] == "2026-10-22"
    assert kept["rate"] == pytest.approx(0.88)
    assert kept["matched"] == "recent"
    assert fxmod.rate_for_pe("2026-10-19", fetch=False) is None


def test_hk_quote_and_forward_pe_show_hkd_and_fx(monkeypatch):
    import builder.build as build

    quote = build._quote_view(
        {"close": 400, "eps_estimate_ntm": 10, "RSI": 55},
        quote_ccy="HKD",
        eps_ccy="CNY",
        fx={"pair": "HKDCNY", "rate": 0.9, "date": "2026-10-23", "source": "yfinance:HKDCNY=X"},
    )
    assert quote["price"] == "HK$400.00"
    assert quote["pe"] == "36.0×"
    assert quote["pe_fx"] == "HKDCNY 0.9000（2026-10-23）"
    bare = build._quote_view({"close": 400, "eps_estimate_ntm": 10}, quote_ccy="HKD", eps_ccy="CNY")
    assert bare["pe"] == "待核对"
    assert "40.0" not in bare["pe"]

    monkeypatch.setattr(
        build,
        "_price_quote",
        lambda *args, **kwargs: {
            "price": 400.0,
            "as_of": "2026-10-23",
            "origin": "tradingview",
            "currency": "HKD",
            "fx": {"pair": "HKDCNY", "rate": 0.9, "date": "2026-10-23", "source": "yfinance:HKDCNY=X"},
        },
    )
    monkeypatch.setattr(build, "_ttm_eps", lambda *args, **kwargs: 10.0)
    monkeypatch.setattr(build, "_annual_consensus", lambda *args, **kwargs: {})
    monkeypatch.setattr(build, "_quarter_eps_cells", lambda *args, **kwargs: {})
    monkeypatch.setattr(build, "_choose_fy_eps", lambda *args, **kwargs: None)
    monkeypatch.setattr(build, "_diluted_shares", lambda *args, **kwargs: 9_000_000_000)
    # 这一支把扫描 EPS 标成人民币，才走「港币股价 / 人民币 EPS」的汇率路径。
    saved_cols = dict(build._tv_columns("0700.HK"))
    monkeypatch.setattr(
        build,
        "_tradingview",
        lambda *args, **kwargs: {"quote": {"columns": {**saved_cols, "fundamental_currency_code": "CNY"}}},
    )
    cfg = {"listing": "hk", "reporting_currency": "CNY", "fiscal_year_end_month": 12}
    view = build._forward_pe_view("0700.HK", {"meta": {}}, "FY2026Q1", cfg, {}, True)
    assert view["price"] == "HK$400.00"
    row = view["rows"][0]
    assert row["label"] == "近 4 季（TTM）"
    assert row["eps"] == "¥10.00"
    assert row["pe"] == "36.0×"
    assert row["fx"] == "HKDCNY 0.9000（2026-10-23）"
    assert "HKDCNY 0.9000（2026-10-23" in view["note"]
    assert "40.0" not in row["pe"]

    monkeypatch.setattr(
        build,
        "_price_quote",
        lambda *args, **kwargs: {"price": 400.0, "as_of": "2026-10-23", "origin": "tradingview", "currency": "HKD"},
    )
    # 行情没带汇率，而且当天和更早都没有 HKDCNY，P/E 才待核对。
    monkeypatch.setattr("pipeline.sources.fx.rate_for_pe", lambda *args, **kwargs: None)
    missing = build._forward_pe_view("0700.HK", {"meta": {}}, "FY2026Q1", cfg, {}, True)
    assert missing["rows"][0]["pe"] == "待核对"
    assert missing["rows"][0]["fx"] == ""
    assert "没有可用的 HKDCNY 时，P/E 显示待核对" in missing["note"]

    header = build._header_quotes("0700.HK", {"meta": {}}, "FY2026Q1", True)
    shown = {item["label"]: item for item in header["items"]}
    assert shown["股价"]["value"] == "HK$400.00"
    cap_cols = build._tv_columns("0700.HK")
    assert cap_cols.get("market_cap_basic") and cap_cols.get("currency") == "HKD"
    assert shown["市值"]["value"] == build._fmt_cap(cap_cols["market_cap_basic"], "HKD")
    assert shown["市值"]["value"].startswith("HK$")
    assert "未接入" not in shown["市值"]["value"]


def test_forward_pe_uses_latest_hkdcny_when_quote_day_has_no_close(tmp_path, monkeypatch):
    import builder.build as build
    from pipeline.sources import fx as fxmod

    monkeypatch.setattr(fxmod, "fx_path", lambda: tmp_path / "HKDCNY.json")
    fxmod.store_rate({"pair": "HKDCNY", "rate": 0.85, "date": "2026-10-23", "source": "yfinance:HKDCNY=X"})
    monkeypatch.setattr(fxmod, "fetch_recent_hkd_cny", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        build,
        "_price_quote",
        lambda *args, **kwargs: {"price": 400.0, "as_of": "2026-10-24", "origin": "tradingview", "currency": "HKD"},
    )
    monkeypatch.setattr(build, "_ttm_eps", lambda *args, **kwargs: 10.0)
    monkeypatch.setattr(build, "_annual_consensus", lambda *args, **kwargs: {})
    monkeypatch.setattr(build, "_quarter_eps_cells", lambda *args, **kwargs: {})
    monkeypatch.setattr(build, "_choose_fy_eps", lambda *args, **kwargs: None)
    monkeypatch.setattr(build, "_tradingview", lambda *args, **kwargs: {})
    cfg = {"listing": "hk", "reporting_currency": "CNY", "fiscal_year_end_month": 12}
    view = build._forward_pe_view("0700.HK", {"meta": {}}, "FY2026Q1", cfg, {}, True)
    assert view["rows"][0]["pe"] == "34.0×"
    assert view["rows"][0]["fx"] == "HKDCNY 0.8500（2026-10-23）"
    assert "最近可用" in view["note"]
    assert "2026-10-23" in view["note"]
    assert "2026-10-24" in view["note"]
    assert "待核对" not in view["rows"][0]["pe"]


def test_forward_pe_keeps_tradingview_eps_in_hkd(monkeypatch):
    """扫描 EPS 与股价同为港币时，整表用港币相除，不换汇，也不采纳报表人民币实际。"""
    import builder.build as build

    tv = {
        "quote": {
            "columns": {"close": 400, "fundamental_currency_code": "HKD", "currency": "HKD"},
            "fetched_at": "2026-10-23T12:00:00Z",
        },
        "rows_used": {
            "earnings_fq_h_chart": [
                {"FiscalPeriod": "2025-Q3", "Actual": 8, "Estimate": 7, "IsReported": True},
                {"FiscalPeriod": "2025-Q4", "Actual": 8, "Estimate": 7, "IsReported": True},
                {"FiscalPeriod": "2026-Q1", "Actual": 8, "Estimate": 7, "IsReported": True},
                {"FiscalPeriod": "2026-Q2", "Actual": 8, "Estimate": 7, "IsReported": True},
                {"FiscalPeriod": "2026-Q3", "Actual": None, "Estimate": 10, "IsReported": False},
                {"FiscalPeriod": "2026-Q4", "Actual": None, "Estimate": 10, "IsReported": False},
                {"FiscalPeriod": "2027-Q1", "Actual": None, "Estimate": 12, "IsReported": False},
                {"FiscalPeriod": "2027-Q2", "Actual": None, "Estimate": 12, "IsReported": False},
                {"FiscalPeriod": "2027-Q3", "Actual": None, "Estimate": 12, "IsReported": False},
                {"FiscalPeriod": "2027-Q4", "Actual": None, "Estimate": 12, "IsReported": False},
            ]
        },
    }
    monkeypatch.setattr(build, "_tradingview", lambda *args, **kwargs: tv)
    monkeypatch.setattr(
        build,
        "_price_quote",
        lambda *args, **kwargs: {"price": 400.0, "as_of": "2026-10-23", "origin": "tradingview", "currency": "HKD"},
    )

    def boom(*args, **kwargs):
        raise AssertionError("港币 EPS 不应再查 HKDCNY")

    monkeypatch.setattr("pipeline.sources.fx.rate_for_pe", boom)
    cfg = {"listing": "hk", "reporting_currency": "CNY", "fiscal_year_end_month": 12}
    docs = {"FY2026Q1": {"eps_gaap_diluted": {"value": 6.3}}}
    view = build._forward_pe_view("0700.HK", {"meta": {}}, "FY2026Q1", cfg, docs, True)
    by_label = {row["label"]: row for row in view["rows"]}
    assert by_label["近 4 季（TTM）"]["eps"] == "HK$32.00"
    assert by_label["近 4 季（TTM）"]["pe"] == "12.5×"
    assert by_label["近 4 季（TTM）"]["fx"] == ""
    assert by_label["FY26E"]["eps"] == "HK$36.00"
    assert by_label["FY26E"]["pe"] == "11.1×"
    assert by_label["未来 12 个月（NTM）"]["eps"] == "HK$45.00"
    assert "¥" not in "".join(row["eps"] for row in view["rows"])
    assert "HKDCNY" not in view["note"]
    assert "港币" in view["note"]
    assert "人民币" in view["note"]


def test_tencent_press_statement_matches_meta_charts():
    import json

    from builder.build import _REPORTING_CCY, _fin_rows, _fin_view, _forward_pe_view, _load_docs
    from pipeline.config import load_ticker_config
    from pipeline.sources.tencent_statement import (
        build_tencent_histories_from_periods,
        parse_tencent_statement,
    )

    text = PRESS.read_text(encoding="utf-8")
    press_statement, segment = parse_tencent_statement(text)
    by_press = {row["period"]: row for row in press_statement["quarters"]}
    current = by_press["FY2026Q1"]
    assert current["revenue"] == 196_458_000_000
    assert current["gross_profit"] == 111_265_000_000
    assert current["cost_of_revenue"] == 85_193_000_000
    assert current["opex"] == 45_143_000_000
    assert current["geo"] == []
    # 单份新闻稿对照列没有 2025Q2 营收，解析器留空。后面的分部行仍来自这份稿。
    assert by_press["FY2025Q2"]["revenue"] is None

    folder = ROOT / "data" / "0700.HK"
    built_statement, _built_segment = build_tencent_histories_from_periods(folder)
    built_periods = [row["period"] for row in built_statement["quarters"]]
    assert built_periods[0] == "FY2016Q1"
    assert built_periods[-1] == "FY2026Q2"
    assert len(built_periods) == 42
    by_built = {row["period"]: row for row in built_statement["quarters"]}
    assert by_built["FY2017Q1"]["revenue"] == 49_552_000_000
    assert by_built["FY2025Q2"]["revenue"] == 184_504_000_000
    assert by_built["FY2026Q1"]["revenue"] == 196_458_000_000
    assert by_built["FY2026Q1"]["cost_of_revenue"] == 85_193_000_000
    assert by_built["FY2026Q1"]["opex"] == 45_143_000_000
    assert all(row.get("geo") == [] for row in built_statement["quarters"])
    assert all(row.get("revenue") is not None for row in built_statement["quarters"])

    lines = {row["period"]: row["lines"] for row in segment["quarters"]}
    assert [line["name"] for line in lines["FY2026Q1"]] == ["增值服务", "营销服务", "金融科技及企业服务", "其他"]
    assert abs(sum(line["share_pct"] for line in lines["FY2026Q1"]) - 100) < 0.2
    assert lines["FY2025Q2"] == []
    assert "FY2017Q1" not in lines

    saved_statement = json.loads((ROOT / "data" / "0700.HK" / "statement_history.json").read_text(encoding="utf-8"))
    saved_segment = json.loads((ROOT / "data" / "0700.HK" / "segment_history.json").read_text(encoding="utf-8"))
    saved_by = {row["period"]: row for row in saved_statement["quarters"]}
    saved_lines = {row["period"]: row["lines"] for row in saved_segment["quarters"]}
    # history --from-periods 用季度 JSON 补上简明表没有的季度，2026Q1 仍用表内原数。
    assert saved_by["FY2026Q1"]["revenue"] == current["revenue"]
    assert saved_by["FY2026Q1"]["cost_of_revenue"] == current["cost_of_revenue"]
    assert [line["name"] for line in saved_lines["FY2026Q1"]] == ["增值服务", "营销服务", "金融科技及企业服务", "其他"]
    assert saved_by["FY2025Q2"]["revenue"] == 184_504_000_000
    assert saved_lines["FY2025Q2"]  # 全历史回填后 Q2 不再空
    assert [line["name"] for line in saved_lines["FY2017Q1"]] == ["增值服务", "营销服务"]
    assert not any(line["key"] == "fintech" for line in saved_lines["FY2017Q1"])
    assert any(line["key"] == "fintech" for line in saved_lines["FY2019Q1"])
    assert saved_by["FY2016Q1"]["revenue"] == 31_995_000_000
    assert "other" not in {line["key"] for line in saved_lines["FY2016Q1"]}

    view = _fin_view("0700.HK", "FY2026Q1")
    charts = view["by_key"]["revenue"]["charts"]
    assert [chart["title"] for chart in charts] == ["营业收入", "按业务", "按地区"]
    assert charts[0]["subtitle"] == "十亿人民币"
    assert charts[0]["labels"][0] == "FY2016Q1"
    assert charts[0]["labels"][-1] == "FY2026Q2"
    assert charts[0]["amount"][charts[0]["labels"].index("FY2016Q1")] == 32.0
    assert charts[0]["amount"][charts[0]["labels"].index("FY2026Q1")] == 196.46
    assert charts[0]["amount"][charts[0]["labels"].index("FY2017Q1")] == 49.55
    business = {point["period"]: point for point in charts[1]["points"]}
    assert business["FY2026Q1"]["gap"] is False
    assert business["FY2025Q2"]["gap"] is False
    assert business["FY2017Q1"]["gap"] is False
    assert all(point["gap"] for point in charts[2]["points"])
    assert [chart["title"] for chart in view["by_key"]["gross_margin_gaap"]["charts"]] == ["毛利"]
    assert [chart["title"] for chart in view["by_key"]["opex"]["charts"]] == ["营业费用", "费用构成"]
    token = _REPORTING_CCY.set("CNY")
    try:
        rows = {row["label"]: row for row in _fin_rows(json.loads((ROOT / "data" / "0700.HK" / "FY2026Q1.json").read_text(encoding="utf-8")), _load_docs("0700.HK"))}
    finally:
        _REPORTING_CCY.reset(token)
    assert rows["营业费用"]["value"] == "¥45.14B"
    assert "销售及市场推广" in rows["营业费用"]["summary"]

    bare = _forward_pe_view("0700.HK", {"meta": {"ticker": "0700.HK", "fiscal_period": "FY2026Q1"}}, "FY2026Q1", load_ticker_config("0700.HK"), _load_docs("0700.HK"), True)
    cols = json.loads((ROOT / "data" / "0700.HK" / "consensus" / "tradingview_scan.json").read_text(encoding="utf-8"))["quote"]["columns"]
    assert bare is not None and bare["price"] == f"HK${cols['close']:.2f}"
    assert bare["rows"]
    assert all(row["eps"].startswith("HK$") for row in bare["rows"])
    assert all(row["fx"] == "" for row in bare["rows"])
    assert all(row["pe"].endswith("×") and "待核对" not in row["pe"] for row in bare["rows"])
    assert "HKDCNY" not in bare["note"]
    assert "港币" in bare["note"]
    assert "人民币" in bare["note"]
    assert "待核对" not in bare["note"]
    by_label = {row["label"]: row for row in bare["rows"]}
    reported = [
        row["Actual"]
        for row in json.loads((ROOT / "data" / "0700.HK" / "consensus" / "tradingview_scan.json").read_text(encoding="utf-8"))["rows_used"]["earnings_fq_h_chart"]
        if row.get("IsReported") and row.get("Actual") is not None
    ]
    ttm = sum(reported[-4:])
    assert by_label["近 4 季（TTM）"]["eps"] == f"HK${ttm:.2f}"
    assert by_label["近 4 季（TTM）"]["eps"] != "¥25.36"
    assert by_label["近 4 季（TTM）"]["pe"] == f"{cols['close'] / ttm:.1f}×"
    fy26 = float(by_label["FY26E"]["eps"].removeprefix("HK$"))
    assert by_label["FY26E"]["eps"] != "¥30.95"
    assert 32 < fy26 < 35


def test_pool_groups_cover_the_four_industries():
    from builder.build import _KIND_ORDER, _kind_filters
    from pipeline.config import list_tickers, load_ticker_config

    expected = {
        "NVDA": "半导体",
        "TSM": "半导体",
        "AVGO": "半导体",
        "MU": "半导体",
        "INTC": "半导体",
        "SNDK": "半导体",
        "MSFT": "软件与云",
        "ORCL": "软件与云",
        "META": "软件与云",
        "GOOGL": "软件与云",
        "0700.HK": "软件与云",
        "AMZN": "软件与云",
        "JPM": "金融",
        "BAC": "金融",
        "GS": "金融",
        "SPGI": "金融",
        "HOOD": "金融",
        "CRCL": "金融",
        "MCD": "消费",
        "NKE": "消费",
    }
    assert set(list_tickers()) == set(expected)
    for ticker, group in expected.items():
        cfg = load_ticker_config(ticker)
        assert cfg.get("分组") == group
        assert not cfg.get("类型")
    assert _KIND_ORDER == ("半导体", "软件与云", "金融", "消费")
    shuffled = [{"kind": name} for name in ("消费", "金融", "软件与云", "半导体")]
    assert _kind_filters(shuffled) == list(_KIND_ORDER)


def test_tv_hkd_amounts_use_the_live_rate_only():
    import builder.build as build

    ctx = {"identity": False, "spot": 0.85, "by_period": {"FY2026Q1": 0.88}}
    assert build._scale_tv_money(10, "FY2026Q1", ctx) == pytest.approx(8.5)
    assert build._scale_tv_money(10, "FY2026Q3", ctx) == pytest.approx(8.5)
    assert build._scale_tv_money(10, "FY2026Q3", {"identity": False, "spot": None, "by_period": {"FY2026Q3": 0.9}}) is None
    assert build._scale_tv_money(10, "FY2026Q1", {"identity": True, "spot": 1.0, "by_period": {}}) == 10
    assert build._scale_tv_money(None, "FY2026Q1", ctx) is None
    assert build._tv_forward_rows(
        {"future_quarters": [{"tv_label": "2026-Q3", "eps_est": 10, "rev_est": 100}]},
        scale=lambda period, value: build._scale_tv_money(value, period, ctx),
    ) == [{"period": "FY2026Q3", "eps": pytest.approx(8.5), "revenue": pytest.approx(85.0)}]
    assert build._tv_forward_rows(
        {"future_quarters": [{"tv_label": "2026-Q3", "eps_est": 10, "rev_est": 100}]},
        scale=lambda period, value: None,
    ) == []


def test_tencent_consensus_uses_live_hkdcny_not_raw_hkd(monkeypatch):
    """港币预期按 HKDCNY 换成人民币。没有汇率时丢掉，不和人民币实际比。"""
    import builder.build as build

    tv = {
        "quote": {"columns": {"fundamental_currency_code": "HKD", "close": 400}},
        "rows_used": {
            "revenues_fq_h": [
                {"FiscalPeriod": "2026-Q1", "Actual": 200.0, "Estimate": 210.0, "IsReported": True},
                {"FiscalPeriod": "2026-Q3", "Actual": None, "Estimate": 220.0, "IsReported": False},
            ]
        },
    }
    docs = {"FY2026Q1": {"financials": {"revenue": {"value": 176.0}}}}
    fx = {"pair": "HKDCNY", "rate": 0.8, "date": "2026-10-23", "source": "yfinance:HKDCNY=X", "matched": "same_day"}
    monkeypatch.setattr(build, "_load_docs", lambda ticker: docs)
    ctx = build._tv_fx_context("0700.HK", {"listing": "hk", "reporting_currency": "CNY"}, tv=tv, fx=fx)
    assert ctx["identity"] is False
    assert ctx["by_period"] == {}
    assert ctx["spot"] == pytest.approx(0.8)
    assert build._scale_tv_money(210, "FY2026Q1", ctx) == pytest.approx(168.0)
    assert build._scale_tv_money(220, "FY2026Q3", ctx) == pytest.approx(176.0)

    usd = build._tv_fx_context("META", {"reporting_currency": "USD"}, tv={"quote": {"columns": {"fundamental_currency_code": "USD"}}})
    assert usd["identity"] is True

    monkeypatch.setattr(build, "_quote_fx", lambda *args, **kwargs: None)
    dropped = build._tv_fx_context(
        "0700.HK",
        {"listing": "hk", "reporting_currency": "CNY"},
        tv={"quote": {"columns": {"fundamental_currency_code": "HKD"}}, "rows_used": {}},
        fx=None,
    )
    assert dropped["spot"] is None and dropped["by_period"] == {}
    assert build._scale_tv_money(10, "FY2026Q3", dropped) is None


def test_tencent_expect_chart_converts_tv_hkd_with_live_hkdcny():
    import json

    from builder.build import _load_docs, _reported_expect_chart

    doc = json.loads((ROOT / "data" / "0700.HK" / "FY2026Q1.json").read_text(encoding="utf-8"))
    scan = json.loads((ROOT / "data" / "0700.HK" / "consensus" / "tradingview_scan.json").read_text(encoding="utf-8"))
    rate = json.loads((ROOT / "data" / "fx" / "HKDCNY.json").read_text(encoding="utf-8"))["rates"]["2026-10-23"]["rate"]
    chart = _reported_expect_chart(doc, _load_docs("0700.HK"))
    assert chart is not None
    revenue = {row["label"]: row for row in chart["revenue"]["quarters"]}
    q1_rev = revenue["FQ1 2026"]
    raw_rev = next(row["Estimate"] for row in scan["rows_used"]["revenues_fq_h"] if row["FiscalPeriod"] == "2026-Q1")
    assert q1_rev["actual"] == pytest.approx(196.46, abs=0.01)
    assert q1_rev["consensus"] == round(raw_rev * rate / 1_000_000_000, 2)
    assert q1_rev["consensus"] != round(raw_rev / 1_000_000_000, 2)
    assert q1_rev["note_cls"] == "beat"
    eps = {row["label"]: row for row in chart["quarters"]}
    raw_eps = next(row["Estimate"] for row in scan["rows_used"]["earnings_fq_h_chart"] if row["FiscalPeriod"] == "2026-Q1")
    raw_fq3 = next(row["Estimate"] for row in scan["rows_used"]["earnings_fq_h_chart"] if row["FiscalPeriod"] == "2026-Q3")
    assert eps["FQ1 2026"]["actual"] == pytest.approx(6.3, abs=0.05)
    assert eps["FQ1 2026"]["consensus"] == round(raw_eps * rate, 2)
    assert eps["FQ1 2026"]["note_cls"] == "miss"
    assert eps["FQ3 2026"]["consensus"] == round(raw_fq3 * rate, 2)
    assert "HKDCNY" in chart["note"]
    assert "人民币" in chart["note"]
    assert "隐含汇率" not in chart["note"]
    assert "待核对" not in chart["note"]


def test_expect_chart_shows_pending_when_hkdcny_is_missing(monkeypatch):
    import json

    import builder.build as build

    monkeypatch.setattr(build, "_quote_fx", lambda *args, **kwargs: None)
    doc = json.loads((ROOT / "data" / "0700.HK" / "FY2026Q1.json").read_text(encoding="utf-8"))
    chart = build._reported_expect_chart(doc, build._load_docs("0700.HK"))
    eps = {row["label"]: row for row in chart["quarters"]}
    revenue = {row["label"]: row for row in chart["revenue"]["quarters"]}
    assert eps["FQ1 2026"]["actual"] == pytest.approx(6.3, abs=0.05)
    assert eps["FQ1 2026"]["consensus"] is None
    assert eps["FQ1 2026"]["note"] == "待核对"
    assert eps["FQ1 2026"]["note_cls"] == "unknown"
    assert eps["FQ3 2026"]["consensus"] is None
    assert eps["FQ3 2026"]["note"] == "待核对"
    assert revenue["FQ1 2026"]["actual"] == pytest.approx(196.46, abs=0.01)
    assert revenue["FQ1 2026"]["consensus"] is None
    assert revenue["FQ1 2026"]["note"] == "待核对"
    assert "缺少 HKDCNY" in chart["note"]
    assert "待核对" in chart["note"]
    assert "缺少 HKDCNY" in chart["revenue"]["note"]
