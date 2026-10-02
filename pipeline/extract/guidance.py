from __future__ import annotations

"""指引抽取：raw → 数值解析。"""

import re
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


_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4}
_PERIOD_PATTERNS = [
    # FQ1-27 / FQ1 FY27 / Q1-27
    re.compile(r"\bF?Q(?P<q>[1-4])[\s\-–]*(?:FY|F)?['’]?(?P<y>\d{2}|\d{4})\b", re.I),
    # Q1 Fiscal 2027 / Q1 FY2027 / Q1 of fiscal 2027
    re.compile(r"\bQ(?P<q>[1-4])\s*(?:of\s+)?(?:fiscal\s*(?:year\s*)?|FY\s*)['’]?(?P<y>\d{2}|\d{4})\b", re.I),
    # first quarter (of) fiscal (year) 2027
    re.compile(
        r"\b(?P<qw>first|second|third|fourth)\s+(?:fiscal\s+)?quarter\s+(?:of\s+)?(?:fiscal\s*(?:year\s*)?|FY\s*)['’]?(?P<y>\d{2}|\d{4})\b",
        re.I,
    ),
    # fiscal 2027 first quarter / FY27 Q1
    re.compile(r"\b(?:fiscal\s*(?:year\s*)?|FY\s*)['’]?(?P<y>\d{2}|\d{4})\s+(?:Q(?P<q>[1-4])|(?P<qw>first|second|third|fourth)\s+quarter)\b", re.I),
]
_FY_ONLY = re.compile(r"^\s*(?:full[\s\-]?year\s+)?(?:fiscal\s*(?:year\s*)?|FY\s*)['’]?(?P<y>\d{2}|\d{4})\s*$", re.I)


def normalize_period(period: str | None) -> str:
    """把指引期间的各种写法归一成 FY2027Q1 / FY2027，用作合并键；无法识别时返回小写原文。

    新闻稿常写 "FQ1-27"，电话会写 "Q1 Fiscal 2027" 或 "first quarter fiscal 2027"，
    不归一会被当成两条指引重复展示。
    """
    text = (period or "").strip()
    if not text:
        return ""
    for pat in _PERIOD_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        gd = m.groupdict()
        q = int(gd["q"]) if gd.get("q") else _ORDINALS.get((gd.get("qw") or "").lower())
        y = int(gd["y"])
        if y < 100:
            y += 2000
        if q:
            return f"FY{y}Q{q}"
    m = _FY_ONLY.match(text)
    if m:
        y = int(m.group("y"))
        return f"FY{y + 2000 if y < 100 else y}"
    return re.sub(r"\s+", " ", text.lower())
