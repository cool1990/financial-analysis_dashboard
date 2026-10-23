"""当前季修正信号：评价顺序、季度选择，以及 EARNINGS_ESTIMATES 缓存。"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from pipeline.compute.revision_signal import (
    chg_30d_pct,
    choose_current_estimate,
    earnings_drive_tag,
    fiscal_period_end,
    format_current_quarter,
    pick_current_quarter_estimate,
    pick_fallback_quarter,
    quarter_end_after_reported,
    revision_signal_current_q,
)

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 23)


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


def test_drive_tag_is_only_the_past_thirty_days():
    today = date(2026, 10, 23)
    assert earnings_drive_tag([date(2026, 9, 23)], today) == "财报驱动"
    assert earnings_drive_tag([date(2026, 10, 23)], today) == "财报驱动"
    assert earnings_drive_tag([date(2026, 9, 22)], today) == "非财报"
    # 未来日期即使落在 30 天内也不算财报驱动
    assert earnings_drive_tag([date(2026, 10, 24)], today) == "非财报"
    assert earnings_drive_tag([date(2026, 11, 22)], today) == "非财报"
    assert earnings_drive_tag([date(2026, 11, 22), date(2026, 10, 1)], today) == "财报驱动"
    assert earnings_drive_tag([], today) is None


def _fixture(name: str) -> dict:
    return json.loads((ROOT / "tests" / "fixtures" / "av" / name).read_text(encoding="utf-8"))


def test_current_quarter_uses_reported_period_not_tradingview_or_yahoo():
    assert fiscal_period_end(2026, 4, 12) == date(2026, 12, 31)
    assert fiscal_period_end(2027, 1, 8) == date(2026, 11, 30)
    assert quarter_end_after_reported("FY2026Q3", 12) == date(2026, 12, 31)

    meta = _fixture("meta_quarters.json")
    picked = choose_current_estimate(
        meta["estimates"],
        reported_period="FY2026Q3",
        fiscal_year_end_month=12,
        today=TODAY,
    )
    assert picked["date"] == "2026-12-31"
    assert picked["horizon"] == "fiscal quarter"
    assert picked["eps_estimate_average"] == "8.2780"
    shown = format_current_quarter(picked, "财报驱动", fetched_at=meta["fetched_at"], now=datetime(2026, 10, 23, tzinfo=timezone.utc))
    assert shown["analysts"] == "43"
    assert shown["eps"] == "$8.28"
    assert shown["stale"] is False

    # 没有 TradingView 的 MCD：没有 FY 文件时，取今天往前约 100 天里最早的季度行，不拿 6 月那条
    mcd = _fixture("mcd_quarters.json")
    mcd_row = choose_current_estimate(
        mcd["estimates"],
        reported_period=None,
        fiscal_year_end_month=12,
        today=TODAY,
    )
    assert mcd_row["date"] == "2026-09-30"
    assert mcd_row["eps_estimate_average"] == "3.3843"


def test_ended_unreported_quarter_stays_current_and_far_rows_are_empty():
    meta = _fixture("meta_quarters.json")["estimates"]
    # 已披露到 Q2，Q3 季末已过但还没有 FY 文件：当前季仍是 Q3，不是 Q4
    ended = choose_current_estimate(
        meta,
        reported_period="FY2026Q2",
        fiscal_year_end_month=12,
        today=TODAY,
    )
    assert ended["date"] == "2026-09-30"
    assert ended["eps_estimate_average"] == "5.6986"

    # 目标季在样本里对不上（差超过 20 天）→ None，单元格是 — / 无数据
    missing = choose_current_estimate(
        meta,
        reported_period="FY2026Q4",
        fiscal_year_end_month=12,
        today=TODAY,
    )
    assert missing is None
    blank = format_current_quarter(missing, None)
    assert blank["eps"] == "—" and blank["analysts"] == "—" and blank["revisions"] == "—"
    assert blank["signal"] == "无数据"

    # 52/53 周允许几天偏差；差 21 天就不拿旁边那季
    assert pick_current_quarter_estimate(
        [{"date": "2026-11-26", "horizon": "fiscal quarter", "eps_estimate_average": "1"}],
        date(2026, 11, 30),
    )["eps_estimate_average"] == "1"
    assert pick_current_quarter_estimate(
        [
            {"date": "2026-11-09", "horizon": "fiscal quarter", "eps_estimate_average": "old"},
            {"date": "2027-02-28", "horizon": "historical fiscal quarter", "eps_estimate_average": "far"},
        ],
        date(2026, 11, 30),
    ) is None
    # 日期对上时，horizon 写着 historical 也照样用
    assert pick_current_quarter_estimate(
        [{"date": "2026-11-30", "horizon": "historical fiscal quarter", "eps_estimate_average": "1"}],
        date(2026, 11, 30),
    )["eps_estimate_average"] == "1"
    # 没有已披露财季时，早于窗口的历史行不选
    assert pick_fallback_quarter(
        [
            {"date": "2024-12-31", "horizon": "fiscal quarter", "eps_estimate_average": "old"},
            {"date": "2026-09-30", "horizon": "fiscal quarter", "eps_estimate_average": "now"},
        ],
        TODAY,
    )["eps_estimate_average"] == "now"


def test_null_revision_counts_as_zero_and_stale_cache_is_marked():
    row = _fixture("nke_null_down.json")["estimates"][0]
    assert row["eps_estimate_revision_down_trailing_30_days"] is None
    assert revision_signal_current_q(
        row["eps_estimate_average"],
        row["eps_estimate_analyst_count"],
        chg_30d_pct(row["eps_estimate_average"], row["eps_estimate_average_30_days_ago"]),
        row["eps_estimate_revision_up_trailing_30_days"],
        None,
    ) != "样本不足"
    shown = format_current_quarter(row, None, fetched_at="2026-10-19T00:00:00+00:00", now=datetime(2026, 10, 23, tzinfo=timezone.utc))
    assert shown["revisions"] == "4 / 0"
    assert shown["signal"] != "样本不足"
    assert shown["stale"] is True
    assert shown["stale_title"] == "旧 2026-10-19T00:00:00+00:00"
    fresh = format_current_quarter(row, None, fetched_at="2026-10-22T00:00:00+00:00", now=datetime(2026, 10, 23, tzinfo=timezone.utc))
    assert fresh["stale"] is False
    assert revision_signal_current_q(1.2, 10, 0.02, None, None) == "样本不足"
    assert format_current_quarter(
        {"eps_estimate_average": "1.2", "eps_estimate_analyst_count": "10", "eps_estimate_revision_up_trailing_30_days": None, "eps_estimate_revision_down_trailing_30_days": None},
        None,
    )["revisions"] == "—"


def test_index_row_uses_first_unpublished_quarter_not_the_next(monkeypatch):
    import builder.build as build
    from pipeline.config import load_ticker_config

    estimates = [
        {
            "date": "2027-03-31",
            "horizon": "fiscal quarter",
            "eps_estimate_average": "9.00",
            "eps_estimate_analyst_count": "10",
            "eps_estimate_average_30_days_ago": "9.00",
            "eps_estimate_revision_up_trailing_30_days": "1",
            "eps_estimate_revision_down_trailing_30_days": "0",
        },
        {
            "date": "2026-12-31",
            "horizon": "fiscal year",
            "eps_estimate_average": "30",
            "eps_estimate_analyst_count": "20",
            "eps_estimate_average_30_days_ago": "29",
            "eps_estimate_revision_up_trailing_30_days": "8",
            "eps_estimate_revision_down_trailing_30_days": "0",
        },
        {
            "date": "2026-12-31",
            "horizon": "fiscal quarter",
            "eps_estimate_average": "8.00",
            "eps_estimate_analyst_count": "10",
            "eps_estimate_average_30_days_ago": "7.00",
            "eps_estimate_revision_up_trailing_30_days": "4",
            "eps_estimate_revision_down_trailing_30_days": "0",
        },
    ]
    monkeypatch.setattr(
        "pipeline.sources.av_estimates.load_earnings_record",
        lambda ticker, **kwargs: (
            {"estimates": estimates, "fetched_at": "2026-10-23T00:00:00+00:00"}
            if ticker == "META"
            else {"estimates": [], "fetched_at": ""}
        ),
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
                    "horizon": "fiscal quarter",
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
                    "horizon": "fiscal quarter",
                    "eps_estimate_average": "8.29",
                    "eps_estimate_analyst_count": "12",
                    "eps_estimate_average_30_days_ago": "7.00",
                    "eps_estimate_revision_up_trailing_30_days": "4",
                    "eps_estimate_revision_down_trailing_30_days": "1",
                    "apikey": "secret-key",
                }
            ]
        }

    out = cmd.refresh_earnings_estimates(
        ["META"], get=fake_get, pause_seconds=0, sleep=sleeps.append, today=date(2026, 10, 23)
    )
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


def test_hermes_path_is_optional_env(monkeypatch, tmp_path):
    from pipeline.sources import av_estimates as av

    monkeypatch.delenv(av.HERMES_DIR_ENV, raising=False)
    assert av.hermes_cache_path("MU") is None
    monkeypatch.setenv(av.HERMES_DIR_ENV, str(tmp_path))
    assert av.hermes_cache_path("mu") == tmp_path / "MU.json"


def test_refresh_skips_cache_younger_than_twenty_hours(tmp_path, monkeypatch):
    from pipeline.commands import av_estimates_cmd as cmd
    from pipeline.sources import av_estimates as av

    monkeypatch.setattr(av, "data_dir", lambda ticker: tmp_path / ticker)
    monkeypatch.setattr(cmd, "data_dir", lambda ticker: tmp_path / ticker)
    monkeypatch.setenv("ALPHAVANTAGE_KEY", "secret-key")
    now = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
    av.write_estimates_cache("NVDA", [{"date": "2026-10-31", "horizon": "fiscal quarter", "eps_estimate_average": "2.47"}])
    fresh_path = tmp_path / "NVDA" / "consensus" / "av_estimates.json"
    payload = json.loads(fresh_path.read_text(encoding="utf-8"))
    payload["fetched_at"] = (now - timedelta(hours=19)).isoformat()
    fresh_path.write_text(json.dumps(payload), encoding="utf-8")
    calls = {"n": 0}

    def fake_get(url, params):
        calls["n"] += 1
        return {"estimates": [{"date": "2026-10-31", "horizon": "fiscal quarter", "eps_estimate_average": "2.50"}]}

    skipped = cmd.refresh_earnings_estimates(["NVDA"], get=fake_get, pause_seconds=0, now=now, today=now.date())
    assert calls["n"] == 0 and skipped["written"] == 0 and skipped["ok"] is True
    assert skipped["results"][0]["skipped"] is True

    payload["fetched_at"] = (now - timedelta(hours=21)).isoformat()
    fresh_path.write_text(json.dumps(payload), encoding="utf-8")
    refreshed = cmd.refresh_earnings_estimates(["NVDA"], get=fake_get, pause_seconds=0, now=now, today=now.date())
    assert calls["n"] == 1 and refreshed["written"] == 1
    record = av.load_earnings_record("NVDA", fetch=False)
    assert record["estimates"][0]["eps_estimate_average"] == "2.50"
    assert record["fetched_at"]
