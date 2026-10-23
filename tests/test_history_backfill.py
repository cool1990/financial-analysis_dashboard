"""缺季回补不覆盖已有 JSON；港股 / ADR 历史只汇总已经写明的数字。"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from pipeline.extract.adr_statement import parse_adr_press_statement


PRESS = """
TSMC today announced consolidated revenue of NT$868.46 billion, net income of NT$374.68 billion,
and diluted earnings per share of NT$14.45 (US$2.24 per ADR unit) for the fourth quarter ended December 31, 2024.
In US dollars, fourth quarter revenue was $26.88 billion, which increased 37.0% year-over-year.
Gross margin for the quarter was 59.0%, operating margin was 49.0%, and net profit margin was 43.1%.
"""


def test_adr_press_keeps_official_usd_and_skips_ntd_net_income():
    parsed = parse_adr_press_statement(PRESS)
    assert parsed["period_end"] == "2024-12-31"
    assert parsed["amounts"]["revenue"]["value"] == 26_880_000_000
    assert parsed["amounts"]["eps_gaap"]["value"] == 2.24
    assert "per ADR" in parsed["amounts"]["eps_gaap"]["source_quote"]
    assert "net_income_gaap" not in parsed["amounts"]
    assert parsed["margins"]["gross"] == 0.59
    assert any("不按汇率" in item for item in parsed["warnings"])


def test_xbrl_history_does_not_overwrite_existing_period(monkeypatch):
    from pipeline.commands import backfill_cmd

    saved: dict[str, dict] = {}
    existing = {"FY2016Q1": {"status": {"stage": "backfill_gaap"}, "financials": {"revenue": {"value": 1}}}}

    monkeypatch.setattr(backfill_cmd, "load_ticker_config", lambda ticker: {"cik": "1", "fiscal_year_end_month": 12})
    monkeypatch.setattr(backfill_cmd, "load_period_json", lambda ticker, period: existing.get(period))
    monkeypatch.setattr(backfill_cmd, "save_period_json", lambda ticker, period, doc: saved.__setitem__(period, doc))
    monkeypatch.setattr(
        backfill_cmd,
        "quarterly_statements",
        lambda facts: {
            date(2016, 3, 31): {"revenue": {"value": 10}, "eps": {"value": 0.2}},
            date(2016, 6, 30): {"revenue": {"value": 11}, "eps": {"value": 0.3}},
        },
    )
    monkeypatch.setattr(backfill_cmd, "SecEdgarClient", lambda: SimpleNamespace(company_facts=lambda cik: {}))

    out = backfill_cmd.write_xbrl_history("META", start="2016-01-01")
    assert out["skipped"] == ["FY2016Q1"]
    assert out["written"] == ["FY2016Q2"]
    assert "FY2016Q1" not in saved
    assert saved["FY2016Q2"]["financials"]["revenue"]["value"] == 11


def test_hk_history_from_periods_leaves_missing_quarter_blank():
    from pipeline.sources.period_history import build_hk_histories_from_periods

    def doc(revenue, gross, vas):
        return {
            "meta": {"period_end": "2017-03-31"},
            "financials": {
                "revenue": {"value": revenue},
                "gross_profit_gaap": {"value": gross},
                "kpis": {"vas_revenue": {"value": vas}, "selling_expense": {"value": 10}},
            },
        }

    statement, segment = build_hk_histories_from_periods(
        "0700.HK",
        {"FY2017Q1": doc(100, 40, 70), "FY2017Q3": doc(120, 50, 80)},
    )
    by_period = {row["period"]: row for row in statement["quarters"]}
    assert [row["period"] for row in statement["quarters"]] == ["FY2017Q1", "FY2017Q2", "FY2017Q3"]
    assert by_period["FY2017Q2"]["revenue"] is None
    assert by_period["FY2017Q1"]["cost_of_revenue"] == 60
    assert by_period["FY2017Q1"]["opex"] is None
    lines = {row["period"]: row["lines"] for row in segment["quarters"]}
    assert [line["key"] for line in lines["FY2017Q1"]] == ["vas"]
    assert lines["FY2017Q2"] == []


def test_adr_extend_does_not_replace_slide_revenue_or_invent_fx_net_income():
    from pipeline.sources.period_history import extend_adr_history_from_periods

    statement = {
        "source": "幻灯片。不按当季平均汇率把新台币换算成美元。",
        "quarters": [
            {
                "period": "FY2024Q1",
                "end": "2024-03-31",
                "revenue": 18_870_000_000,
                "revenue_method": "slide",
                "net_income": 100,
                "eps_adr_usd": None,
            }
        ],
    }
    segment = {"quarters": [{"period": "FY2024Q1", "end": "2024-03-31", "method": "slide", "lines": [{"key": "hpc"}]}]}
    docs = {
        "FY2024Q1": {
            "meta": {"period_end": "2024-03-31"},
            "financials": {
                "revenue": {"value": 1, "source_quote": "In US dollars, revenue was $1 billion"},
                "net_income_gaap": {"value": 9_999, "source_quote": "net income of NT$999"},
                "eps_gaap": {"value": 1.5, "source_quote": "US$1.50 per ADR unit"},
            },
        },
        "FY2016Q1": {
            "meta": {"period_end": "2016-03-31"},
            "financials": {
                "revenue": {"value": 6_000_000_000, "source_quote": "In US dollars, first quarter revenue was $6.00 billion"},
                "eps_gaap": {"value": 0.4, "source_quote": "US$0.40 per ADR unit"},
                "gross_margin_gaap": {"value": 0.5, "source_quote": "Gross margin for the quarter was 50.0%"},
            },
        },
    }
    statement, segment = extend_adr_history_from_periods(statement, segment, docs)
    by_period = {row["period"]: row for row in statement["quarters"]}
    assert by_period["FY2024Q1"]["revenue"] == 18_870_000_000
    assert by_period["FY2024Q1"]["net_income"] == 100
    assert by_period["FY2024Q1"]["eps_adr_usd"] == 1.5
    assert by_period["FY2016Q1"]["revenue"] == 6_000_000_000
    assert by_period["FY2016Q1"]["gross_profit"] == 3_000_000_000
    assert "net_income" not in by_period["FY2016Q1"]
    assert "fx_usd_ntd" not in by_period["FY2016Q1"]
    assert by_period["FY2016Q2"]["revenue"] is None
    hpc = next(row for row in segment["quarters"] if row["period"] == "FY2024Q1")
    assert hpc["lines"][0]["key"] == "hpc"
    assert next(row for row in segment["quarters"] if row["period"] == "FY2016Q1")["lines"] == []
