from datetime import date, timedelta

from builder.build import _history
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


def test_history_fills_missing_qoq_from_neighbor():
    docs = {
        "FY2026Q2": {"financials": {"revenue": {"value": 100.0, "yoy_pct": None, "qoq_pct": None}}},
        "FY2026Q3": {"financials": {"revenue": {"value": 110.0, "yoy_pct": 0.1, "qoq_pct": None}}},
    }
    points = {p["period"]: p for p in _history(docs)}
    assert points["FY2026Q3"]["revenue_yoy"] == 0.1
    assert abs(points["FY2026Q3"]["revenue_qoq"] - 0.1) < 1e-9
