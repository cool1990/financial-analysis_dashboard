from __future__ import annotations

from typing import Any

from pipeline.compute.metrics import pct_change, round_eps, verdict_eps, verdict_revenue
from pipeline.config import load_settings


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

    rev_diff = (revenue_actual - revenue_estimate) if revenue_actual is not None and revenue_estimate is not None else None
    rev_pct = pct_change(revenue_actual, revenue_estimate)
    cards.append(
        {
            "metric": "revenue",
            "actual": revenue_actual,
            "benchmark": revenue_estimate,
            "benchmark_source": "consensus" if revenue_estimate is not None else "none",
            "diff": rev_diff,
            "pct": rev_pct,
            "verdict": verdict_revenue(rev_pct, thr["revenue_inline_pct"]),
            "basis": "gaap",
        }
    )

    eps_a = round_eps(eps_actual)
    eps_e = round_eps(eps_estimate)
    eps_diff = (eps_a - eps_e) if eps_a is not None and eps_e is not None else None
    eps_pct = pct_change(eps_a, eps_e)
    cards.append(
        {
            "metric": "eps",
            "actual": eps_a,
            "benchmark": eps_e,
            "benchmark_source": "consensus" if eps_e is not None else "none",
            "diff": eps_diff,
            "pct": eps_pct,
            "verdict": verdict_eps(
                eps_diff,
                eps_pct,
                inline_pct=thr["eps_inline_pct"],
                inline_abs=thr["eps_inline_abs"],
                estimate=eps_e,
            ),
            "basis": eps_basis,
        }
    )

    def _margin_card(name: str, actual: float | None, bench: float | None) -> dict[str, Any]:
        diff = (actual - bench) if actual is not None and bench is not None else None
        return {
            "metric": name,
            "actual": actual,
            "benchmark": bench,
            "benchmark_source": "prior_guidance_mid" if bench is not None else "none",
            "diff": diff,
            "pct": diff,  # pp on fraction scale
            "verdict": "inline"
            if diff is not None and abs(diff) <= thr["margin_inline_pp"] * 0.01
            else ("beat" if diff and diff > 0 else ("miss" if diff and diff < 0 else "unknown")),
            "basis": eps_basis,
        }

    cards.append(_margin_card("gross_margin", gross_margin, gross_margin_benchmark))
    cards.append(_margin_card("operating_margin", operating_margin, operating_margin_benchmark))
    cards.append(
        {
            "metric": "fcf",
            "actual": fcf,
            "benchmark": None,
            "benchmark_source": "none",
            "diff": None,
            "pct": None,
            "verdict": "",
            "basis": "gaap",
        }
    )

    nrev_pct = pct_change(next_q_revenue_guidance_mid, next_q_revenue_consensus)
    cards.append(
        {
            "metric": "next_q_revenue_guidance",
            "actual": next_q_revenue_guidance_mid,
            "benchmark": next_q_revenue_consensus,
            "benchmark_source": "consensus",
            "diff": (next_q_revenue_guidance_mid - next_q_revenue_consensus)
            if next_q_revenue_guidance_mid is not None and next_q_revenue_consensus is not None
            else None,
            "pct": nrev_pct,
            "verdict": verdict_revenue(nrev_pct, thr["revenue_inline_pct"])
            if next_q_revenue_guidance_mid is not None and next_q_revenue_consensus is not None
            else "unknown",
            "basis": "gaap",
        }
    )

    neps_diff = (
        round_eps(next_q_eps_guidance_mid) - round_eps(next_q_eps_consensus)
        if next_q_eps_guidance_mid is not None and next_q_eps_consensus is not None
        else None
    )
    neps_pct = pct_change(round_eps(next_q_eps_guidance_mid), round_eps(next_q_eps_consensus))
    cards.append(
        {
            "metric": "next_q_eps_guidance",
            "actual": round_eps(next_q_eps_guidance_mid),
            "benchmark": round_eps(next_q_eps_consensus),
            "benchmark_source": "consensus",
            "diff": neps_diff,
            "pct": neps_pct,
            "verdict": verdict_eps(
                neps_diff,
                neps_pct,
                inline_pct=thr["eps_inline_pct"],
                inline_abs=thr["eps_inline_abs"],
                estimate=round_eps(next_q_eps_consensus),
            )
            if neps_diff is not None
            else "unknown",
            "basis": eps_basis,
        }
    )
    return cards
