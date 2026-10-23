"""用 FY*.json 组装 statement_history，不联网。"""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.sources.period_history import build_histories_from_periods, write_period_histories


def test_build_from_periods_fills_revenue_and_leaves_geo_empty(tmp_path: Path):
    folder = tmp_path / "AMZN"
    folder.mkdir()
    (folder / "FY2025Q1.json").write_text(
        json.dumps(
            {
                "meta": {"fiscal_period": "FY2025Q1", "period_end": "2025-03-31"},
                "financials": {
                    "revenue": {"value": 100_000_000_000},
                    "gross_margin_gaap": {"value": 0.5},
                    "kpis": {},
                },
            }
        ),
        encoding="utf-8",
    )
    (folder / "FY2025Q2.json").write_text(
        json.dumps(
            {
                "meta": {"fiscal_period": "FY2025Q2", "period_end": "2025-06-30"},
                "financials": {
                    "revenue": {"value": 110_000_000_000},
                    "gross_profit_gaap": {"value": 55_000_000_000},
                    "kpis": {
                        "aws_revenue": {"raw": "30,000", "unit": "millions"},
                        "advertising_revenue": {"raw": "20,000", "unit": "millions"},
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    # gap quarter file missing → span still includes Q? only Q1-Q2 present so span is Q1-Q2
    statement, segment = build_histories_from_periods(folder, ticker="AMZN")
    by = {row["period"]: row for row in statement["quarters"]}
    assert by["FY2025Q1"]["revenue"] == 100_000_000_000
    assert by["FY2025Q1"]["cost_of_revenue"] is None
    assert by["FY2025Q1"]["gross_profit"] == 50_000_000_000
    assert by["FY2025Q1"]["geo"] == []
    assert by["FY2025Q2"]["gross_profit"] == 55_000_000_000
    lines = {row["period"]: row["lines"] for row in segment["quarters"]}
    assert lines["FY2025Q1"] == []
    assert [line["key"] for line in lines["FY2025Q2"]] == ["aws", "marketing"]


def test_write_period_histories_roundtrip(tmp_path: Path):
    folder = tmp_path / "HOOD"
    folder.mkdir()
    (folder / "FY2026Q1.json").write_text(
        json.dumps(
            {
                "meta": {"period_end": "2026-03-31"},
                "financials": {"revenue": {"value": 1_000_000_000}, "kpis": {}},
            }
        ),
        encoding="utf-8",
    )
    statement_path, segment_path = write_period_histories(folder, ticker="HOOD")
    saved = json.loads(statement_path.read_text(encoding="utf-8"))
    assert saved["quarters"][0]["revenue"] == 1_000_000_000
    assert segment_path is not None
    assert json.loads(segment_path.read_text(encoding="utf-8"))["quarters"][0]["lines"] == []


def test_amzn_repo_history_starts_at_fy2016q1():
    root = Path(__file__).resolve().parents[1]
    statement = json.loads((root / "data" / "AMZN" / "statement_history.json").read_text(encoding="utf-8"))
    assert statement["quarters"][0]["period"] == "FY2016Q1"
    assert statement["quarters"][-1]["period"] == "FY2026Q2"
    # SEC 重建没有地区事实时 geo 为空，不补地区。
    assert all(not q.get("geo") for q in statement["quarters"])
    assert sum(1 for q in statement["quarters"] if q.get("revenue") is not None) >= 40
