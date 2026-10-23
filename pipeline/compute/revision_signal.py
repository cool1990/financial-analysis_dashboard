"""当前季 EPS 修正信号。数字只认 Alpha Vantage EARNINGS_ESTIMATES 的原字段。

评价顺序（命中即停）。不用已废弃的「温和上修 / 温和下修」：

1. 没有 eps_estimate_average → 无数据
2. |EPS| < 0.1 → eps低基数
3. 覆盖人数 < 3，或上修 + 下修 = 0，或上下修都缺失 → 样本不足
   上修、下修只有一边缺失时，缺失的一边按 0，不因此判样本不足
4. |30 日变化| ≥ 5% 且 |净修正| / 覆盖人数 > 10%，方向与变化、净修正同号 → 强上修 / 强下修
5. 还没到强，但 |30 日变化| ≥ 5% 且 |净修正| ≥ 2 → 少数上修 / 少数下修（方向跟净修正）
6. 其余 → 中性

已知财报日有一天落在过去 30 天内（含今天）时，显示成「主信号/财报驱动」，否则「主信号/非财报」。
未来的财报日不算。没有日期就无法判断。

字段：
- 覆盖人数 = eps_estimate_analyst_count
- 当前季 EPS = eps_estimate_average
- 30 日% = (eps_estimate_average - eps_estimate_average_30_days_ago) / abs(30 日前)
- 上修 = eps_estimate_revision_up_trailing_30_days
- 下修 = eps_estimate_revision_down_trailing_30_days

当前季不看 TradingView，也不看 Yahoo 的下一财报日。
它是最近一份已披露财季（data/{代码}/FY*.json）的下一财季。
再在 EARNINGS_ESTIMATES 里找日期落在该季结束日约 20 天内的季度行。
对不上就空着，不拿更旧的一行来填。季末已过但还没披露时，这一季仍是当前季。
没有已披露财季时，取结束日不早于今天往前约 100 天的最早一条季度行。
真实数据的 horizon 都是 fiscal quarter，不能靠「historical」字样把旧行滤掉。
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from pipeline.compute.periods import fiscal_quarter_end, next_fiscal_period, parse_fiscal_period


DRIVE_WINDOW_DAYS = 30
QUARTER_MATCH_DAYS = 20
FALLBACK_LOOKBACK_DAYS = 100
STALE_AFTER = timedelta(days=3)
_PERIOD = re.compile(r"FY\d{4}Q[1-4]$")


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


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        day = _parse_date(text)
        if day is None:
            return None
        return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def fiscal_period_end(fiscal_year: int, fiscal_quarter: int, fiscal_year_end_month: int) -> date:
    """财季结束月的最后一天。Q4 落在财年结束月。"""
    return fiscal_quarter_end(fiscal_year, fiscal_quarter, fiscal_year_end_month)


def _revision_pair(up: Any, down: Any) -> tuple[float | None, float | None]:
    """一边有数、另一边是 null 时，null 当 0。两边都空则保持空。"""
    up_n = _num(up)
    down_n = _num(down)
    if up_n is None and down_n is None:
        return None, None
    return (0.0 if up_n is None else up_n), (0.0 if down_n is None else down_n)


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
    up_n, down_n = _revision_pair(up, down)

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
    """已知财报日有一天落在过去 window 天内（含今天）→ 财报驱动，否则非财报。未来日期不算。没有日期就无法判断。"""
    if not earnings_dates:
        return None
    for day in earnings_dates:
        delta = (today - day).days
        if 0 <= delta <= window_days:
            return "财报驱动"
    return "非财报"


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


def _dated_quarter_rows(estimates: list[Any] | None) -> list[tuple[date, dict[str, Any]]]:
    dated: list[tuple[date, dict[str, Any]]] = []
    for row in estimates or []:
        if not isinstance(row, dict) or _is_year_row(row) or not _is_quarter_row(row):
            continue
        day = _parse_date(row.get("date") or row.get("fiscalDateEnding"))
        if day is not None:
            dated.append((day, row))
    return dated


def quarter_end_after_reported(reported_period: str, fiscal_year_end_month: int) -> date:
    """已披露财季的下一季结束日。季末已过但还没有下一份 FY 文件时，返回的仍是这一季。"""
    nxt = next_fiscal_period(reported_period)
    fiscal_year, fiscal_quarter = parse_fiscal_period(nxt)
    return fiscal_quarter_end(fiscal_year, fiscal_quarter, fiscal_year_end_month)


def pick_current_quarter_estimate(
    estimates: list[Any] | None,
    target_date: date | None,
    *,
    tolerance_days: int = QUARTER_MATCH_DAYS,
) -> dict[str, Any] | None:
    """选结束日落在目标日 tolerance 天内的季度行。并列时取更近的，距离相同取较早的。

    没有落在窗口里的行就返回 None，不用更远的旧行顶上。
    horizon 里的 historical / next 字样不参与筛选。
    """
    if target_date is None:
        return None
    dated = _dated_quarter_rows(estimates)
    if not dated:
        return None
    dated.sort(key=lambda item: (abs((item[0] - target_date).days), item[0]))
    day, row = dated[0]
    if abs((day - target_date).days) <= tolerance_days:
        return row
    return None


def pick_fallback_quarter(
    estimates: list[Any] | None,
    today: date,
    *,
    lookback_days: int = FALLBACK_LOOKBACK_DAYS,
) -> dict[str, Any] | None:
    """没有已披露财季时，取结束日不早于 today - lookback 的最早一条季度行。更早的不选。"""
    floor = today - timedelta(days=lookback_days)
    dated = [(day, row) for day, row in _dated_quarter_rows(estimates) if day >= floor]
    if not dated:
        return None
    dated.sort(key=lambda item: item[0])
    return dated[0][1]


def choose_current_estimate(
    estimates: list[Any] | None,
    *,
    reported_period: str | None,
    fiscal_year_end_month: int,
    today: date,
) -> dict[str, Any] | None:
    """有已披露财季就对齐它的下一季；没有则用约 100 天内最早的一条。对不上返回 None。"""
    if reported_period and _PERIOD.fullmatch(reported_period):
        target = quarter_end_after_reported(reported_period, fiscal_year_end_month)
        return pick_current_quarter_estimate(estimates, target)
    return pick_fallback_quarter(estimates, today)


def estimates_are_stale(
    fetched_at: Any,
    now: datetime | None = None,
    *,
    max_age: timedelta = STALE_AFTER,
) -> bool:
    """fetched_at 早于 max_age（默认 3 天）则缓存过期。没有时间戳时不标旧。"""
    parsed = _parse_datetime(fetched_at)
    if parsed is None:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current - parsed > max_age


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


def format_current_quarter(
    row: dict[str, Any] | None,
    drive: str | None,
    *,
    fetched_at: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """首页一格。没有选中的季度行时，数字是「—」，评价仍走无数据。缓存超过 3 天标旧。"""
    picked = row or {}
    has_row = bool(row)
    eps = _num(picked.get("eps_estimate_average")) if has_row else None
    coverage = _num(picked.get("eps_estimate_analyst_count")) if has_row else None
    ago = _num(picked.get("eps_estimate_average_30_days_ago")) if has_row else None
    if has_row:
        up, down = _revision_pair(
            picked.get("eps_estimate_revision_up_trailing_30_days"),
            picked.get("eps_estimate_revision_down_trailing_30_days"),
        )
    else:
        up = down = None
    chg = chg_30d_pct(eps, ago) if has_row else None
    if has_row:
        signal = revision_signal_current_q(eps, coverage, chg, up, down, drive)
    else:
        signal = revision_signal_current_q(None, None, None, None, None, drive)
    revisions = "—"
    if up is not None and down is not None:
        revisions = f"{_count(up)} / {_count(down)}"
    fetched = str(fetched_at or "").strip()
    stale = bool(has_row and fetched and estimates_are_stale(fetched, now))
    return {
        "analysts": _count(coverage),
        "eps": _eps_text(eps),
        "chg": _chg_view(chg),
        "revisions": revisions,
        "signal": signal,
        "signal_cls": signal_class(signal),
        "has_numbers": any(value is not None for value in (eps, coverage, up, down, chg)),
        "stale": stale,
        "fetched_at": fetched,
        "stale_title": f"旧 {fetched}" if stale else "",
    }
