from __future__ import annotations

from typing import Any

from pipeline.compute.guidance_review import gap_closure
from pipeline.sources.yfinance_src import estimate_row


def compute_analyst_revisions(
    snapshots: dict[str, dict[str, Any]],
    *,
    t_dates: dict[str, str],
    guidance_mid_eps: float | None = None,
    guidance_mid_rev: float | None = None,
) -> dict[str, Any]:
    """snapshots: {date: snapshot_json}; t_dates keys: t_minus_1, t_plus_1, t_plus_3, t_plus_7."""

    def pick(date_key: str, field: str, row: str = "0q") -> float | None:
        d = t_dates.get(date_key)
        if not d or d not in snapshots:
            return None
        block = snapshots[d].get("earnings_estimate" if field == "eps" else "revenue_estimate")
        row_data = estimate_row(block or {}, row)
        if not row_data:
            return None
        val = row_data.get("avg")
        return float(val) if val is not None else None

    out: dict[str, Any] = {}
    for key in ["t_minus_1", "t_plus_1", "t_plus_3", "t_plus_7"]:
        eps = pick(key, "eps")
        rev = pick(key, "rev")
        out[key] = {"eps": eps, "revenue": rev, "date": t_dates.get(key)}

    t0_eps = out["t_minus_1"]["eps"]
    t7_eps = out["t_plus_7"]["eps"]
    t0_rev = out["t_minus_1"]["revenue"]
    t7_rev = out["t_plus_7"]["revenue"]
    out["gap_closure"] = {
        "eps": gap_closure(t0_eps, t7_eps, guidance_mid_eps),
        "revenue": gap_closure(t0_rev, t7_rev, guidance_mid_rev),
    }
    # changes vs T-1
    for key in ["t_plus_1", "t_plus_3", "t_plus_7"]:
        for metric in ["eps", "revenue"]:
            base = out["t_minus_1"][metric]
            cur = out[key][metric]
            if base and cur is not None and base != 0:
                out[key][f"{metric}_chg"] = (cur - base) / abs(base)
            else:
                out[key][f"{metric}_chg"] = None
    return out
