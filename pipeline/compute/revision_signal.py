"""当前季 EPS 修正信号。数字只认 Alpha Vantage EARNINGS_ESTIMATES 的原字段。

评价顺序（命中即停）。不用已废弃的「温和上修 / 温和下修」：

1. 没有 eps_estimate_average → 无数据
2. |EPS| < 0.1 → eps低基数
3. 覆盖人数 < 3，或上修 + 下修 = 0，或上下修缺失 → 样本不足
4. |30 日变化| ≥ 5% 且 |净修正| / 覆盖人数 > 10%，方向与变化、净修正同号 → 强上修 / 强下修
5. 还没到强，但 |30 日变化| ≥ 5% 且 |净修正| ≥ 2 → 少数上修 / 少数下修（方向跟净修正）
6. 其余 → 中性

能判断财报是否落在约 30 天内时，显示成「主信号/财报驱动」或「主信号/非财报」。

字段：
- 覆盖人数 = eps_estimate_analyst_count
- 当前季 EPS = eps_estimate_average
- 30 日% = (eps_estimate_average - eps_estimate_average_30_days_ago) / abs(30 日前)
- 上修 = eps_estimate_revision_up_trailing_30_days
- 下修 = eps_estimate_revision_down_trailing_30_days

季度：horizon 含 quarter 的行里，日期最接近第一个未公布季度。
那个季度是 TradingView future_quarters[0]（没有这份列表时，用 earnings_fq_h 里最早一条未公布）。
绝不使用 future_quarters[1]。没有 TradingView 时，用 Yahoo 下一财报日（0q），不用 +1q。
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from typing import Any


DRIVE_WINDOW_DAYS = 30
_FISCAL = re.compile(r"(?:FY)?(\d{4})-?Q([1-4])$", re.IGNORECASE)


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, str) and value.strip().lower() in {"none", "null", "n/a", "na", "—", "-"}:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _parse_fiscal_label(value: Any) -> tuple[int, int] | None:
    match = _FISCAL.search(str(value or "").strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def fiscal_period_end(fiscal_year: int, fiscal_quarter: int, fiscal_year_end_month: int) -> date:
    """财季结束月的最后一天。Q4 落在财年结束月。"""
    month = fiscal_year_end_month - (4 - fiscal_quarter) * 3
    year = fiscal_year
    while month <= 0:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def chg_30d_pct(average: Any, average_30_days_ago: Any) -> float | None:
    """(avg - 30 日前) / abs(30 日前)。30 日前缺失或为 0 时没有变化率。"""
    avg = _num(average)
    ago = _num(average_30_days_ago)
    if avg is None or ago is None or ago == 0:
        return None
    return (avg - ago) / abs(ago)


def revision_signal_current_q(
    eps: Any,
    coverage: Any,
    chg: Any,
    up: Any,
    down: Any,
    drive: str | None = None,
) -> str:
    """命中即停。chg 是小数比例，0.05 = 5%。drive 只接受「财报驱动」或「非财报」。"""
    eps_n = _num(eps)
    coverage_n = _num(coverage)
    chg_n = _num(chg)
    up_n = _num(up)
    down_n = _num(down)

    if eps_n is None:
        main = "无数据"
    elif abs(eps_n) < 0.1:
        main = "eps低基数"
    elif (
        coverage_n is None
        or coverage_n < 3
        or up_n is None
        or down_n is None
        or (up_n + down_n) == 0
    ):
        main = "样本不足"
    else:
        net = up_n - down_n
        ratio = net / coverage_n
        if chg_n is not None and chg_n >= 0.05 and ratio > 0.10:
            main = "强上修"
        elif chg_n is not None and chg_n <= -0.05 and -ratio > 0.10:
            main = "强下修"
        elif chg_n is not None and abs(chg_n) >= 0.05 and abs(net) >= 2:
            main = "少数上修" if net > 0 else "少数下修"
        else:
            main = "中性"

    if drive in {"财报驱动", "非财报"}:
        return f"{main}/{drive}"
    return main


def signal_class(label: str) -> str:
    main = (label or "").split("/", 1)[0]
    if main in {"强上修", "少数上修"}:
        return "up"
    if main in {"强下修", "少数下修"}:
        return "down"
    if main == "中性":
        return "flat"
    return "na"


def earnings_drive_tag(
    earnings_dates: list[date] | None,
    today: date,
    *,
    window_days: int = DRIVE_WINDOW_DAYS,
) -> str | None:
    """已知财报日有一天落在今天前后 window 天（含）内 → 财报驱动，否则非财报。没有日期就无法判断。"""
    if not earnings_dates:
        return None
    for day in earnings_dates:
        if abs((day - today).days) <= window_days:
            return "财报驱动"
    return "非财报"


def _date_from_quarter_token(token: Any, fiscal_year_end_month: int) -> date | None:
    if isinstance(token, dict):
        for key in ("date", "period_end", "fiscalDateEnding", "periodEnd"):
            found = _parse_date(token.get(key))
            if found:
                return found
        for key in ("FiscalPeriod", "fiscal_period", "period", "label"):
            parsed = _parse_fiscal_label(token.get(key))
            if parsed:
                return fiscal_period_end(parsed[0], parsed[1], fiscal_year_end_month)
        return None
    if isinstance(token, str):
        found = _parse_date(token)
        if found:
            return found
        parsed = _parse_fiscal_label(token)
        if parsed:
            return fiscal_period_end(parsed[0], parsed[1], fiscal_year_end_month)
    return None


def first_unpublished_date(tv: dict[str, Any] | None, *, fiscal_year_end_month: int) -> date | None:
    """第一个未公布季度的结束日。只读 future_quarters[0]，没有可用日期时也不读 [1]。"""
    tv = tv or {}
    future = tv.get("future_quarters")
    if isinstance(future, list) and future:
        found = _date_from_quarter_token(future[0], fiscal_year_end_month)
        if found:
            return found
    rows_used = tv.get("rows_used") or {}
    rows = rows_used.get("earnings_fq_h_chart") or rows_used.get("earnings_fq_h") or []
    pending: list[tuple[int, int]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("IsReported") is not False:
            continue
        parsed = _parse_fiscal_label(row.get("FiscalPeriod"))
        if parsed:
            pending.append(parsed)
    if not pending:
        return None
    fiscal_year, fiscal_quarter = sorted(pending)[0]
    return fiscal_period_end(fiscal_year, fiscal_quarter, fiscal_year_end_month)


def current_quarter_target(
    tv: dict[str, Any] | None,
    *,
    fiscal_year_end_month: int,
    yahoo_0q_date: date | None = None,
) -> date | None:
    """当前季 = 第一个未公布季度。TradingView 没有时，用 Yahoo 0q 的下一财报日，不用 +1q。"""
    found = first_unpublished_date(tv, fiscal_year_end_month=fiscal_year_end_month)
    if found:
        return found
    return yahoo_0q_date


def _horizon(row: dict[str, Any]) -> str:
    return str(row.get("horizon") or "").strip().lower().replace("_", " ")


def _is_year_row(row: dict[str, Any]) -> bool:
    horizon = _horizon(row)
    return "year" in horizon and "quarter" not in horizon


def _is_quarter_row(row: dict[str, Any]) -> bool:
    horizon = _horizon(row)
    if not horizon:
        return False
    if "quarter" in horizon:
        return True
    return horizon in {"0q", "fq", "current"}


def pick_current_quarter_estimate(
    estimates: list[Any] | None,
    target_date: date | None,
) -> dict[str, Any] | None:
    """选日期最接近当前季的季度行。并列时取较早的日期，不取下一个季度。"""
    rows = [row for row in (estimates or []) if isinstance(row, dict)]
    if len(rows) == 1 and not _is_year_row(rows[0]) and (
        "eps_estimate_average" in rows[0] or _is_quarter_row(rows[0])
    ):
        return rows[0]

    quarters = [row for row in rows if _is_quarter_row(row)]
    if not quarters:
        return None
    dated: list[tuple[date, dict[str, Any]]] = []
    for row in quarters:
        day = _parse_date(row.get("date") or row.get("fiscalDateEnding"))
        if day is not None:
            dated.append((day, row))
    if target_date is not None and dated:
        dated.sort(key=lambda item: (abs((item[0] - target_date).days), item[0]))
        return dated[0][1]

    next_rows = [row for row in quarters if "next" in _horizon(row)]
    if len(next_rows) == 1 and not dated:
        return next_rows[0]
    if dated:
        future = [(day, row) for day, row in dated if "historical" not in _horizon(row)]
        pool = future or dated
        pool.sort(key=lambda item: item[0])
        return pool[0][1]
    return (next_rows or quarters)[0]


def _count(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    return f"{value:.1f}"


def _eps_text(value: float | None) -> str:
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):.2f}"


def _chg_view(value: float | None) -> dict[str, str]:
    if value is None:
        return {"text": "—", "cls": "na"}
    cls = "up" if value > 0 else "down" if value < 0 else "flat"
    return {"text": f"{value * 100:+.1f}%", "cls": cls}


def format_current_quarter(row: dict[str, Any] | None, drive: str | None) -> dict[str, Any]:
    """首页一格。没有选中的季度行时，数字是「—」，评价仍走无数据。"""
    picked = row or {}
    has_row = bool(row)
    eps = _num(picked.get("eps_estimate_average")) if has_row else None
    coverage = _num(picked.get("eps_estimate_analyst_count")) if has_row else None
    ago = _num(picked.get("eps_estimate_average_30_days_ago")) if has_row else None
    up = _num(picked.get("eps_estimate_revision_up_trailing_30_days")) if has_row else None
    down = _num(picked.get("eps_estimate_revision_down_trailing_30_days")) if has_row else None
    chg = chg_30d_pct(eps, ago) if has_row else None
    if has_row:
        signal = revision_signal_current_q(eps, coverage, chg, up, down, drive)
    else:
        signal = revision_signal_current_q(None, None, None, None, None, drive)
    revisions = "—"
    if up is not None and down is not None:
        revisions = f"{_count(up)} / {_count(down)}"
    return {
        "analysts": _count(coverage),
        "eps": _eps_text(eps),
        "chg": _chg_view(chg),
        "revisions": revisions,
        "signal": signal,
        "signal_cls": signal_class(signal),
        "has_numbers": any(value is not None for value in (eps, coverage, up, down, chg)),
    }
