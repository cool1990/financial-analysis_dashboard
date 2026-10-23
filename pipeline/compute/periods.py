from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from dateutil.relativedelta import relativedelta


FiscalPeriod = str  # e.g. FY2026Q4
CalendarQuarter = str  # e.g. 2026Q3


@dataclass(frozen=True)
class PeriodInfo:
    fiscal_period: FiscalPeriod
    calendar_quarter: CalendarQuarter
    period_start: date
    period_end: date
    fiscal_year: int
    fiscal_quarter: int


def _to_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def calendar_quarter_of(d: date) -> CalendarQuarter:
    q = (d.month - 1) // 3 + 1
    return f"{d.year}Q{q}"


def calendar_quarter_from_midpoint(period_start: date | str, period_end: date | str) -> CalendarQuarter:
    """取财季起止中点所在日历季度。"""
    start = _to_date(period_start)
    end = _to_date(period_end)
    mid = start + (end - start) / 2
    if isinstance(mid, datetime):
        mid_d = mid.date()
    else:
        # timedelta division yields datetime-like; ensure date
        mid_d = start + timedelta(days=(end - start).days // 2)
    return calendar_quarter_of(mid_d)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    return (nxt - timedelta(days=1)).day


def _quarter_anchor(fiscal_year: int, fiscal_quarter: int, fiscal_year_end_month: int) -> date:
    """财季锚点：该季结束月的 28 日。Q4 落在财年结束月。"""
    month = fiscal_year_end_month - (4 - fiscal_quarter) * 3
    year = fiscal_year
    while month <= 0:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    return date(year, month, min(28, _days_in_month(year, month)))


def fiscal_quarter_for_end(period_end: date | str, fiscal_year_end_month: int) -> tuple[int, int]:
    """根据财季结束日与财年结束月份，计算 (fiscal_year, fiscal_quarter)。

    财年 FY{N} 的 Q4 落在日历年 N 的 fiscal_year_end_month。
    Q1 往往在上一日历年（1 月结束的财年，Q1 在上年 4 月）。
    取距离最近的财季锚点，避免把财年结束后的下一季并进上一年 Q4。
    52/53 周结束日相对月末锚点通常只差几天。
    """
    end = _to_date(period_end)
    best_year = end.year
    best_quarter = 4
    best_dist = 10**9
    for year in range(end.year - 1, end.year + 2):
        for quarter in range(1, 5):
            dist = abs((end - _quarter_anchor(year, quarter, fiscal_year_end_month)).days)
            if dist < best_dist:
                best_dist = dist
                best_year = year
                best_quarter = quarter
    return best_year, best_quarter


def fiscal_year_guess(period_end: date, fiscal_year_end_month: int) -> int:
    """结束日所属财年。与 fiscal_quarter_for_end 使用同一套最近锚点。"""
    return fiscal_quarter_for_end(period_end, fiscal_year_end_month)[0]


def make_fiscal_period(fiscal_year: int, fiscal_quarter: int) -> FiscalPeriod:
    return f"FY{fiscal_year}Q{fiscal_quarter}"


def parse_fiscal_period(label: FiscalPeriod) -> tuple[int, int]:
    m = __import__("re").match(r"FY(\d{4})Q([1-4])", label)
    if not m:
        raise ValueError(f"非法 fiscal_period: {label}")
    return int(m.group(1)), int(m.group(2))


def build_period_info(
    period_end: date | str,
    fiscal_year_end_month: int,
    period_start: date | str | None = None,
) -> PeriodInfo:
    end = _to_date(period_end)
    if period_start is None:
        start = end - relativedelta(months=3) + timedelta(days=1)
    else:
        start = _to_date(period_start)
    fy, fq = fiscal_quarter_for_end(end, fiscal_year_end_month)
    return PeriodInfo(
        fiscal_period=make_fiscal_period(fy, fq),
        calendar_quarter=calendar_quarter_from_midpoint(start, end),
        period_start=start,
        period_end=end,
        fiscal_year=fy,
        fiscal_quarter=fq,
    )


def prior_fiscal_period(fiscal_period: FiscalPeriod) -> FiscalPeriod:
    fy, fq = parse_fiscal_period(fiscal_period)
    if fq == 1:
        return make_fiscal_period(fy - 1, 4)
    return make_fiscal_period(fy, fq - 1)


def yoy_fiscal_period(fiscal_period: FiscalPeriod) -> FiscalPeriod:
    fy, fq = parse_fiscal_period(fiscal_period)
    return make_fiscal_period(fy - 1, fq)


def days_warning_for_yoy(period_start: date, period_end: date, prior_start: date, prior_end: date) -> str | None:
    days = (period_end - period_start).days + 1
    prior_days = (prior_end - prior_start).days + 1
    if abs(days - prior_days) >= 5:
        return f"季度天数与上年同期相差 {abs(days - prior_days)} 天（52/53 周财年），同比可能失真"
    return None


Basis = Literal["gaap", "non_gaap", "unknown"]
