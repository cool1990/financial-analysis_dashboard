"""当前季修正信号：评价顺序、季度选择，以及 EARNINGS_ESTIMATES 缓存。"""
from __future__ import annotations

import json
from datetime import date

from pipeline.compute.revision_signal import (
    chg_30d_pct,
    current_quarter_target,
    earnings_drive_tag,
    first_unpublished_date,
    fiscal_period_end,
    format_current_quarter,
    pick_current_quarter_estimate,
    revision_signal_current_q,
)


def test_signal_order_stops_at_first_match_and_skips_mild_labels():
    assert revision_signal_current_q(None, 20, 0.2, 10, 0) == "无数据"
    assert revision_signal_current_q(0.05, 20, 0.2, 10, 0) == "eps低基数"
    assert revision_signal_current_q(-0.09, 1, 0.2, 1, 0) == "eps低基数"
    assert revision_signal_current_q(1.2, 2, 0.2, 2, 0, drive="非财报") == "样本不足/非财报"
    assert revision_signal_current_q(1.2, 8, 0.2, 0, 0) == "样本不足"
    assert revision_signal_current_q(1.2, None, 0.2, 3, 1) == "样本不足"
    assert revision_signal_current_q("1.20", "10", 0.06, "5", "1", drive="非财报") == "强上修/非财报"
    assert revision_signal_current_q(1.5, 10, -0.06, 1, 5) == "强下修"
    # |净修正|/覆盖 = 3/40 = 7.5%，不到强；|净修正|≥2 → 少数
    assert revision_signal_current_q(2.0, 40, 0.06, 5, 2, drive="财报驱动") == "少数上修/财报驱动"
    assert revision_signal_current_q(1.5, 40, -0.08, 1, 4, drive="非财报") == "少数下修/非财报"
    # 刚好 10% 不算强，净修正 2 次仍是少数
    assert revision_signal_current_q(1.2, 20, 0.05, 3, 1) == "少数上修"
    # |净修正| < 2 不再标成少数，落到中性
    assert revision_signal_current_q(1.2, 10, 0.08, 1, 0) == "中性"
    assert revision_signal_current_q(1.2, 10, 0.02, 6, 1) == "中性"
    assert "温和" not in revision_signal_current_q(1.2, 10, 0.06, 5, 1)


def test_chg_30d_uses_abs_of_prior_average():
    assert chg_30d_pct(8, 7) == (8 - 7) / 7
    assert chg_30d_pct(-0.2, -0.4) == (-0.2 - -0.4) / 0.4
    assert chg_30d_pct(1, 0) is None
    assert chg_30d_pct(1, None) is None


def test_drive_tag_is_thirty_days_either_side():
    today = date(2026, 10, 23)
    assert earnings_drive_tag([date(2026, 9, 23)], today) == "财报驱动"
    assert earnings_drive_tag([date(2026, 9, 22)], today) == "非财报"
    assert earnings_drive_tag([date(2026, 11, 22)], today) == "财报驱动"
    assert earnings_drive_tag([], today) is None


def test_first_unpublished_quarter_never_uses_index_one():
    assert fiscal_period_end(2026, 4, 12) == date(2026, 12, 31)
    assert fiscal_period_end(2027, 1, 8) == date(2026, 11, 30)
    tv = {
        "future_quarters": [
            {"FiscalPeriod": "2026-Q4", "date": "2026-12-31"},
            {"FiscalPeriod": "2027-Q1", "date": "2027-03-31"},
        ]
    }
    assert first_unpublished_date(tv, fiscal_year_end_month=12) == date(2026, 12, 31)

    empty_first = {
        "future_quarters": [{}, {"date": "2027-03-31"}],
        "rows_used": {
            "earnings_fq_h_chart": [
                {"FiscalPeriod": "2026-Q3", "IsReported": True},
                {"FiscalPeriod": "2026-Q4", "IsReported": False},
                {"FiscalPeriod": "2027-Q1", "IsReported": False},
            ]
        },
    }
    assert first_unpublished_date(empty_first, fiscal_year_end_month=12) == date(2026, 12, 31)
    assert first_unpublished_date({"future_quarters": [{}, {"date": "2027-03-31"}]}, fiscal_year_end_month=12) is None

    yahoo_only = current_quarter_target(None, fiscal_year_end_month=12, yahoo_0q_date=date(2026, 12, 16))
    assert yahoo_only == date(2026, 12, 16)
    assert current_quarter_target(tv, fiscal_year_end_month=12, yahoo_0q_date=date(2026, 3, 1)) == date(2026, 12, 31)


