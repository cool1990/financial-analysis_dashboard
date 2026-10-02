from __future__ import annotations

from typing import Any, Literal


Verdict = Literal["beat", "miss", "inline", "unknown", ""]


def pct_change(actual: float | None, base: float | None) -> float | None:
    if actual is None or base is None or base == 0:
        return None
    return (actual - base) / abs(base)


def pp_change(actual: float | None, base: float | None) -> float | None:
    if actual is None or base is None:
        return None
    return actual - base


def ratio(numer: float | None, denom: float | None) -> float | None:
    if numer is None or denom is None or denom == 0:
        return None
    return numer / denom


def round_eps(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 2)


def verdict_revenue(pct: float | None, inline_pct: float = 0.01) -> Verdict:
    if pct is None:
        return "unknown"
    if abs(pct) <= inline_pct:
        return "inline"
    return "beat" if pct > 0 else "miss"


def verdict_eps(
    diff: float | None,
    pct: float | None,
    *,
    inline_pct: float = 0.02,
    inline_abs: float = 0.01,
    estimate: float | None = None,
) -> Verdict:
    if diff is None:
        return "unknown"
    # 微利/亏损：绝对值判定
    if estimate is not None and abs(estimate) < 0.05:
        if abs(diff) <= inline_abs:
            return "inline"
        return "beat" if diff > 0 else "miss"
    if pct is not None and (abs(pct) <= inline_pct or abs(diff) <= inline_abs):
        return "inline"
    if abs(diff) <= inline_abs:
        return "inline"
    return "beat" if diff > 0 else "miss"


def verdict_margin(pp: float | None, inline_pp: float = 0.5) -> Verdict:
    if pp is None:
        return "unknown"
    # inline_pp is in percentage points on 0-100 scale or 0-1?
    # We store margins as fractions (0.42). Convert threshold.
    thr = inline_pp / 100.0 if inline_pp > 1 else inline_pp
    # Doc says ±0.5 个百分点 → 0.5 on percent scale = 0.005 fraction
    thr = inline_pp * 0.01
    if abs(pp) <= thr:
        return "inline"
    return "beat" if pp > 0 else "miss"


def ytd_to_quarterly(
    current_ytd: float | None,
    prior_ytd: float | None,
    *,
    is_first_quarter: bool,
) -> float | None:
    if current_ytd is None:
        return None
    if is_first_quarter:
        return current_ytd
    if prior_ytd is None:
        return None
    return current_ytd - prior_ytd


def build_metric_block(
    value: float | None,
    *,
    yoy_base: float | None = None,
    qoq_base: float | None = None,
    benchmark: float | None = None,
    benchmark_source: str = "none",
    is_ratio: bool = False,
    source_quote: str | None = None,
) -> dict[str, Any]:
    if is_ratio:
        yoy = pp_change(value, yoy_base)
        qoq = pp_change(value, qoq_base)
        vs = pp_change(value, benchmark) if benchmark is not None else None
    else:
        yoy = pct_change(value, yoy_base)
        qoq = pct_change(value, qoq_base)
        vs = pct_change(value, benchmark) if benchmark is not None else None
    return {
        "value": value,
        "yoy": yoy,
        "qoq": qoq,
        "benchmark": {
            "value": benchmark,
            "source": benchmark_source,
            "diff": (value - benchmark) if value is not None and benchmark is not None else None,
            "pct_or_pp": vs,
        },
        "source_quote": source_quote,
    }
