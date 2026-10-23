"""单季残差：只用同一起点、结束日相距一个季度的两段累计相减。

跨度不是一个季度就返回 None，避免把半年差写成一季（硬算放大）。
每股盈利不参与相减：加权股本不是可加的。
"""

from __future__ import annotations

from datetime import date

# 流量科目。eps 故意不在这里。
FLOW_KEYS = (
    "revenue",
    "cost_of_revenue",
    "gross_profit",
    "operating_income",
    "operating_income_nongaap",
    "net_income",
    "vas_revenue",
    "advertising_revenue",
    "fintech_biz_revenue",
    "selling_expense",
    "ga_expense",
    "rd_expense",
    "capex",
)


def is_one_quarter_gap(later_end: date, earlier_end: date) -> bool:
    """两段累计的结束日相距 80–100 天才像一个季度。"""
    if earlier_end >= later_end:
        return False
    return 80 <= (later_end - earlier_end).days <= 100


def residual_flows(
    later: dict[str, float],
    earlier: dict[str, float],
    *,
    later_end: date,
    earlier_end: date,
    later_start: date,
    earlier_start: date,
) -> dict[str, float] | None:
    """later、earlier 必须是同一起点的累计，单位一致。

    某一科目只在一边出现就跳过该科目，不拿缺数去减。
    跨度不是一个季度，或起点不同，整段返回 None。
    """
    if later_start != earlier_start:
        return None
    if not is_one_quarter_gap(later_end, earlier_end):
        return None
    out: dict[str, float] = {}
    for key in FLOW_KEYS:
        left = later.get(key)
        right = earlier.get(key)
        if left is None or right is None:
            continue
        out[key] = left - right
    return out or None


def residual_from_quarter_sum(
    annual: dict[str, float],
    quarters: list[tuple[date, dict[str, float]]],
    *,
    year_start: date,
    year_end: date,
) -> dict[str, float] | None:
    """Q4 = 全年 − (Q1+Q2+Q3)。三季缺任何一季则整段留空。

    quarters 必须正好是该财年 3/31、6/30、9/30 三季。某一科目在任一季缺失则该科目留空。
    """
    expected = [date(year_end.year, 3, 31), date(year_end.year, 6, 30), date(year_end.year, 9, 30)]
    if year_end != date(year_end.year, 12, 31):
        return None
    if year_start != date(year_end.year, 1, 1):
        return None
    by_end = {end: flows for end, flows in quarters}
    if any(day not in by_end for day in expected):
        return None
    summed: dict[str, float] = {}
    for key in FLOW_KEYS:
        parts = [by_end[day].get(key) for day in expected]
        if any(part is None for part in parts):
            continue
        summed[key] = float(sum(part for part in parts if part is not None))
    return residual_flows(
        annual,
        summed,
        later_end=year_end,
        earlier_end=expected[-1],
        later_start=year_start,
        earlier_start=year_start,
    )
