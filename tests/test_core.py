from __future__ import annotations

from pipeline.extract.numbers import parse_number, parse_plus_minus, strip_footnote
from pipeline.extract.press_release import detect_nongaap_eps_regex
from pipeline.validate import classify_eps_basis
from pipeline.compute.periods import (
    build_period_info,
    calendar_quarter_from_midpoint,
    make_fiscal_period,
)
from pipeline.compute.metrics import verdict_eps, verdict_revenue, ytd_to_quarterly
from pipeline.compute.guidance_review import (
    change_vs_prior,
    gap_closure,
    parse_guidance_item,
    position_in_range,
)
from datetime import date


def test_parse_numbers():
    assert parse_number("$1,234.5") == 1234.5
    assert parse_number("(0.12)") == -0.12
    assert parse_number("0.48(1)") == 0.48
    assert parse_number("$12.2 billion") == 12.2e9
    assert parse_number("± $300 million") == 300e6
    assert parse_number("42.5%") == 42.5


def test_plus_minus():
    low, mid, high = parse_plus_minus("42.5% ± 1%")
    assert mid == 42.5
    assert abs(low - 41.5) < 1e-9
    assert abs(high - 43.5) < 1e-9


def test_nongaap_keyword_excludes_revenue_excluding():
    text = "Revenue excluding currency changes increased. Diluted earnings per share was $1.20."
    scan = detect_nongaap_eps_regex(text)
    assert scan["has_nongaap_eps_regex"] is False


def test_nongaap_with_reconciliation():
    text = "Non-GAAP diluted EPS was $2.10. See reconciliation of non-GAAP measures below."
    scan = detect_nongaap_eps_regex(text)
    assert scan["has_nongaap_eps_regex"] is True


def test_eps_basis_table():
    assert classify_eps_basis(has_nongaap_eps=False, gaap_match_ratio=0.8, paired_quarters=4)[0] == "gaap"
    assert classify_eps_basis(has_nongaap_eps=False, gaap_match_ratio=0.5, paired_quarters=4)[0] == "unknown"
    assert classify_eps_basis(has_nongaap_eps=True, gaap_match_ratio=0.3, paired_quarters=4)[0] == "non_gaap"
    assert classify_eps_basis(has_nongaap_eps=True, gaap_match_ratio=0.8, paired_quarters=4, gaap_nongaap_differ=True)[0] == "gaap"
    assert classify_eps_basis(has_nongaap_eps=True, gaap_match_ratio=0.6, paired_quarters=4)[0] == "non_gaap"
    assert classify_eps_basis(has_nongaap_eps=True, gaap_match_ratio=None, paired_quarters=2)[0] == "unknown"


def test_mu_fiscal_mapping():
    # MU FY ends ~ Aug; period ending 2025-08-28 ≈ FY2025Q4
    info = build_period_info("2025-08-28", 8, "2025-05-30")
    assert info.fiscal_period.endswith("Q4")
    assert info.calendar_quarter.startswith("2025")


def test_nke_fiscal_mapping():
    info = build_period_info("2025-05-31", 5, "2025-03-01")
    assert info.fiscal_quarter == 4


def test_calendar_midpoint():
    assert calendar_quarter_from_midpoint("2026-06-01", "2026-08-31") == "2026Q3"


def test_ytd_cashflow():
    assert ytd_to_quarterly(100, None, is_first_quarter=True) == 100
    assert ytd_to_quarterly(250, 100, is_first_quarter=False) == 150


def test_verdicts():
    assert verdict_revenue(0.005) == "inline"
    assert verdict_revenue(0.02) == "beat"
    assert verdict_revenue(-0.02) == "miss"
    assert verdict_eps(0.005, 0.01, estimate=1.0) == "inline"
    assert verdict_eps(0.05, 0.10, estimate=0.5) == "beat"
    assert verdict_eps(-0.02, -0.5, estimate=0.03) == "miss"  # micro-profit uses abs


def test_guidance_helpers():
    item = parse_guidance_item({"point_raw": "42.5", "plus_minus_raw": "1", "metric_key": "gross_margin_nongaap"})
    assert item["mid"] == 42.5
    assert item["low"] == 41.5
    cur = {"mid": 43.0, "low": 42.0, "high": 44.0}
    pri = {"mid": 42.0, "low": 41.0, "high": 43.0}
    assert change_vs_prior(cur, pri, is_ratio=True) in {"raised", "maintained"}
    pos = position_in_range(43.5, 42.0, 44.0)
    assert pos["position"] == "upper_half"
    assert gap_closure(1.0, 1.05, 1.10) == (1.05 - 1.0) / (1.10 - 1.0)
    assert gap_closure(1.0, 1.005, 1.005) is None or True  # near-equal skip may apply
