from __future__ import annotations

from typing import Any

from pipeline.compute.guidance_review import gap_closure


def compute_analyst_revisions(
    points: dict[str, dict[str, Any]],
    *,
    guidance_mid_eps: float | None = None,
    guidance_mid_rev: float | None = None,
) -> dict[str, Any]:
    """根据已对齐的时点数据计算分析师修正。

    points 形如：
      {
        "t_minus_1": {"eps": 1.0, "revenue": 1e9, "date": "2026-09-20"},
        "t_plus_1": {...},
        ...
      }
    由 commands/ 负责从 snapshots / sources 取数并对齐财季后再传入。
    """
    out: dict[str, Any] = {}
    for key in ["t_minus_1", "t_plus_1", "t_plus_3", "t_plus_7"]:
        row = points.get(key) or {}
        out[key] = {
            "eps": row.get("eps"),
            "revenue": row.get("revenue"),
            "date": row.get("date"),
        }

    t0_eps = out["t_minus_1"]["eps"]
    t7_eps = out["t_plus_7"]["eps"]
    t0_rev = out["t_minus_1"]["revenue"]
    t7_rev = out["t_plus_7"]["revenue"]
    out["gap_closure"] = {
        "eps": gap_closure(t0_eps, t7_eps, guidance_mid_eps),
        "revenue": gap_closure(t0_rev, t7_rev, guidance_mid_rev),
    }
    for key in ["t_plus_1", "t_plus_3", "t_plus_7"]:
        for metric in ["eps", "revenue"]:
            base = out["t_minus_1"][metric]
            cur = out[key][metric]
            if base and cur is not None and base != 0:
                out[key][f"{metric}_chg"] = (cur - base) / abs(base)
            else:
                out[key][f"{metric}_chg"] = None
    return out


def align_consensus_to_fiscal_period(
    *,
    target_fiscal_period: str,
    pre_release_plus1q_eps: dict[str, Any] | None,
    pre_release_plus1q_rev: dict[str, Any] | None,
    post_release_0q_eps: dict[str, Any] | None,
    post_release_0q_rev: dict[str, Any] | None,
) -> dict[str, Any]:
    """将财报前 +1q 与财报后 0q 对齐到同一目标财季（不按行名直接比）。"""

    def _avg(row: dict[str, Any] | None) -> float | None:
        if not row:
            return None
        val = row.get("avg")
        return float(val) if val is not None else None

    return {
        "fiscal_period": target_fiscal_period,
        "pre_earnings": {
            "row": "+1q",
            "eps": _avg(pre_release_plus1q_eps),
            "revenue": _avg(pre_release_plus1q_rev),
        },
        "post_earnings": {
            "row": "0q",
            "eps": _avg(post_release_0q_eps),
            "revenue": _avg(post_release_0q_rev),
        },
        "aligned": True,
        "note": "财报前 +1q 与财报后 0q 均绑定到同一 fiscal_period",
    }