def test_pick_quarter_matches_target_date_not_the_later_quarter_or_the_year():
    estimates = [
        {
            "date": "2027-03-31",
            "horizon": "next fiscal quarter",
            "eps_estimate_average": "9",
        },
        {
            "date": "2026-12-31",
            "horizon": "next fiscal year",
            "eps_estimate_average": "30",
        },
        {
            "date": "2026-12-31",
            "horizon": "next fiscal quarter",
            "eps_estimate_average": "8.29",
            "eps_estimate_analyst_count": "12",
            "eps_estimate_average_30_days_ago": "7.00",
            "eps_estimate_revision_up_trailing_30_days": "4",
            "eps_estimate_revision_down_trailing_30_days": "0",
        },
    ]
    picked = pick_current_quarter_estimate(estimates, date(2026, 12, 31))
    assert picked["eps_estimate_average"] == "8.29"
    # 公告日更接近本季期末，而不是下一季
    announced = pick_current_quarter_estimate(
        [
            {"date": "2027-02-28", "horizon": "next fiscal quarter", "eps_estimate_average": "2"},
            {"date": "2026-11-30", "horizon": "next fiscal quarter", "eps_estimate_average": "1"},
        ],
        date(2026, 12, 16),
    )
    assert announced["eps_estimate_average"] == "1"
    # 没有日期时只取第一条季度行
    assert pick_current_quarter_estimate(
        [
            {"horizon": "next fiscal quarter", "eps_estimate_average": "1"},
            {"horizon": "next fiscal quarter", "eps_estimate_average": "2"},
        ],
        None,
    )["eps_estimate_average"] == "1"
    shown = format_current_quarter(picked, "财报驱动")
    assert shown["analysts"] == "12"
    assert shown["eps"] == "$8.29"
    assert shown["chg"]["text"] == "+18.4%"
    assert shown["revisions"] == "4 / 0"
    assert shown["signal"] == "强上修/财报驱动"


def test_index_row_uses_first_unpublished_quarter_not_the_next(monkeypatch):
    import builder.build as build
    from pipeline.config import load_ticker_config

    estimates = [
        {
            "date": "2027-03-31",
            "horizon": "next fiscal quarter",
            "eps_estimate_average": "9.00",
            "eps_estimate_analyst_count": "10",
            "eps_estimate_average_30_days_ago": "9.00",
            "eps_estimate_revision_up_trailing_30_days": "1",
            "eps_estimate_revision_down_trailing_30_days": "0",
        },
        {
            "date": "2026-12-31",
            "horizon": "next fiscal year",
            "eps_estimate_average": "30",
            "eps_estimate_analyst_count": "20",
            "eps_estimate_average_30_days_ago": "29",
            "eps_estimate_revision_up_trailing_30_days": "8",
            "eps_estimate_revision_down_trailing_30_days": "0",
        },
        {
            "date": "2026-12-31",
            "horizon": "next fiscal quarter",
            "eps_estimate_average": "8.00",
            "eps_estimate_analyst_count": "10",
            "eps_estimate_average_30_days_ago": "7.00",
            "eps_estimate_revision_up_trailing_30_days": "4",
            "eps_estimate_revision_down_trailing_30_days": "0",
        },
    ]
    monkeypatch.setattr(
        "pipeline.sources.av_estimates.load_earnings_estimates",
        lambda ticker, **kwargs: estimates if ticker == "META" else [],
    )
    meta = json.loads((build.ROOT / "data" / "META" / "FY2026Q3.json").read_text(encoding="utf-8"))
    row = build._index_row("META", load_ticker_config("META"), "FY2026Q3", meta, today=date(2026, 10, 23))
    assert row["quarter"]["eps"] == "$8.00"
    assert row["quarter"]["analysts"] == "10"
    assert row["quarter"]["chg"]["text"] == "+14.3%"
    assert row["quarter"]["revisions"] == "4 / 0"
    assert row["quarter"]["signal"] == "强上修/财报驱动"


