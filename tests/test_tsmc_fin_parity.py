"""TSM 财报分析 / 盈利预期与 META 同一套图。数字只来自幻灯片白字，不回退 META。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

META_REVENUE_NOTE = (
    "营收、营业成本、研发、销售、管理费用来自 SEC companyfacts 的单季 us-gaap。"
    "毛利为营收减营业成本，申报没有单独的毛利科目，也没有把营业成本再拆开。"
    "地区收入来自 10-Q/10-K XBRL 的地区轴。第四季没有单季事实时用全年减前九个月；地区口径对不上时留空。"
    " 2016 年地区是美国与美国以外，2017 起改为美国和加拿大、欧洲、亚太、世界其他地区。"
    " FY2019Q4、FY2020Q4 没有完整的业务分部，柱上留空。"
    " 业务柱只计广告、其他和 Reality Labs；应用家族是母项，不重复计入。"
)


def _load(ticker: str, period: str) -> dict:
    return json.loads((ROOT / "data" / ticker / f"{period}.json").read_text(encoding="utf-8"))


def test_tsmc_slide_history_keeps_text_platform_and_adr_mapping():
    from pipeline.sources.tsmc_slides import build_tsmc_histories

    raw = sorted((ROOT / "data" / "TSM" / "raw").glob("FY*/presentation.html"))
    statement, segment = build_tsmc_histories(raw, cik="0001046179")
    saved_statement = json.loads((ROOT / "data" / "TSM" / "statement_history.json").read_text(encoding="utf-8"))
    saved_segment = json.loads((ROOT / "data" / "TSM" / "segment_history.json").read_text(encoding="utf-8"))
    assert saved_statement["quarters"] == statement["quarters"]
    assert saved_segment["quarters"] == segment["quarters"]
    assert saved_statement["skipped_slides"] == ["FY2025Q2"]
    assert "FY2026Q3" in saved_statement["parsed_slides"]

    by_period = {row["period"]: row for row in statement["quarters"]}
    q3 = by_period["FY2026Q3"]
    assert q3["revenue"] == 46_720_000_000
    assert q3["fx_usd_ntd"] == pytest.approx(31.98)
    assert q3["eps_ordinary_ntd"] == pytest.approx(29.43)
    assert q3["eps_adr_usd"] == pytest.approx(4.60)
    assert q3["eps_adr_method"] == "press_release"
    assert by_period["FY2025Q4"]["eps_adr_usd"] == pytest.approx(3.14)
    assert by_period["FY2025Q4"]["eps_adr_method"] == "press_release"
    assert by_period["FY2024Q1"].get("eps_ordinary_ntd") is not None
    assert "eps_adr_usd" not in by_period["FY2024Q1"]
    assert "不按当季平均汇率把新台币换算成美元" in statement["source"]
    assert statement["adr"]["eps_method"] == "press_release_us_per_adr"
    assert "US$ per ADR" in statement["source"]
    assert q3["geo"] == []
    assert q3["observed_on"] == {"slide": "FY2026Q3", "column": "current"}
    assert by_period["FY2024Q4"]["eps_adr_usd"] == pytest.approx(2.24)
    assert by_period["FY2025Q1"]["eps_adr_usd"] == pytest.approx(2.12)
    assert by_period["FY2026Q1"]["eps_adr_usd"] == pytest.approx(3.49)
    assert by_period["FY2026Q2"]["eps_adr_usd"] == pytest.approx(4.31)
    assert by_period["FY2025Q2"]["revenue"] == 30_070_000_000
    assert by_period["FY2025Q2"]["observed_on"]["column"] != "current"
    assert by_period["FY2024Q2"]["revenue"] is None
    assert all(row["geo"] == [] for row in statement["quarters"])
    assert "1 ADR = 5" in statement["source"]
    assert "不使用图像识别" in statement["source"]

    lines = {row["period"]: row["lines"] for row in segment["quarters"]}
    assert lines["FY2026Q3"] == []
    assert lines["FY2026Q2"] == []
    assert lines["FY2025Q3"] == []
    q4 = {line["key"]: line for line in lines["FY2024Q4"]}
    assert set(q4) == {"hpc", "smartphone", "iot", "auto", "dce", "other"}
    assert q4["hpc"]["share_pct"] == 53
    assert q4["hpc"]["value"] == round(26_880_000_000 * 0.53)
    assert abs(sum(line["share_pct"] for line in lines["FY2024Q4"]) - 100) < 0.2
    assert {line["key"]: line["share_pct"] for line in lines["FY2026Q1"]}["hpc"] == 61
    assert all("3nm" not in line["key"] and "7nm" not in line["name"] for row in lines.values() for line in row)


def test_fin_view_keeps_meta_copy_and_gives_tsm_three_revenue_charts():
    from builder.build import _fin_view

    meta = _fin_view("META", "FY2026Q3")
    assert meta["by_key"]["revenue"]["note"] == META_REVENUE_NOTE
    assert [chart["title"] for chart in meta["by_key"]["revenue"]["charts"]] == ["营业收入", "按业务", "按地区"]
    assert [chart["title"] for chart in meta["by_key"]["opex"]["charts"]] == ["营业费用", "费用构成"]
    assert [chart["title"] for chart in meta["by_key"]["gross_margin_gaap"]["charts"]] == ["毛利"]
    assert "companyfacts" in meta["by_key"]["cost_of_revenue"]["note"]

    tsm = _fin_view("TSM", "FY2026Q3")
    charts = tsm["by_key"]["revenue"]["charts"]
    assert [chart["title"] for chart in charts] == ["营业收入", "按业务", "按地区"]
    assert charts[0]["amount"][-1] == 46.72
    assert "FY2025" in charts[0]["annual"]["labels"]
    assert "FY2024" not in charts[0]["annual"]["labels"]
    assert "FY2026" not in charts[0]["annual"]["labels"]
    assert charts[0]["annual"]["amount"][charts[0]["annual"]["labels"].index("FY2025")] == 122.43
    business = {point["period"]: point for point in charts[1]["points"]}
    assert business["FY2026Q3"]["gap"] is True
    assert business["FY2026Q1"]["gap"] is False
    assert abs(sum(item["share"] for item in business["FY2026Q1"]["items"]) - 100) < 0.2
    assert all(point["gap"] for point in charts[2]["points"])
    note = tsm["by_key"]["revenue"]["note"]
    assert "Reality Labs" not in note and "2016 年地区" not in note
    assert "留空" in note and "不补数" in note
    assert [chart["title"] for chart in tsm["by_key"]["opex"]["charts"]] == ["营业费用"]
    assert "没有再拆" in tsm["by_key"]["opex"]["note"]
    assert [chart["title"] for chart in tsm["by_key"]["cost_of_revenue"]["charts"]] == ["营业成本"]
    assert "companyfacts" not in tsm["by_key"]["cost_of_revenue"]["note"]
    assert "减去毛利" in tsm["by_key"]["cost_of_revenue"]["note"]

    mu = _fin_view("MU", "FY2026Q4")
    assert "revenue" not in mu["by_key"]


def test_expect_chart_uses_meta_controls_without_fake_consensus():
    from builder.build import _eps_chart, _load_docs

    meta = _eps_chart(_load("META", "FY2026Q3"))
    assert meta is not None
    assert [row["label"] for row in meta["quarters"]][-1] >= "FQ3 2027"
    assert "mark" not in meta

    tsm_doc = _load("TSM", "FY2026Q3")
    chart = _eps_chart(tsm_doc, _load_docs("TSM"))
    assert chart is not None and chart["revenue"]
    assert chart.get("mark") != "NT$"
    assert "4.60" in chart["note"] and "per ADR" in chart["note"]
    assert "平均汇率" in chart["note"] and "不是把新台币" in chart["note"]
    # tv-scan 写入后允许有 TradingView 一致预期；仍不得把新台币假扮成美元共识
    current = next(row for row in chart["quarters"] if row["label"] == "FQ3 2026")
    assert current["actual"] == 4.60
    cleared = next(row for row in chart["quarters"] if row["label"] == "FQ4 2025")
    assert cleared["actual"] == 3.14
    revenue = chart["revenue"]["quarters"]
    assert next(row for row in revenue if row["label"] == "FQ3 2026")["actual"] == 46.72
    guide = next(row for row in revenue if row["label"] == "FQ4 2026")
    assert guide["actual"] is None
    # 有 TV 营收共识时画灰柱；没有时仍只显示管理层指引中值
    if guide["consensus"] is None:
        assert guide["note"] == "指引中值 $53.30B"
        assert "缺一致预期" in chart["revenue"]["note"]
    else:
        assert "TradingView scanner" in chart["revenue"]["note"] or "一致预期" in chart["revenue"]["note"]

    mu = _eps_chart(_load("MU", "FY2026Q4"), _load_docs("MU"))
    assert mu is not None
    reported = next(row for row in mu["quarters"] if row["label"] == "FQ4 2026")
    assert reported["actual"] == 33.42 and reported["consensus"] == 32.46
    assert reported["note"] == "超预期 $0.96"
    assert "一致预期" in mu["revenue"]["note"]
