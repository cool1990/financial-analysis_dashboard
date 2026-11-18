from datetime import date, timedelta

import pytest

from builder.build import _history, adjust_diluted_shares
from pipeline.sources.xbrl_history import quarterly_statements


def _row(start, end, val, filed="2020-01-01"):
    return {"start": start, "end": end, "val": val, "filed": filed, "form": "10-Q", "fp": "Q1"}


def test_quarterly_prefers_single_quarter_and_does_not_residual_eps():
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            _row("2016-01-01", "2016-03-31", 100),
                            _row("2016-04-01", "2016-06-30", 110),
                            _row("2016-07-01", "2016-09-30", 120),
                            _row("2016-01-01", "2016-09-30", 330),
                            _row("2016-01-01", "2016-12-31", 500, filed="2017-02-01"),
                        ]
                    }
                },
                "EarningsPerShareDiluted": {
                    "units": {
                        "USD/shares": [
                            _row("2016-01-01", "2016-03-31", 0.5),
                            _row("2016-01-01", "2016-12-31", 2.0, filed="2017-02-01"),
                        ]
                    }
                },
                "NetCashProvidedByUsedInOperatingActivities": {
                    "units": {
                        "USD": [
                            _row("2016-01-01", "2016-03-31", 40),
                            _row("2016-01-01", "2016-06-30", 90),
                        ]
                    }
                },
            }
        }
    }
    series = quarterly_statements(facts)
    assert series[date(2016, 3, 31)]["revenue"]["value"] == 100
    assert series[date(2016, 3, 31)]["revenue"]["derived"] is False
    assert series[date(2016, 12, 31)]["revenue"]["value"] == 170  # 500 - 330
    assert series[date(2016, 12, 31)]["revenue"]["derived"] is True
    assert series[date(2016, 3, 31)]["eps"]["value"] == 0.5
    assert series[date(2016, 12, 31)]["eps"] is None
    assert series[date(2016, 6, 30)]["ocf"]["value"] == 50  # 90 - 40
    assert series[date(2016, 6, 30)]["ocf"]["derived"] is True


def test_bank_revenue_is_net_interest_plus_noninterest_when_revenues_missing():
    facts = {
        "facts": {
            "us-gaap": {
                "InterestIncomeExpenseNet": {"units": {"USD": [_row("2026-04-01", "2026-06-30", 10)]}},
                "NoninterestIncome": {"units": {"USD": [_row("2026-04-01", "2026-06-30", 7)]}},
                "Revenues": {"units": {"USD": [_row("2026-01-01", "2026-03-31", 99)]}},
            }
        }
    }
    series = quarterly_statements(facts)
    derived = series[date(2026, 6, 30)]["revenue"]
    assert derived["value"] == 17
    assert derived["derived"] is True
    assert series[date(2026, 3, 31)]["revenue"]["value"] == 99
    assert series[date(2026, 3, 31)]["revenue"]["derived"] is False


def test_gross_margin_is_revenue_minus_cost_over_revenue():
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": [_row("2016-01-01", "2016-03-31", 200)]}},
                "CostOfRevenue": {"units": {"USD": [_row("2016-01-01", "2016-03-31", 50)]}},
            }
        }
    }
    node = quarterly_statements(facts)[date(2016, 3, 31)]["gross_margin"]
    assert node["value"] == 0.75
    assert "CostOfRevenue" in node["source_quote"]


def test_latest_filing_wins_same_span():
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            _row("2016-01-01", "2016-03-31", 90, filed="2016-04-28"),
                            _row("2016-01-01", "2016-03-31", 100, filed="2017-05-04"),
                        ]
                    }
                }
            }
        }
    }
    series = quarterly_statements(facts)
    assert series[date(2016, 3, 31)]["revenue"]["value"] == 100


def _revenue_ending(annual_end: date, prior_end: date, annual: float = 500, prior: float = 330):
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            _row("2016-01-01", annual_end.isoformat(), annual),
                            _row("2016-01-01", prior_end.isoformat(), prior),
                        ]
                    }
                }
            }
        }
    }
    return quarterly_statements(facts).get(annual_end)


