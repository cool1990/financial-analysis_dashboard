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


def make_benchmark(
    actual: float | None,
    benchmark: float | None,
    *,
    source: str = "none",
    is_ratio: bool = False,
) -> dict[str, Any]:
    if actual is None or benchmark is None:
        return {
            "value": benchmark,
            "source": source if benchmark is not None else "none",
            "diff": None,
            "diff_pct": None,
            "diff_pp": None,
        }
    diff = actual - benchmark
    return {
        "value": benchmark,
        "source": source,
        "diff": diff,
        "diff_pct": None if is_ratio else pct_change(actual, benchmark),
        "diff_pp": pp_change(actual, benchmark) if is_ratio else None,
    }


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
        return {
            "value": value,
            "yoy_pct": None,
            "qoq_pct": None,
            "yoy_pp": pp_change(value, yoy_base),
            "qoq_pp": pp_change(value, qoq_base),
            "benchmark": make_benchmark(value, benchmark, source=benchmark_source, is_ratio=True),
            "source_quote": source_quote,
        }
    return {
        "value": value,
        "yoy_pct": pct_change(value, yoy_base),
        "qoq_pct": pct_change(value, qoq_base),
        "yoy_pp": None,
        "qoq_pp": None,
        "benchmark": make_benchmark(value, benchmark, source=benchmark_source, is_ratio=False),
        "source_quote": source_quote,
    }
