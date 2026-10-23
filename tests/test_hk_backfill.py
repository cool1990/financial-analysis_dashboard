"""0700.HK 历史季：列头日期、调节表最后一列、三个月而不是九个月、残差只在跨度为一个季度时相减。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from pipeline.extract.hk_residual import (
    FLOW_KEYS,
    is_one_quarter_gap,
    residual_flows,
    residual_from_quarter_sum,
)
from pipeline.extract.hk_statement import (
    HkCumulativeOnly,
    _dates_in,
    infer_hk_filing_period,
    parse_hk_quarter,
)


def test_filename_maps_quarter_interim_and_annual():
    assert infer_hk_filing_period("腾讯2017年一季报.pdf") == "FY2017Q1"
    assert infer_hk_filing_period("腾讯2019年三季报.pdf") == "FY2019Q3"
    assert infer_hk_filing_period("腾讯控股_2017_中报.pdf") == "FY2017Q2"
    assert infer_hk_filing_period("腾讯控股_2018_年报.pdf") == "FY2018Q4"
    assert infer_hk_filing_period("FY2019Q2.pdf") == "FY2019Q2"


def test_column_years_are_not_taken_from_the_section_title():
    header = "\n".join([
        "截至二零一七年十二月三十一日止年度的所得税开支同比增长。",
        "二零一七年第四季与二零一六年第四季的比较",
        "未经审核",
        "截至下列日期止三个月",
        "二零一七年",
        "二零一六年",
        "十二月三十一日",
        "十二月三十一日",
        "(人民币百万元)",
    ])
    assert _dates_in(header)[:2] == [date(2017, 12, 31), date(2016, 12, 31)]


def test_highlight_header_keeps_three_quarter_ends_in_order():
    header = "\n".join([
        "截至下列日期止三个月",
        "二零一七年",
        "三月三十一日",
        "二零一六年",
        "三月三十一日",
        "同比变动",
        "二零一六年",
        "十二月三十一日",
        "环比变动",
    ])
    assert _dates_in(header)[:3] == [date(2017, 3, 31), date(2016, 3, 31), date(2016, 12, 31)]


def test_reconciliation_last_column_is_operating_profit_not_eps():
    text = """
未经审核
截至下列日期止三个月
二零一七年
二零一六年
十二月三十一日
十二月三十一日
(人民币百万元)
收入 66,392 43,864
收入成本 34,897 22,000
毛利 31,495 21,864
经营盈利 25,724 13,930
期内盈利 21,622 10,000
二零一七年第四季与二零一七年第三季的比较
未经审核
截至下列日期止三个月
二零一七年
二零一七年
十二月三十一日
九月三十日
(人民币百万元)
收入 66,392 65,210
收入成本 34,897 33,529
毛利 31,495 31,681
经营盈利 25,724 22,746
期内盈利 21,622 18,047
股份酬金
摊销
减值拨备
截至二零一七年十二月三十一日止三个月
经营盈利 25,724 22,746 13,930 90,302 56,117
经营盈利 25,724 1,874 (6,281) 112 424 21,853
每股盈利(每股人民币元)
－基本 2.206 1.852
－摊薄 2.177 1.827
"""
    parsed = parse_hk_quarter(text, "FY2017Q4")
    rows = parsed["rows"]
    assert rows["revenue"]["raw"] == "66,392"
    assert rows["revenue"]["year_ago_raw"] == "43,864"
    assert rows["revenue"]["prior_quarter_raw"] == "65,210"
    assert rows["non_ifrs_operating_profit"]["raw"] == "21,853"
    assert rows["eps_gaap"]["raw"] == "2.177"
    assert rows["eps_nongaap"]["raw"] == "1.827"
    assert rows["non_ifrs_operating_profit"]["raw"] != rows["eps_nongaap"]["raw"]


def test_quarterly_release_bridge_ignores_the_nongaap_profit_row_above_eps():
    text = """
每股基本盈利为人民币1.912 元,每股摊薄盈利为人民币1.888 元。
按非通用会计准则,已排除若干非现金项目及并购交易的若干影响：
－ 每股摊薄盈利为人民币1.790 元。
综合收益表
未经审核
3Q2017 3Q2016 3Q2017 2Q2017
收入 65,210 40,388 65,210 56,606
收入成本 33,529 20,000 33,529 28,300
毛利 31,681 20,388 31,681 28,306
经营盈利 22,746 14,460 22,746 22,560
期内盈利 18,047 10,776 18,047 18,254
非通用会计准则本公司权益持有人应占盈利 17,070 11,737 17,070 16,391
每股盈利(每股人民币元)
- 摊薄 1.888 1.121 1.888 1.914
股份酬金
摊销
截至 2017 年 9 月 30 日三个月未经审核
经营盈利 22,746 1,632 (3,169) 110 295 21,614
"""
    parsed = parse_hk_quarter(text, "FY2017Q3")
    rows = parsed["rows"]
    assert rows["operating_income"]["raw"] == "22,746"
    assert rows["non_ifrs_operating_profit"]["raw"] == "21,614"
    assert rows["eps_gaap"]["raw"] == "1.888"
    assert rows["eps_nongaap"]["raw"] == "1.790"


def test_nine_month_diluted_eps_is_not_the_quarter():
    text = """
