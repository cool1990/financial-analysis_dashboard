"""history 命令、小夹具页面，以及无新 8-K 时 poll 不做事。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pipeline.commands.history_cmd import (
    HISTORY_FILES,
    build_history_documents,
    refresh_history,
    refresh_history_if_new,
)
from pipeline.sources.history_filings import instance_name


def test_instance_name_prefers_inline_xbrl_and_skips_linkbases():
    assert instance_name(["meta-20260930_cal.xml", "meta-20260930_htm.xml", "FilingSummary.xml"]) == "meta-20260930_htm.xml"
    assert instance_name(["fb-20160331_pre.xml", "fb-20160331.xml", "FilingSummary.xml"]) == "fb-20160331.xml"
    assert instance_name(["FilingSummary.xml", "notes.htm"]) is None


def test_statement_does_not_invent_a_missing_quarter():
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "start": "2016-01-01",
                                "end": "2016-03-31",
                                "val": 10,
                                "filed": "2016-04-28",
                                "form": "10-Q",
                            }
                        ]
                    }
                },
                "CostOfRevenue": {
                    "units": {
                        "USD": [
                            {
                                "start": "2016-01-01",
                                "end": "2016-03-31",
                                "val": 4,
                                "filed": "2016-04-28",
                                "form": "10-Q",
                            }
                        ]
                    }
                },
            }
        }
    }
    docs = build_history_documents(
        "META",
        {"cik": "1326801", "fiscal_year_end_month": 12},
        [],
        [],
        facts,
        latest="0001326801-16-000067",
        generated_at="2026-10-23T00:00:00Z",
    )
    rows = docs["statement_history"]["quarters"]
    assert [row["period"] for row in rows] == ["FY2016Q1"]
    assert rows[0]["revenue"] == 10
    assert rows[0]["gross_profit"] == 6
    assert rows[0]["geo"] is None
    assert rows[0]["opex"] is None
    assert docs["segment_history"]["generated_at"] == "2026-10-23T00:00:00Z"
    assert docs["xbrl_activity"]["latest_accession"] == "0001326801-16-000067"
    assert docs["xbrl_activity"]["diluted_shares"] == []


def test_refresh_skips_hk_and_adr_without_calling_sec(monkeypatch):
    from pipeline.commands import history_cmd

    configs = {
        "0700.HK": {"listing": "hk", "cik": "", "fiscal_year_end_month": 12},
        "TSM": {"listing": "adr", "cik": "0001046179", "fiscal_year_end_month": 12},
    }
    monkeypatch.setattr(history_cmd, "load_ticker_config", lambda ticker: configs[ticker])
    monkeypatch.setattr(history_cmd, "SecEdgarClient", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应访问 SEC")))
    hk = refresh_history("0700.HK")
    adr = refresh_history("TSM")
    assert hk["skipped"] is True and "listing=hk" in hk["reason"]
    assert adr["skipped"] is True and "listing=adr" in adr["reason"]


def test_tsm_config_is_adr():
    from pipeline.config import load_ticker_config

    assert load_ticker_config("TSM").get("listing") == "adr"


def test_if_new_refreshes_only_when_the_10q_accession_changes(monkeypatch, tmp_path):
    from pipeline.commands import history_cmd

    monkeypatch.setattr(history_cmd, "list_tickers", lambda: ["META"])
    monkeypatch.setattr(
        history_cmd,
        "load_ticker_config",
        lambda ticker: {"cik": "0001326801", "fiscal_year_end_month": 12},
    )
    monkeypatch.setattr(history_cmd, "data_dir", lambda ticker=None: tmp_path)
    for name in HISTORY_FILES:
        (tmp_path / name).write_text(json.dumps({"latest_accession": "000-1"}), encoding="utf-8")
    monkeypatch.setattr(history_cmd, "SecEdgarClient", lambda *a, **k: object())
    monkeypatch.setattr(
        history_cmd,
        "list_periodic_filings",
        lambda *a, **k: [{"filing_date": "2026-10-22", "accepted": "t", "accession": "000-1"}],
    )
    called: list[str] = []
    monkeypatch.setattr(history_cmd, "refresh_history", lambda *a, **k: called.append(a[0]) or {"skipped": False})
    quiet = refresh_history_if_new()
    assert called == []
    assert quiet["results"][0]["refreshed"] is False

    monkeypatch.setattr(
        history_cmd,
        "list_periodic_filings",
        lambda *a, **k: [{"filing_date": "2026-10-22", "accepted": "t", "accession": "000-2"}],
    )
    monkeypatch.setattr(
        history_cmd,
        "refresh_history",
        lambda *a, **k: {"ticker": "META", "skipped": False, "latest_accession": "000-2"},
    )
    changed = refresh_history_if_new()
    assert changed["results"][0]["refreshed"] is True
    assert changed["results"][0]["latest_accession"] == "000-2"


def test_pool_if_new_does_not_backfill_tickers_without_history(monkeypatch, tmp_path):
    from pipeline.commands import history_cmd

    monkeypatch.setattr(history_cmd, "list_tickers", lambda: ["MU"])
    monkeypatch.setattr(
        history_cmd,
        "load_ticker_config",
        lambda ticker: {"cik": "0000723125", "fiscal_year_end_month": 8},
    )
    monkeypatch.setattr(history_cmd, "data_dir", lambda ticker=None: tmp_path / "empty")
    monkeypatch.setattr(history_cmd, "SecEdgarClient", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应访问 SEC")))
    called: list[str] = []
    monkeypatch.setattr(history_cmd, "refresh_history", lambda *a, **k: called.append(a[0]))
    out = refresh_history_if_new()
    assert called == []
    assert out["results"][0]["refreshed"] is False
    assert "还没有历史文件" in out["results"][0]["reason"]


def test_backfill_xbrl_only_refreshes_history(monkeypatch):
    from pipeline.commands import backfill_cmd

    monkeypatch.setattr(backfill_cmd, "write_xbrl_history", lambda *a, **k: {"written": ["FY2016Q1"]})
    monkeypatch.setattr(
        "pipeline.commands.history_cmd.refresh_history",
        lambda ticker, **k: {"ticker": ticker.upper(), "skipped": False, "latest_accession": "acc"},
    )
    out = backfill_cmd.backfill("META", xbrl_only=True)
    assert out["written"] == ["FY2016Q1"]
    assert out["history"]["latest_accession"] == "acc"


def test_stage1_success_asks_for_history_refresh(monkeypatch):
    from pipeline.commands import run_cmd

    monkeypatch.setattr(run_cmd, "load_ticker_config", lambda ticker: {"cik": "1"})
    monkeypatch.setattr(run_cmd, "SecEdgarClient", lambda *a, **k: object())
    monkeypatch.setattr(run_cmd, "detect_new_filings", lambda *a, **k: [{"accessionNumber": "new"}])
    doc = {"meta": {"fiscal_period": "FY2026Q1"}, "status": {"warnings": []}}
    monkeypatch.setattr(run_cmd, "_run_stage1_filing", lambda *a, **k: doc)
    seen: list[str] = []
    monkeypatch.setattr(run_cmd, "_attach_history", lambda ticker, payload: seen.append(ticker))
    assert run_cmd.run_stage1("META") is doc
    assert seen == ["META"]


def test_attach_history_failure_keeps_stage1(monkeypatch):
    from pipeline.commands import run_cmd

    def boom(ticker: str, **kwargs):
        raise RuntimeError("sec down")

    monkeypatch.setattr("pipeline.commands.history_cmd.refresh_history", boom)
    saved: list[tuple] = []
    monkeypatch.setattr(run_cmd, "save_period_json", lambda *a, **k: saved.append(a))
    doc = {"meta": {"fiscal_period": "FY2026Q1"}, "status": {"warnings": []}}
    run_cmd._attach_history("META", doc)
    assert any("未刷新" in item for item in doc["status"]["warnings"])
    assert saved and saved[0][1] == "FY2026Q1"


def test_poll_once_did_work_is_false_without_a_new_8k(monkeypatch, tmp_path):
    """没有新 8-K、也没有待补的 Stage2 时，did_work 为 False。"""
    from pipeline.commands import poll_cmd

    monkeypatch.setattr(poll_cmd, "list_tickers", lambda: ["NVDA"])
    monkeypatch.setattr(poll_cmd, "load_ticker_config", lambda ticker: {"release_timing": "amc", "cik": "0001045810"})
    monkeypatch.setattr(poll_cmd, "data_dir", lambda ticker=None: tmp_path)
    monkeypatch.setattr(poll_cmd, "_in_window", lambda *a, **k: True)
    monkeypatch.setattr(poll_cmd, "near_earnings", lambda *a, **k: True)
    monkeypatch.setattr(poll_cmd, "detect_new_filings", lambda *a, **k: [])
    monkeypatch.setattr(poll_cmd, "run_stage1", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应跑 Stage1")))
    monkeypatch.setattr(poll_cmd, "run_stage2", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应跑 Stage2")))
    out = poll_cmd.poll_once(datetime(2026, 10, 23, 21, 0, tzinfo=timezone.utc))
    assert out["did_work"] is False
    assert out["reported"] == []
    assert out["actions"][0]["detail"] == "no new 8-K"


def test_build_site_small_fixture_has_homepage_cells_and_fin(tmp_path, monkeypatch):
    import builder.build as build
    from pipeline.state import empty_period_doc

    data = tmp_path / "data"
    site = tmp_path / "site"
    ticker = "FIX"
    folder = data / ticker
    folder.mkdir(parents=True)
    specs = [
        ("FY2026Q1", "2026-01-01", "2026-03-31", 10.0, 1.0),
        ("FY2026Q2", "2026-04-01", "2026-06-30", 11.0, 1.1),
        ("FY2026Q3", "2026-07-01", "2026-09-30", 12.0, 1.2),
    ]
    for period, start, end, revenue, eps in specs:
        doc = empty_period_doc(
            ticker,
            period,
            calendar_quarter="2026Q3",
            period_start=start,
            period_end=end,
            release_at_utc="2026-10-21T20:00:00Z",
            accession="0000000000-26-000001",
            eps_basis="gaap",
        )
        doc["status"]["stage"] = "stage1_done"
        doc["scorecard"] = [
            {
                "metric": "revenue",
                "actual": revenue * 1_000_000_000,
                "verdict": "beat",
                "benchmark": {"value": revenue * 0.9 * 1_000_000_000, "diff_pct": 0.1},
            },
            {
                "metric": "eps",
                "actual": eps,
                "verdict": "miss",
                "benchmark": {"value": eps + 0.2, "diff_pct": -0.1},
            },
        ]
        doc["financials"] = {
            "revenue": {"value": revenue * 1_000_000_000, "yoy_pct": 0.1, "qoq_pct": 0.05},
            "eps_gaap": {"value": eps, "yoy_pct": 0.02, "qoq_pct": None},
        }
        (folder / f"{period}.json").write_text(json.dumps(doc), encoding="utf-8")

    monkeypatch.setattr(build, "site_dir", lambda: site)
    monkeypatch.setattr(build, "list_tickers", lambda: [ticker])
    monkeypatch.setattr(build, "data_dir", lambda name=None: data if not name else data / name)
    monkeypatch.setattr(build, "load_position", lambda name: None)
    monkeypatch.setattr(
        build,
        "load_ticker_config",
        lambda name: {"name": "Fixture Co", "fiscal_year_end_month": 12, "release_timing": "amc", "ticker": name},
    )
    pages = build.build_site()
    index = (site / "index.html").read_text(encoding="utf-8")
    assert "FIX" in index
    assert "营收" in index and "EPS" in index and "股价" in index
    assert "beat 10.0%" in index and "miss 10.0%" in index
    period_html = (site / "stocks" / "FIX" / "FY2026Q3.html").read_text(encoding="utf-8")
    assert 'id="fin"' in period_html
    assert "财报分析" in period_html and "营业收入" in period_html
    written = {Path(path).name for path in pages}
    assert {"FY2026Q1.html", "FY2026Q2.html", "FY2026Q3.html", "index.html"} <= written
