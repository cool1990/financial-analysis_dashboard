from datetime import date

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


def test_history_fills_missing_qoq_from_neighbor():
    docs = {
        "FY2026Q2": {"financials": {"revenue": {"value": 100.0, "yoy_pct": None, "qoq_pct": None}}},
        "FY2026Q3": {"financials": {"revenue": {"value": 110.0, "yoy_pct": 0.1, "qoq_pct": None}}},
    }
    points = {p["period"]: p for p in _history(docs)}
    assert points["FY2026Q3"]["revenue_yoy"] == 0.1
    assert abs(points["FY2026Q3"]["revenue_qoq"] - 0.1) < 1e-9