def test_residual_rejects_span_wider_than_a_quarter_and_accepts_a_normal_one():
    """单季残差只接受结束日相距 80–100 天。半年累计不能倒挤成第四季。"""
    annual_end = date(2016, 12, 31)
    nine_month = _revenue_ending(annual_end, date(2016, 9, 30), prior=330)
    assert nine_month is not None
    assert nine_month["revenue"]["value"] == 170
    assert nine_month["revenue"]["derived"] is True
    assert (annual_end - date(2016, 9, 30)).days == 92

    # 全年 − 上半年 = 184 天。不产出，也不把 500 − 180 写成单季。
    assert (annual_end - date(2016, 6, 30)).days == 184
    assert _revenue_ending(annual_end, date(2016, 6, 30), prior=180) is None

    # 九个月和半年都在时，用相距一个季度的那条，而不是更早的半年。
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            _row("2016-01-01", "2016-12-31", 500),
                            _row("2016-01-01", "2016-09-30", 330),
                            _row("2016-01-01", "2016-06-30", 180),
                        ]
                    }
                }
            }
        }
    }
    both = quarterly_statements(facts)[annual_end]["revenue"]
    assert both["value"] == 170 and both["derived"] is True

    assert _revenue_ending(annual_end, annual_end - timedelta(days=80)) is not None
    assert _revenue_ending(annual_end, annual_end - timedelta(days=100)) is not None
    assert _revenue_ending(annual_end, annual_end - timedelta(days=79)) is None
    assert _revenue_ending(annual_end, annual_end - timedelta(days=101)) is None


def test_capex_prefers_productive_assets_and_does_not_add_the_ppe_tag():
    """同一季两条资本开支都有时用 productive assets，不把固定资产再加一遍。"""
    facts = {
        "facts": {
            "us-gaap": {
                "PaymentsToAcquireProductiveAssets": {
                    "units": {"USD": [_row("2026-01-26", "2026-04-26", 1_757_000_000, filed="2026-05-20")]}
                },
                "PaymentsToAcquirePropertyPlantAndEquipment": {
                    "units": {"USD": [_row("2026-01-26", "2026-04-26", 99_000_000, filed="2026-08-26")]}
                },
                "NetCashProvidedByUsedInOperatingActivities": {
                    "units": {"USD": [_row("2026-01-26", "2026-04-26", 5_000_000_000)]}
                },
            }
        }
    }
    node = quarterly_statements(facts)[date(2026, 4, 26)]
    assert node["capex"]["value"] == 1_757_000_000
    assert node["capex"]["tag"] == "PaymentsToAcquireProductiveAssets"
    assert node["fcf"]["value"] == 5_000_000_000 - 1_757_000_000


def test_capex_falls_back_to_ppe_and_residuals_q4_from_the_annual_span():
    facts = {
        "facts": {
            "us-gaap": {
                "PaymentsToAcquirePropertyPlantAndEquipment": {
                    "units": {
                        "USD": [
                            _row("2016-01-01", "2016-03-31", 30),
                            _row("2016-01-01", "2016-06-30", 50),
                            _row("2016-01-01", "2016-09-30", 80),
                        ]
                    }
                },
                "PaymentsToAcquireProductiveAssets": {
                    "units": {
                        "USD": [
                            _row("2016-01-01", "2016-09-30", 80),
                            _row("2016-01-01", "2016-12-31", 110, filed="2017-02-01"),
                        ]
                    }
                },
                "NetCashProvidedByUsedInOperatingActivities": {
                    "units": {"USD": [_row("2016-10-01", "2016-12-31", 40)]}
                },
            }
        }
    }
    series = quarterly_statements(facts)
    assert series[date(2016, 3, 31)]["capex"]["value"] == 30
    assert series[date(2016, 3, 31)]["capex"]["tag"] == "PaymentsToAcquirePropertyPlantAndEquipment"
    assert series[date(2016, 6, 30)]["capex"]["value"] == 20
    q4 = series[date(2016, 12, 31)]
    assert q4["capex"]["value"] == 30  # 110 - 80，不是两条标签相加
    assert q4["capex"]["derived"] is True
    assert q4["fcf"]["value"] == 10  # 40 - 30