def test_av_cache_roundtrip_and_hermes_fallback(tmp_path, monkeypatch):
    from pipeline.sources import av_estimates as av

    monkeypatch.setattr(av, "data_dir", lambda ticker: tmp_path / ticker)
    monkeypatch.setattr(av, "hermes_cache_path", lambda ticker: tmp_path / "hermes" / f"{ticker}.json")
    monkeypatch.delenv("ALPHAVANTAGE_KEY", raising=False)
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    assert av.load_earnings_estimates("MU") == []

    hermes = tmp_path / "hermes" / "MU.json"
    hermes.parent.mkdir(parents=True)
    hermes.write_text(
        json.dumps({"estimates": [{"horizon": "next fiscal quarter", "eps_estimate_average": "1.5"}]}),
        encoding="utf-8",
    )
    assert av.load_earnings_estimates("MU")[0]["eps_estimate_average"] == "1.5"

    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "secret-key")
    called = {"n": 0}

    def fake_get(url, params):
        called["n"] += 1
        assert params["apikey"] == "secret-key"
        assert "secret-key" not in url
        return {
            "estimates": [
                {
                    "date": "2026-11-30",
                    "horizon": "next fiscal quarter",
                    "eps_estimate_average": "38.28",
                    "eps_estimate_analyst_count": "29",
                    "eps_estimate_average_30_days_ago": "35.66",
                    "eps_estimate_revision_up_trailing_30_days": "4",
                    "eps_estimate_revision_down_trailing_30_days": "1",
                }
            ]
        }

    # Hermes 已有文件时不再请求
    assert av.load_earnings_estimates("MU", get=fake_get)[0]["eps_estimate_average"] == "1.5"
    assert called["n"] == 0

    monkeypatch.setattr(av, "hermes_cache_path", lambda ticker: tmp_path / "missing" / f"{ticker}.json")
    rows = av.load_earnings_estimates("SNDK", get=fake_get)
    assert rows[0]["eps_estimate_analyst_count"] == "29"
    cached = (tmp_path / "SNDK" / "consensus" / "av_estimates.json").read_text(encoding="utf-8")
    assert "secret-key" not in cached
    assert "eps_estimate_revision_up_trailing_30_days" in cached
    assert av.load_earnings_estimates("SNDK", get=fake_get)[0]["eps_estimate_average"] == "38.28"
    assert called["n"] == 1


def test_refresh_writes_cache_and_retries_rate_limit(tmp_path, monkeypatch):
    from pipeline.commands import av_estimates_cmd as cmd
    from pipeline.sources import av_estimates as av

    monkeypatch.setattr(av, "data_dir", lambda ticker: tmp_path / ticker)
    monkeypatch.setattr(cmd, "data_dir", lambda ticker: tmp_path / ticker)
    monkeypatch.setenv("ALPHAVANTAGE_KEY", "secret-key")
    calls = {"n": 0}
    sleeps: list[float] = []

    def fake_get(url, params):
        calls["n"] += 1
        assert params["apikey"] == "secret-key"
        if calls["n"] == 1:
            return {"Note": "Thank you for using Alpha Vantage! Please spread out your requests."}
        return {
            "estimates": [
                {
                    "date": "2026-12-31",
                    "horizon": "next fiscal quarter",
                    "eps_estimate_average": "8.29",
                    "eps_estimate_analyst_count": "12",
                    "eps_estimate_average_30_days_ago": "7.00",
                    "eps_estimate_revision_up_trailing_30_days": "4",
                    "eps_estimate_revision_down_trailing_30_days": "1",
                    "apikey": "secret-key",
                }
            ]
        }

    out = cmd.refresh_earnings_estimates(["META"], get=fake_get, pause_seconds=0, sleep=sleeps.append)
    assert out["written"] == 1 and out["results"][0]["analysts"] == "12"
    assert out["results"][0]["eps"] == "$8.29" and out["results"][0]["revisions"] == "4 / 1"
    cached = (tmp_path / "META" / "consensus" / "av_estimates.json").read_text(encoding="utf-8")
    assert "secret-key" not in cached and "eps_estimate_average" in cached
    assert calls["n"] == 2 and sleeps

    monkeypatch.delenv("ALPHAVANTAGE_KEY", raising=False)
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    skipped = cmd.refresh_earnings_estimates(["META"], get=lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应发请求")))
    assert skipped["written"] == 0 and "ALPHAVANTAGE_KEY" in skipped["reason"]
