from __future__ import annotations

import ast
from pathlib import Path

from pipeline.compute.periods import build_period_info
from pipeline.compute.revisions import align_consensus_to_fiscal_period, compute_analyst_revisions
from pipeline.extract.guidance import parse_guidance_item
from pipeline.extract.numbers import parse_number, parse_plus_minus
from pipeline.extract.press_release import detect_nongaap_eps_regex
from pipeline.schemas import validate_period_doc
from pipeline.state import empty_period_doc, save_period_json
from pipeline.validate import classify_eps_basis
from pipeline.compute.metrics import verdict_eps, verdict_revenue, ytd_to_quarterly, build_metric_block
from pipeline.compute.guidance_review import change_vs_prior, gap_closure, position_in_range
from datetime import date
import json


ROOT = Path(__file__).resolve().parents[1]


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


def test_eps_basis_table_no_nongaap_high_match():
    assert classify_eps_basis(has_nongaap_eps=False, gaap_match_ratio=0.8, paired_quarters=4)[0] == "gaap"


def test_eps_basis_table_no_nongaap_low_match():
    assert classify_eps_basis(has_nongaap_eps=False, gaap_match_ratio=0.5, paired_quarters=4)[0] == "unknown"


def test_eps_basis_table_nongaap_low_gaap_match():
    assert classify_eps_basis(has_nongaap_eps=True, gaap_match_ratio=0.3, paired_quarters=4)[0] == "non_gaap"


def test_eps_basis_table_nongaap_but_provider_uses_gaap():
    assert (
        classify_eps_basis(
            has_nongaap_eps=True, gaap_match_ratio=0.8, paired_quarters=4, gaap_nongaap_differ=True
        )[0]
        == "gaap"
    )


def test_eps_basis_table_nongaap_default_other():
    assert classify_eps_basis(has_nongaap_eps=True, gaap_match_ratio=0.6, paired_quarters=4)[0] == "non_gaap"


def test_eps_basis_table_insufficient_pairs():
    assert classify_eps_basis(has_nongaap_eps=True, gaap_match_ratio=None, paired_quarters=2)[0] == "unknown"


def test_mu_fiscal_mapping():
    info = build_period_info("2025-08-28", 8, "2025-05-30")
    assert info.fiscal_period.endswith("Q4")
    assert info.calendar_quarter.startswith("2025")


def test_nke_fiscal_mapping():
    info = build_period_info("2025-05-31", 5, "2025-03-01")
    assert info.fiscal_quarter == 4


def test_calendar_year_company_fiscal_mapping():
    """自然年公司（FYE=12）：日历 Q2 结束日 → 财年同季。"""
    info = build_period_info("2024-06-30", 12, "2024-04-01")
    assert info.fiscal_year == 2024
    assert info.fiscal_quarter == 2
    assert info.fiscal_period == "FY2024Q2"
    info_q4 = build_period_info("2024-12-31", 12, "2024-10-01")
    assert info_q4.fiscal_period == "FY2024Q4"


def test_ytd_cashflow():
    assert ytd_to_quarterly(100, None, is_first_quarter=True) == 100
    assert ytd_to_quarterly(250, 100, is_first_quarter=False) == 150


def test_verdicts():
    assert verdict_revenue(0.005) == "inline"
    assert verdict_revenue(0.02) == "beat"
    assert verdict_revenue(-0.02) == "miss"
    assert verdict_eps(0.005, 0.01, estimate=1.0) == "inline"
    assert verdict_eps(0.05, 0.10, estimate=0.5) == "beat"
    assert verdict_eps(-0.02, -0.5, estimate=0.03) == "miss"


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


