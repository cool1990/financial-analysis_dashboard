from __future__ import annotations

"""指引抽取：raw → 数值解析。"""

from typing import Any

from pipeline.extract.numbers import PM_SPLIT_RE, parse_number, parse_plus_minus


def parse_guidance_item(item: dict[str, Any]) -> dict[str, Any]:
    """解析指引条目的 raw 文本为 low/mid/high。"""
    out = dict(item)
    low = high = mid = None
    point_has_pm = bool(item.get("point_raw")) and bool(PM_SPLIT_RE.search(str(item["point_raw"])))
    if point_has_pm and not item.get("plus_minus_raw"):
        # LLM 常把 "$61.5 billion ± $1.5 billion" 整段放进 point_raw
        low, mid, high = parse_plus_minus(str(item["point_raw"]))
    elif item.get("plus_minus_raw") or (
        item.get("point_raw") and item.get("plus_minus_raw") is not None
    ):
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
