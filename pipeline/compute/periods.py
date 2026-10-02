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


def fiscal_quarter_for_end(period_end: date | str, fiscal_year_end_month: int) -> tuple[int, int]:
    """根据财季结束日与财年结束月份，计算 (fiscal_year, fiscal_quarter)。

    财年 FY{N} 结束于日历年 N 的 fiscal_year_end_month。
    """
    end = _to_date(period_end)
    # 财年标签：结束月之后到下一年结束月之前属于下一财年
    if end.month > fiscal_year_end_month or (
        end.month == fiscal_year_end_month and end.day >= 15
    ):
        # 接近或刚过财年末：Q4 结束日通常在结束月或稍后几天
        fiscal_year = end.year if end.month >= fiscal_year_end_month else end.year
    else:
        fiscal_year = end.year if end.month > fiscal_year_end_month else end.year

    # 更稳健：以“结束月中心”划分四个季度
    # Q1 ends ~ FYE-9m, Q2 ~ FYE-6m, Q3 ~ FYE-3m, Q4 ~ FYE
    centers = []
    for q in range(1, 5):
        # Q4 center month = fiscal_year_end_month of fiscal_year
        months_before_fye = (4 - q) * 3
        year = fiscal_year_guess(end, fiscal_year_end_month)
        center_month_idx = fiscal_year_end_month - months_before_fye
        center_year = year
        while center_month_idx <= 0:
            center_month_idx += 12
            center_year -= 1
        centers.append((q, center_year, center_month_idx))

    year = fiscal_year_guess(end, fiscal_year_end_month)
    best_q = 4
    best_dist = 10**9
    for q, cy, cm in [
        (1, year if fiscal_year_end_month - 9 > 0 else year - 1, ((fiscal_year_end_month - 9 - 1) % 12) + 1),
        (2, year if fiscal_year_end_month - 6 > 0 else year - 1, ((fiscal_year_end_month - 6 - 1) % 12) + 1),
        (3, year if fiscal_year_end_month - 3 > 0 else year - 1, ((fiscal_year_end_month - 3 - 1) % 12) + 1),
        (4, year, fiscal_year_end_month),
    ]:
        # approximate end day = 28 of center month
        approx = date(cy if q < 4 else year, cm, 28)
        # fix year for q1-q3 relative to FYE year
        if q < 4:
            m = fiscal_year_end_month - (4 - q) * 3
            y = year
            while m <= 0:
                m += 12
                y -= 1
            approx = date(y, m, min(28, _days_in_month(y, m)))
        dist = abs((end - approx).days)
        if dist < best_dist:
            best_dist = dist
            best_q = q
    return year, best_q


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    return (nxt - timedelta(days=1)).day


def fiscal_year_guess(period_end: date, fiscal_year_end_month: int) -> int:
    """财年 FY{N} 的 Q4 结束于日历年 N 的结束月附近。

    若结束日在结束月之后的月份，仍属同一财年（52/53 周浮动）。
    若结束日远早于结束月，则可能是下一财年的早期季度——由季度匹配决定年份。
    """
    end = period_end
    # 默认：结束月及之后到 12 月 → 当年为财年；1 月到结束月之前 → 看是否更接近上一年 FYE
    if end.month > fiscal_year_end_month:
        # e.g. MU FY ends Aug; Sept 1 still FY of that calendar year
        if end.month == fiscal_year_end_month + 1 and end.day <= 15:
            return end.year
        return end.year
    if end.month == fiscal_year_end_month:
        return end.year
    # before FYE month: could be Q1-Q3 of FY ending this year, or Q4 of prior year spilled?
    # Prefer FY ending this calendar year if within ~10 months before FYE
    months_to_fye = (fiscal_year_end_month - end.month) % 12
    if months_to_fye <= 9:
        return end.year
    return end.year + 1


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