def test_metric_block_units_split():
    amount = build_metric_block(110.0, yoy_base=100.0, qoq_base=105.0, benchmark=108.0, benchmark_source="consensus")
    assert amount["yoy_pct"] is not None and amount["yoy_pp"] is None
    assert amount["benchmark"]["diff_pct"] is not None and amount["benchmark"]["diff_pp"] is None
    ratio = build_metric_block(0.45, yoy_base=0.40, qoq_base=0.44, benchmark=0.43, benchmark_source="prior_guidance_mid", is_ratio=True)
    assert ratio["yoy_pp"] is not None and ratio["yoy_pct"] is None
    assert ratio["benchmark"]["diff_pp"] is not None and ratio["benchmark"]["diff_pct"] is None


def test_consensus_rolling_alignment():
    """财报前 +1q 与财报后 0q 对齐到同一财季。"""
    aligned = align_consensus_to_fiscal_period(
        target_fiscal_period="FY2026Q1",
        pre_release_plus1q_eps={"avg": 3.35},
        pre_release_plus1q_rev={"avg": 12.1e9},
        post_release_0q_eps={"avg": 3.40},
        post_release_0q_rev={"avg": 12.2e9},
    )
    assert aligned["fiscal_period"] == "FY2026Q1"
    assert aligned["pre_earnings"]["row"] == "+1q"
    assert aligned["post_earnings"]["row"] == "0q"
    assert aligned["pre_earnings"]["eps"] == 3.35
    assert aligned["post_earnings"]["eps"] == 3.40
    assert aligned["aligned"] is True


def test_revisions_accepts_points_not_snapshots():
    out = compute_analyst_revisions(
        {
            "t_minus_1": {"eps": 1.0, "revenue": 100.0, "date": "2026-09-20"},
            "t_plus_1": {"eps": 1.05, "revenue": 101.0, "date": "2026-09-24"},
            "t_plus_3": {"eps": 1.08, "revenue": 102.0, "date": "2026-09-26"},
            "t_plus_7": {"eps": 1.10, "revenue": 103.0, "date": "2026-09-30"},
        },
        guidance_mid_eps=1.12,
        guidance_mid_rev=105.0,
    )
    assert out["t_plus_7"]["eps_chg"] == (1.10 - 1.0) / 1.0
    assert out["gap_closure"]["eps"] is not None


def test_period_doc_schema_and_save(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    # redirect data dir via monkeypatch of data_dir
    from pipeline import state as state_mod
    from pipeline import config as config_mod

    monkeypatch.setattr(config_mod, "data_dir", lambda ticker=None: tmp_path / (ticker or ""))
    monkeypatch.setattr(state_mod, "data_dir", lambda ticker=None: tmp_path / (ticker or ""))

    doc = empty_period_doc("MU", "FY2025Q4", calendar_quarter="2025Q3", eps_basis="non_gaap")
    assert doc["meta"]["schema_version"] == 1
    path = save_period_json("MU", "FY2025Q4", doc)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["meta"]["schema_version"] == 1
    validate_period_doc(loaded)


def test_mock_fixture_validates():
    path = ROOT / "tests" / "fixtures" / "mu_fy2025q4_mock.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "mock" in " ".join(data["status"]["warnings"]).lower() or "mock" in path.name
    doc = validate_period_doc(data)
    assert doc.meta.schema_version == 1
    assert doc.scorecard[0].benchmark.source in {"consensus", "none", "prior_guidance_mid"}


def _forbidden_imports(package_dir: Path, forbidden_prefixes: tuple[str, ...]) -> list[str]:
    offenders: list[str] = []
    for path in package_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            mod = None
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
            if not mod:
                continue
            if any(mod == p or mod.startswith(p + ".") for p in forbidden_prefixes):
                offenders.append(f"{path.relative_to(ROOT)}: {mod}")
    return offenders


def test_sources_and_extract_do_not_import_upper_layers():
    forbidden = ("pipeline.compute", "pipeline.analyze", "builder")
    offenders = []
    offenders += _forbidden_imports(ROOT / "pipeline" / "sources", forbidden)
    offenders += _forbidden_imports(ROOT / "pipeline" / "extract", forbidden)
    assert offenders == [], "禁止向上依赖:\n" + "\n".join(offenders)
