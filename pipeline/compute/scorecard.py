from __future__ import annotations

from typing import Any

from pipeline.compute.metrics import make_benchmark, pct_change, round_eps, verdict_eps, verdict_revenue
from pipeline.config import load_settings


def _scorecard_row(
    metric: str,
    actual: float | None,
    benchmark: float | None,
    *,
    source: str,
    verdict: str,
    basis: str,
    is_ratio: bool = False,
) -> dict[str, Any]:
    return {
        "metric": metric,
        "actual": actual,
        "benchmark": make_benchmark(actual, benchmark, source=source, is_ratio=is_ratio),
        "verdict": verdict,
        "basis": basis,
    }


def build_scorecard(
    *,
    revenue_actual: float | None,
    revenue_estimate: float | None,
    eps_actual: float | None,
    eps_estimate: float | None,
    eps_basis: str,
    gross_margin: float | None = None,
    gross_margin_benchmark: float | None = None,
    operating_margin: float | None = None,
    operating_margin_benchmark: float | None = None,
    fcf: float | None = None,
    next_q_revenue_guidance_mid: float | None = None,
    next_q_revenue_consensus: float | None = None,
    next_q_eps_guidance_mid: float | None = None,
    next_q_eps_consensus: float | None = None,
) -> list[dict[str, Any]]:
    thr = load_settings()["thresholds"]
    cards: list[dict[str, Any]] = []

    rev_pct = pct_change(revenue_actual, revenue_estimate)
    cards.append(
        _scorecard_row(
            "revenue",
            revenue_actual,
            revenue_estimate,
            source="consensus" if revenue_estimate is not None else "none",
            verdict=verdict_revenue(rev_pct, thr["revenue_inline_pct"]),
            basis="gaap",
        )
    )

    eps_a = round_eps(eps_actual)
    eps_e = round_eps(eps_estimate)
    eps_diff = (eps_a - eps_e) if eps_a is not None and eps_e is not None else None
    eps_pct = pct_change(eps_a, eps_e)
    cards.append(
        _scorecard_row(
            "eps",
            eps_a,
            eps_e,
            source="consensus" if eps_e is not None else "none",
            verdict=verdict_eps(
                eps_diff,
                eps_pct,
                inline_pct=thr["eps_inline_pct"],
                inline_abs=thr["eps_inline_abs"],
                estimate=eps_e,
            ),
            basis=eps_basis,
        )
    )

    def _margin_verdict(actual: float | None, bench: float | None) -> str:
        if actual is None or bench is None:
            return "unknown"
        diff = actual - bench
        if abs(diff) <= thr["margin_inline_pp"] * 0.01:
            return "inline"
        return "beat" if diff > 0 else "miss"

    cards.append(
        _scorecard_row(
            "gross_margin",
            gross_margin,
            gross_margin_benchmark,
            source="prior_guidance_mid" if gross_margin_benchmark is not None else "none",
            verdict=_margin_verdict(gross_margin, gross_margin_benchmark),
            basis=eps_basis,
            is_ratio=True,
        )
    )
    cards.append(
        _scorecard_row(
            "operating_margin",
            operating_margin,
            operating_margin_benchmark,
            source="prior_guidance_mid" if operating_margin_benchmark is not None else "none",
            verdict=_margin_verdict(operating_margin, operating_margin_benchmark),
            basis=eps_basis,
            is_ratio=True,
        )
    )
    cards.append(
        _scorecard_row(
            "fcf",
            fcf,
            None,
            source="none",
            verdict="",
            basis="gaap",
        )
    )

    nrev_pct = pct_change(next_q_revenue_guidance_mid, next_q_revenue_consensus)
    cards.append(
        _scorecard_row(
            "next_q_revenue_guidance",
            next_q_revenue_guidance_mid,
            next_q_revenue_consensus,
            source="consensus",
            verdict=verdict_revenue(nrev_pct, thr["revenue_inline_pct"])
            if next_q_revenue_guidance_mid is not None and next_q_revenue_consensus is not None
            else "unknown",
            basis="gaap",
        )
    )

    neps_a = round_eps(next_q_eps_guidance_mid)
    neps_e = round_eps(next_q_eps_consensus)
    neps_diff = (neps_a - neps_e) if neps_a is not None and neps_e is not None else None
    neps_pct = pct_change(neps_a, neps_e)
    cards.append(
        _scorecard_row(
            "next_q_eps_guidance",
            neps_a,
            neps_e,
            source="consensus",
            verdict=verdict_eps(
                neps_diff,
                neps_pct,
                inline_pct=thr["eps_inline_pct"],
                inline_abs=thr["eps_inline_abs"],
                estimate=neps_e,
            )
            if neps_diff is not None
            else "unknown",
            basis=eps_basis,
        )
    )
    return cards
