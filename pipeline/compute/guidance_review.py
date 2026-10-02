from __future__ import annotations

from typing import Any, Literal

from pipeline.extract.numbers import parse_number, parse_plus_minus


ChangeVsPrior = Literal["raised", "lowered", "maintained", "narrowed", "new", "dropped"]


def parse_guidance_item(item: dict[str, Any]) -> dict[str, Any]:
    out = dict(item)
    low = high = mid = None
    if item.get("plus_minus_raw") or (
        item.get("point_raw") and item.get("plus_minus_raw") is not None
    ):
        # point ± delta
        combined = None
        if item.get("point_raw") and item.get("plus_minus_raw"):
            combined = f"{item['point_raw']} ± {item['plus_minus_raw']}"
        elif item.get("point_raw"):
            combined = str(item["point_raw"])
        low, mid, high = parse_plus_minus(combined)
    else:
        if item.get("low_raw") is not None:
            low = parse_number(item.get("low_raw"))
        if item.get("high_raw") is not None:
            high = parse_number(item.get("high_raw"))
        if item.get("point_raw") is not None:
            mid = parse_number(item.get("point_raw"))
        if mid is None and low is not None and high is not None:
            mid = (low + high) / 2
        if low is None and high is None and mid is not None:
            low = high = mid
    out.update({"low": low, "mid": mid, "high": high})
    return out


def change_vs_prior(
    current: dict[str, Any] | None,
    prior: dict[str, Any] | None,
    *,
    is_ratio: bool = False,
    change_pct: float = 0.005,
    change_pp: float = 0.25,
) -> ChangeVsPrior:
    if prior is None and current is not None:
        return "new"
    if current is None and prior is not None:
        return "dropped"
    if current is None or prior is None:
        return "new"
    cur_mid = current.get("mid")
    pri_mid = prior.get("mid")
    if cur_mid is None or pri_mid is None:
        return "maintained"
    if is_ratio:
        # margins stored as fraction or percent? assume same units as guidance parse
        delta = cur_mid - pri_mid
        thr = change_pp if abs(cur_mid) > 1 else change_pp * 0.01
        # if values look like 42.5 (percent points), thr=0.25
        thr = change_pp if cur_mid > 1 else change_pp * 0.01
        if abs(delta) <= thr:
            # check narrowing
            if _width(current) < _width(prior) - 1e-12 and abs(delta) <= thr:
                return "narrowed"
            return "maintained"
        return "raised" if delta > 0 else "lowered"
    if pri_mid == 0:
        return "raised" if cur_mid > 0 else ("lowered" if cur_mid < 0 else "maintained")
    pct = (cur_mid - pri_mid) / abs(pri_mid)
    if abs(pct) <= change_pct:
        if _width(current) < _width(prior) - 1e-12:
            return "narrowed"
        return "maintained"
    return "raised" if pct > 0 else "lowered"


def _width(item: dict[str, Any]) -> float:
    low, high = item.get("low"), item.get("high")
    if low is None or high is None:
        return 0.0
    return float(high - low)


def position_in_range(actual: float | None, low: float | None, high: float | None) -> dict[str, Any]:
    if actual is None or low is None or high is None:
        return {"position": None, "position_pct": None}
    if high == low:
        pct = 0.5 if actual == low else (1.0 if actual > low else 0.0)
    else:
        pct = (actual - low) / (high - low)
    if pct > 1:
        pos = "above_high"
    elif pct >= 0.5:
        pos = "upper_half"
    elif pct >= 0:
        pos = "lower_half"
    else:
        pos = "below_low"
    return {"position": pos, "position_pct": pct}


def gap_closure(
    t_minus_1: float | None,
    t_plus_7: float | None,
    guidance_mid: float | None,
) -> float | None:
    if t_minus_1 is None or t_plus_7 is None or guidance_mid is None:
        return None
    denom = guidance_mid - t_minus_1
    if abs(denom) / max(abs(t_minus_1), 1e-9) < 0.01:
        return None
    return (t_plus_7 - t_minus_1) / denom
