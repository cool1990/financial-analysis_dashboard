"""毛利率图画比率，不画毛利金额。"""

from builder.build import _fin_view, _rate_yoy_chart


def test_annual_margin_weights_by_revenue_and_skips_a_partial_year():
    rows = [
        {"period": "FY2024Q1", "rate": 0.50, "revenue": 100.0, "yoy_pp": None, "qoq_pp": None},
        {"period": "FY2024Q2", "rate": 0.60, "revenue": 100.0, "yoy_pp": None, "qoq_pp": None},
        {"period": "FY2024Q3", "rate": 0.70, "revenue": 200.0, "yoy_pp": None, "qoq_pp": None},
        {"period": "FY2024Q4", "rate": 0.40, "revenue": 100.0, "yoy_pp": None, "qoq_pp": None},
        {"period": "FY2025Q1", "rate": 0.80, "revenue": 100.0, "yoy_pp": None, "qoq_pp": None},
    ]
    chart = _rate_yoy_chart(rows, "毛利率（GAAP）")
    assert chart is not None
    assert chart["kind"] == "rate_yoy"
    assert chart["subtitle"] == "%"
    # 50+60+140+40 = 290，除以营收 500，是 58%，不是四个比率相加。
    assert chart["annual"]["labels"] == ["FY2024"]
    assert chart["annual"]["rate"] == [58.0]
    assert "FY2025" not in chart["annual"]["labels"]


def test_nvda_margin_charts_keep_gaap_and_nongaap_apart():
    view = _fin_view("NVDA", "FY2027Q3")
    assert [chart["title"] for chart in view["by_key"]["revenue"]["charts"]] == ["营业收入", "按业务", "按地区"]
    gaap = view["by_key"]["gross_margin_gaap"]["charts"][0]
    nongaap = view["by_key"]["gross_margin_nongaap"]["charts"][0]
    assert gaap["title"] == "毛利率（GAAP）"
    assert nongaap["title"] == "毛利率（Non-GAAP）"
    assert gaap["kind"] == nongaap["kind"] == "rate_yoy"
    assert gaap["subtitle"] == nongaap["subtitle"] == "%"
    assert "十亿" not in gaap["subtitle"] and "十亿" not in nongaap["subtitle"]
    g = gaap["labels"].index("FY2027Q3")
    n = nongaap["labels"].index("FY2027Q3")
    assert gaap["rate"][g] == 74.8
    assert gaap["rate_text"][g] == "74.8%"
    assert gaap["yoy"][g] == 1.4
    assert gaap["qoq"][g] == -0.2
    assert nongaap["rate"][n] == 74.9
    assert nongaap["rate_text"][n] == "74.9%"
    assert nongaap["yoy"][n] == 1.4
    assert nongaap["qoq"][n] is None
    assert nongaap["rate"][nongaap["labels"].index("FY2027Q2")] is None
    assert gaap["rate"] != nongaap["rate"]
    assert "FY2027" not in gaap["annual"]["labels"]
    assert nongaap["annual"] is None