未经审核
截至下列日期止三个月
二零一九年
九月三十日
二零一八年
九月三十日
同比变动
二零一九年
六月三十日
环比变动
(人民币百万元,另有指明者除外)
收入 97,236 80,595 21% 88,821 9%
毛利 42,479 35,480 20% 39,126 9%
经营盈利 25,827 27,861 27,521
期内盈利 20,976 23,405 24,684
每股盈利(每股人民币元)
－摊薄 2.127 2.440 2.520
非国际财务报告准则
每股盈利(每股人民币元)
－摊薄 2.548 2.061 2.456
未经审核
截至下列日期止九个月
二零一九年
九月三十日
收入 271,522 227,798
毛利 121,425 106,968
经营盈利 90,090 80,360
期内盈利 73,516 65,958
非国际财务报告准则每股盈利
－摊薄 7.188 6.035
"""
    parsed = parse_hk_quarter(text, "FY2019Q3")
    assert parsed["rows"]["revenue"]["raw"] == "97,236"
    assert parsed["rows"]["eps_gaap"]["raw"] == "2.127"
    assert parsed["rows"]["eps_nongaap"]["raw"] == "2.548"


def test_half_year_without_a_quarter_column_stays_cumulative():
    text = (
        "腾讯公布二零二六年中期业绩\n"
        "截至二零二六年六月三十日止六个月\n"
        "简明综合收益表\n"
        "收入 400,000 360,000\n"
        "毛利 200,000 180,000\n"
        "期内盈利 80,000 70,000\n"
    )
    with pytest.raises(HkCumulativeOnly, match="六个月"):
        parse_hk_quarter(text, "FY2026Q2")


def test_residual_only_when_the_gap_is_one_quarter():
    start = date(2017, 3, 31)
    assert is_one_quarter_gap(start + timedelta(days=80), start)
    assert is_one_quarter_gap(start + timedelta(days=100), start)
    assert not is_one_quarter_gap(start + timedelta(days=79), start)
    assert not is_one_quarter_gap(start + timedelta(days=101), start)
    assert not is_one_quarter_gap(start + timedelta(days=184), start)
    assert "eps_gaap" not in FLOW_KEYS
    assert "eps_nongaap" not in FLOW_KEYS

    later = {"revenue": 100.0, "net_income": 40.0, "eps_gaap": 9.0}
    earlier = {"revenue": 40.0, "net_income": 10.0, "eps_gaap": 3.0}
    got = residual_flows(
        later,
        earlier,
        later_end=date(2017, 6, 30),
        earlier_end=date(2017, 3, 31),
        later_start=date(2017, 1, 1),
        earlier_start=date(2017, 1, 1),
    )
    assert got == {"revenue": 60.0, "net_income": 30.0}
    assert residual_flows(
        later,
        earlier,
        later_end=date(2017, 6, 30),
        earlier_end=date(2017, 3, 31),
        later_start=date(2017, 1, 1),
        earlier_start=date(2017, 4, 1),
    ) is None
    skipped = residual_flows(
        {"revenue": 100.0},
        {"revenue": 40.0, "net_income": 10.0},
        later_end=date(2017, 6, 30),
        earlier_end=date(2017, 3, 31),
        later_start=date(2017, 1, 1),
        earlier_start=date(2017, 1, 1),
    )
    assert skipped == {"revenue": 60.0}


def test_q4_residual_is_blank_when_any_quarter_is_missing():
    annual = {"revenue": 100.0, "net_income": 10.0}
    quarters = [
        (date(2017, 3, 31), {"revenue": 10.0, "net_income": 1.0}),
        (date(2017, 6, 30), {"revenue": 20.0, "net_income": 2.0}),
        (date(2017, 9, 30), {"revenue": 30.0, "net_income": 3.0}),
    ]
    got = residual_from_quarter_sum(
        annual,
        quarters,
        year_start=date(2017, 1, 1),
        year_end=date(2017, 12, 31),
    )
    assert got == {"revenue": 40.0, "net_income": 4.0}
    assert residual_from_quarter_sum(
        annual,
        quarters[:2],
        year_start=date(2017, 1, 1),
        year_end=date(2017, 12, 31),
    ) is None
    partial = residual_from_quarter_sum(
        annual,
        [
            quarters[0],
            (date(2017, 6, 30), {"revenue": 20.0}),
            quarters[2],
        ],
        year_start=date(2017, 1, 1),
        year_end=date(2017, 12, 31),
    )
    assert partial == {"revenue": 40.0}
    assert "net_income" not in partial