def test_nvda_share_series_is_split_adjusted_and_does_not_residual_q4(monkeypatch):
    monkeypatch.setattr("builder.build._fill_statement_levels", lambda *a, **k: None)
    monkeypatch.setattr(
        "builder.build._xbrl_activity",
        lambda ticker: {
            "diluted_shares": [
                {"start": "2020-01-27", "end": "2020-04-26", "value": 622_000_000, "filed": "2021-05-26"},
                {"start": "2020-04-27", "end": "2020-07-26", "value": 2_504_000_000, "filed": "2021-08-20"},
                {"start": "2023-01-30", "end": "2023-04-30", "value": 2_490_000_000, "filed": "2024-05-29"},
                {"start": "2023-05-01", "end": "2023-07-30", "value": 24_994_000_000, "filed": "2024-08-28"},
                {"start": "2025-01-27", "end": "2026-01-25", "value": 24_514_000_000, "filed": "2026-02-25"},
                {"start": "2026-04-27", "end": "2026-07-26", "value": 24_285_000_000, "filed": "2026-08-26"},
            ]
        },
    )
    docs = {
        "FY2021Q1": {"meta": {"ticker": "NVDA", "period_end": "2020-04-26"}, "financials": {}},
        "FY2021Q2": {"meta": {"ticker": "NVDA", "period_end": "2020-07-26"}, "financials": {}},
        "FY2024Q1": {"meta": {"ticker": "NVDA", "period_end": "2023-04-30"}, "financials": {}},
        "FY2024Q2": {"meta": {"ticker": "NVDA", "period_end": "2023-07-30"}, "financials": {}},
        "FY2026Q4": {"meta": {"ticker": "NVDA", "period_end": "2026-01-25"}, "financials": {}},
        "FY2027Q2": {"meta": {"ticker": "NVDA", "period_end": "2026-07-26"}, "financials": {}},
        "FY2027Q3": {
            "meta": {"ticker": "NVDA", "period_end": "2026-10-25"},
            "financials": {"share_count": {"value": 24_186_000_000, "qoq_pct": None}},
        },
    }
    points = {row["period"]: row for row in _history(docs)}
    assert points["FY2021Q1"]["share_count"] == 622_000_000 * 40
    assert points["FY2021Q2"]["share_count"] == 2_504_000_000 * 10
    assert points["FY2024Q1"]["share_count"] == 2_490_000_000 * 10
    assert points["FY2024Q2"]["share_count"] == 24_994_000_000
    assert points["FY2026Q4"]["share_count"] is None
    assert points["FY2027Q2"]["share_count"] == 24_285_000_000
    assert points["FY2027Q3"]["share_count"] == 24_186_000_000
    assert adjust_diluted_shares("AAPL", 622_000_000, date(2020, 4, 26)) == 622_000_000


def test_nvda_history_keeps_q3_official_figures_and_fills_cash_and_shares():
    from builder.build import _history, _load_docs

    docs = _load_docs("NVDA")
    points = {row["period"]: row for row in _history(docs)}
    q3 = points["FY2027Q3"]
    assert q3["revenue"] == 115_844_000_000
    assert q3["eps"] == 2.7
    assert q3["eps_nongaap"] == 2.68
    assert q3["capex"] == 4_422_000_000
    assert q3["fcf"] == 43_831_000_000
    assert q3["capex_qoq"] == pytest.approx(4_422 / 2_677 - 1)
    assert q3["fcf_qoq"] == pytest.approx(43_831 / 21_400 - 1)
    assert points["FY2027Q2"]["capex"] == 2_677_000_000
    assert points["FY2026Q3"]["capex"] == 1_636_000_000
    assert [points[f"FY2026Q{i}"]["capex"] for i in range(1, 5)] == [
        1_227_000_000,
        1_895_000_000,
        1_636_000_000,
        1_284_000_000,
    ]
    assert points["FY2023Q1"]["capex"] is None
    assert points["FY2016Q4"]["capex"] is None
    assert q3["share_count"] == 24_186_000_000
    assert points["FY2027Q2"]["share_count"] == 24_285_000_000
    assert points["FY2026Q3"]["share_count"] == 24_483_000_000
    assert points["FY2021Q1"]["share_count"] == 622_000_000 * 40
    assert points["FY2026Q4"]["share_count"] is None
    filled = lambda key: sum(row.get(key) is not None for row in points.values())
    assert filled("capex") == 38
    assert filled("fcf") == 38
    assert filled("share_count") == 36
    assert filled("ocf") == 48


def test_history_fills_missing_qoq_from_neighbor():
    docs = {
        "FY2026Q2": {"financials": {"revenue": {"value": 100.0, "yoy_pct": None, "qoq_pct": None}}},
        "FY2026Q3": {"financials": {"revenue": {"value": 110.0, "yoy_pct": 0.1, "qoq_pct": None}}},
    }
    points = {p["period"]: p for p in _history(docs)}
    assert points["FY2026Q3"]["revenue_yoy"] == 0.1
    assert abs(points["FY2026Q3"]["revenue_qoq"] - 0.1) < 1e-9
