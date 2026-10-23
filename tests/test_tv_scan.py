"""TradingView scanner：符号映射、JSON 形状、写入合并。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline.sources import tradingview_scan as tv


ROOT = Path(__file__).resolve().parents[1]


def test_hkdcny_quote_prefers_direct_and_inverts_cnyhkd():
    seen = {}

    def post(url, body):
        seen["url"] = url
        seen["columns"] = body["columns"]
        seen["tickers"] = body["symbols"]["tickers"]
        return {
            "data": [
                {"s": "FX_IDC:HKDCNY", "d": ["HKDCNY", 0.8505, 1792706400, "CNY"]},
                {"s": "FX_IDC:CNYHKD", "d": ["CNYHKD", 1.1748, 1792706400, "HKD"]},
            ]
        }

    got = tv.fetch_hkd_cny_quote(post=post)
    assert "scanner.tradingview.com/forex/scan" in seen["url"]
    assert seen["columns"] == tv.FX_COLUMNS
    assert seen["tickers"] == ["FX_IDC:HKDCNY", "FX_IDC:CNYHKD"]
    assert got["pair"] == "HKDCNY"
    assert got["rate"] == pytest.approx(0.8505)
    assert got["date"] == "2026-10-23"
    assert got["source"] == "tradingview:FX_IDC:HKDCNY"
    assert got["symbol"] == "FX_IDC:HKDCNY"

    def only_inverse(url, body):
        return {"data": [{"s": "FX_IDC:CNYHKD", "d": ["CNYHKD", 1.25, 1792706400, "HKD"]}]}

    inverted = tv.fetch_hkd_cny_quote(post=only_inverse)
    assert inverted["rate"] == pytest.approx(0.8)
    assert inverted["date"] == "2026-10-23"
    assert inverted["source"] == "tradingview:FX_IDC:CNYHKD"
    assert inverted["symbol"] == "FX_IDC:CNYHKD"

    def zero_direct(url, body):
        return {
            "data": [
                {"s": "FX_IDC:HKDCNY", "d": ["HKDCNY", 0, 1792706400, "CNY"]},
                {"s": "FX_IDC:CNYHKD", "d": ["CNYHKD", 1.25, 1792706400, "HKD"]},
            ]
        }

    fallback = tv.fetch_hkd_cny_quote(post=zero_direct)
    assert fallback["rate"] == pytest.approx(0.8)
    assert fallback["source"] == "tradingview:FX_IDC:CNYHKD"
    assert tv.fetch_hkd_cny_quote(post=lambda url, body: {"data": []}) is None

    def down(url, body):
        raise RuntimeError("down")

    assert tv.fetch_hkd_cny_quote(post=down) is None


def test_hk_maps_to_hkex_without_leading_zeros():
    market, cands = tv.tv_symbol_candidates("0700.HK", {"listing": "hk", "exchange": "HK"})
    assert market == "hongkong"
    assert cands == ["HKEX:700"]


def test_us_candidates_prefer_nasdaq_then_nyse_amex():
    market, cands = tv.tv_symbol_candidates("META", {})
    assert market == "america"
    assert cands == ["NASDAQ:META", "NYSE:META", "AMEX:META"]


def test_adr_prefers_nyse():
    market, cands = tv.tv_symbol_candidates("TSM", {"listing": "adr"})
    assert market == "america"
    assert cands[0] == "NYSE:TSM"
    assert "NASDAQ:TSM" in cands


def test_quote_columns_scale_change_to_fraction():
    cols = tv.quote_columns_from_raw(
        {
            "close": 100.5,
            "change": 1.25,  # TV 百分比点数
            "RSI": 55.1,
            "eps_estimate_ntm": 4.0,
            "currency": "USD",
        }
    )
    assert cols["close"] == 100.5
    assert cols["RSI"] == 55.1
    assert abs(cols["day_change_pct"] - 0.0125) < 1e-12
    assert cols["change_pct"] == cols["day_change_pct"]
    assert cols["eps_estimate_ntm"] == 4.0
    assert cols["currency"] == "USD"


def _row(**fields):
    values = [None] * len(tv.SCAN_COLUMNS)
    for key, value in fields.items():
        values[tv.SCAN_COLUMNS.index(key)] = value
    return values


def test_refresh_writes_quote_shape_and_merges(tmp_path, monkeypatch):
    monkeypatch.setattr(tv, "data_dir", lambda ticker: tmp_path / ticker)
    monkeypatch.setattr(tv, "list_tickers", lambda: ["META", "0700.HK"])
    monkeypatch.setattr(
        tv,
        "load_ticker_config",
        lambda t: {"listing": "hk"} if t.endswith(".HK") else {},
    )

    def fake_post(url, body):
        assert body["columns"] == tv.SCAN_COLUMNS
        tickers = body["symbols"]["tickers"]
        if "hongkong" in url:
            assert "HKEX:700" in tickers
            return {
                "data": [
                    {
                        "s": "HKEX:700",
                        "d": _row(
                            name="0700",
                            close=600.0,
                            change=-0.5,
                            RSI=42.0,
                            volume=1e6,
                            currency="HKD",
                            fundamental_currency_code="CNY",
                            eps_estimate_ntm=20.0,
                            earnings_fq_h=[
                                {"Actual": None, "Estimate": 8.0, "FiscalPeriod": "2026-Q2", "IsReported": False, "Type": 10},
                                {"Actual": None, "Estimate": 8.5, "FiscalPeriod": "2026-Q3", "IsReported": False, "Type": 10},
                                {"Actual": None, "Estimate": 9.0, "FiscalPeriod": "2026-Q4", "IsReported": False, "Type": 10},
                                {"Actual": None, "Estimate": 9.5, "FiscalPeriod": "2027-Q1", "IsReported": False, "Type": 10},
                            ],
                            revenues_fq_h=[
                                {"Actual": None, "Estimate": 1e11, "FiscalPeriod": "2026-Q2", "IsReported": False, "Type": 10},
                                {"Actual": None, "Estimate": 1.1e11, "FiscalPeriod": "2026-Q3", "IsReported": False, "Type": 10},
                                {"Actual": None, "Estimate": 1.2e11, "FiscalPeriod": "2026-Q4", "IsReported": False, "Type": 10},
                                {"Actual": None, "Estimate": 1.3e11, "FiscalPeriod": "2027-Q1", "IsReported": False, "Type": 10},
                            ],
                        ),
                    }
                ]
            }
        assert "NASDAQ:META" in tickers
        return {
            "data": [
                {
                    "s": "NASDAQ:META",
                    "d": _row(
                        name="META",
                        close=700.0,
                        change=2.0,
                        RSI=47.3,
                        volume=2e6,
                        currency="USD",
                        fundamental_currency_code="USD",
                        eps_estimate_ntm=32.0,
                        earnings_per_share_forecast_next_fy=30.0,
                        price_earnings_fwd=21.5,
                        price_earnings_ttm=25.0,
                        dividends_yield=0.3,
                        dividends_yield_current=0.3,
                        market_cap_basic=1e12,
                        earnings_fq_h=[
                            {"Actual": 3.8, "Estimate": 6.5, "FiscalPeriod": "2026-Q3", "IsReported": True, "Type": 22},
                            {"Actual": None, "Estimate": 8.3, "FiscalPeriod": "2026-Q4", "IsReported": False, "Type": 10},
                            {"Actual": None, "Estimate": 7.5, "FiscalPeriod": "2027-Q1", "IsReported": False, "Type": 10},
                            {"Actual": None, "Estimate": 7.8, "FiscalPeriod": "2027-Q2", "IsReported": False, "Type": 10},
                            {"Actual": None, "Estimate": 8.1, "FiscalPeriod": "2027-Q3", "IsReported": False, "Type": 10},
                        ],
                        revenues_fq_h=[
                            {"Actual": 6.4e10, "Estimate": 6.3e10, "FiscalPeriod": "2026-Q3", "IsReported": True, "Type": 22},
                            {"Actual": None, "Estimate": 7.4e10, "FiscalPeriod": "2026-Q4", "IsReported": False, "Type": 10},
                            {"Actual": None, "Estimate": 6.8e10, "FiscalPeriod": "2027-Q1", "IsReported": False, "Type": 10},
                            {"Actual": None, "Estimate": 7.2e10, "FiscalPeriod": "2027-Q2", "IsReported": False, "Type": 10},
                            {"Actual": None, "Estimate": 7.6e10, "FiscalPeriod": "2027-Q3", "IsReported": False, "Type": 10},
                        ],
                    ),
                }
            ]
        }

    # 预置 META 旧文件，确认 scalars 不被冲掉
    meta_path = tmp_path / "META" / "consensus" / "tradingview_scan.json"
    meta_path.parent.mkdir(parents=True)
    meta_path.write_text(
        json.dumps({"scalars": {"keep": True}, "quote": {"columns": {"close": 1, "dividend_yield_ttm": None}}}, ensure_ascii=False),
        encoding="utf-8",
    )

    out = tv.refresh_quotes(
        ["META", "0700.HK"],
        post=fake_post,
        sleep=lambda _: None,
        now=datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc),
    )
    assert out["ok"] is True and out["written"] == 2
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    # scalars 由本次 scanner 覆盖；旧手工键不保留
    assert meta["scalars"]["eps_estimate_ntm"] == 32.0
    assert "keep" not in meta["scalars"]
    assert meta["quote"]["ticker"] == "NASDAQ:META"
    assert meta["quote"]["columns"]["close"] == 700.0
    assert abs(meta["quote"]["columns"]["day_change_pct"] - 0.02) < 1e-12
    assert meta["quote"]["columns"]["RSI"] == 47.3
    assert meta["quote"]["columns"]["eps_estimate_ntm"] == 32.0
    assert meta["quote"]["columns"]["price_earnings_forward"] == meta["quote"]["columns"]["price_earnings_fwd"] == 21.5
    assert "dividend_yield_ttm" in meta["quote"]["columns"]  # 旧列合并保留
    assert meta["quote"]["endpoint"].endswith("/america/scan")
    assert meta["fetched_at"] == "2026-10-23T12:00:00Z"
    assert meta["tv_scan"]["fetched_at"] == "2026-10-23T12:00:00Z"
    assert len(meta["future_quarters"]) == 4
    assert meta["future_quarters"][0]["eps_est"] == 8.3
    assert meta["rows_used"]["earnings_fq_h"][0]["FiscalPeriod"] == "2026-Q3"
    assert meta["rows_used"]["revenues_fq_h"][1]["Estimate"] == 7.4e10
    assert meta["scalars"]["eps_estimate_ntm"] == 32.0
    assert "alpha_vantage_annual_eps" in meta["sources"]

    hk = json.loads((tmp_path / "0700.HK" / "consensus" / "tradingview_scan.json").read_text(encoding="utf-8"))
    assert hk["quote"]["ticker"] == "HKEX:700"
    assert hk["quote"]["columns"]["close"] == 600.0
    assert abs(hk["quote"]["columns"]["day_change_pct"] - (-0.005)) < 1e-12
    assert hk["quote"]["endpoint"].endswith("/hongkong/scan")
    assert [q["tv_label"] for q in hk["future_quarters"]] == ["2026-Q2", "2026-Q3", "2026-Q4", "2027-Q1"]


def test_refresh_soft_fails_when_scanner_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(tv, "data_dir", lambda ticker: tmp_path / ticker)
    monkeypatch.setattr(tv, "list_tickers", lambda: ["META"])
    monkeypatch.setattr(tv, "load_ticker_config", lambda t: {})

    def boom(url, body):
        raise RuntimeError("scanner down")

    out = tv.refresh_quotes(["META"], post=boom, sleep=lambda _: None)
    assert out["ok"] is False and out["written"] == 0
    assert out["errors"]
    assert out["results"][0]["ok"] is False


def test_daily_workflow_runs_tv_scan_before_build():
    daily = (ROOT / ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8")
    assert "python -m pipeline tv-scan" in daily
    assert "logs/daily_tv.log" in daily
    av_at = daily.index("python -m pipeline av-estimates")
    tv_at = daily.index("python -m pipeline tv-scan")
    build_at = daily.index("python -m pipeline build")
    assert av_at < tv_at < build_at
    # soft-fail：失败只 warning，不阻断
    assert "|| echo" in daily[tv_at : tv_at + 200]


def test_cli_tv_scan_registered():
    from pipeline.__main__ import main
    import sys
    from io import StringIO

    buf = StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
    finally:
        sys.stdout = old
    assert exc.value.code == 0
    help_text = buf.getvalue()
    assert "tv-scan" in help_text
    assert "TradingView" in help_text or "tradingview_scan.json" in help_text

def test_future_quarters_limit_four():
    raw = {
        "earnings_fq_h": [
            {"FiscalPeriod": f"2027-Q{i}", "Estimate": float(i), "IsReported": False}
            for i in range(1, 6)
        ],
        "revenues_fq_h": [
            {"FiscalPeriod": f"2027-Q{i}", "Estimate": float(i) * 10, "IsReported": False}
            for i in range(1, 6)
        ],
    }
    rows = tv.future_quarters_from_raw(raw, limit=4)
    assert len(rows) == 4
    assert rows[0] == {"tv_label": "2027-Q1", "eps_est": 1.0, "rev_est": 10.0}


def test_cutoff_pt_formats_los_angeles():
    from builder.build import _cutoff_pt, _source_cutoff

    assert _cutoff_pt("2026-10-23T12:00:00Z") == "2026-10-23 05:00 PT"
    assert _cutoff_pt("") == ""
    assert _cutoff_pt("not-a-date") == ""
    assert _source_cutoff("TradingView scanner", "2026-10-23T12:00:00Z") == (
        "TradingView scanner · 截止 2026-10-23 05:00 PT"
    )


def test_annual_consensus_prefers_alpha_vantage(tmp_path, monkeypatch):
    from builder import build as b
    from pipeline.sources import av_estimates as av

    monkeypatch.setattr(av, "data_dir", lambda ticker: tmp_path / ticker)
    monkeypatch.setattr(b, "_latest_snapshot", lambda ticker: {"snapshot_at": "2026-10-01", "earnings_estimate": {}})
    av.write_estimates_cache(
        "META",
        [
            {"date": "2026-12-31", "horizon": "fiscal year", "eps_estimate_average": "30.5"},
            {"date": "2027-12-31", "horizon": "fiscal year", "eps_estimate_average": "34.0"},
            {"date": "2026-12-31", "horizon": "fiscal quarter", "eps_estimate_average": "8.0"},
        ],
    )
    out = b._annual_consensus("META", 12)
    assert out["source"] == "alpha_vantage"
    assert out["years"][2026] == 30.5 and out["years"][2027] == 34.0
    assert out["fetched_at"]

