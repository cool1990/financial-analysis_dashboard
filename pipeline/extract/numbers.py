from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


UNIT_SCALE = {
    "units": 1.0,
    "unit": 1.0,
    "ones": 1.0,
    "thousands": 1_000.0,
    "thousand": 1_000.0,
    "millions": 1_000_000.0,
    "million": 1_000_000.0,
    "billions": 1_000_000_000.0,
    "billion": 1_000_000_000.0,
}


FOOTNOTE_RE = re.compile(r"(?<=\d)\(\d+\)\s*$")
PLUS_MINUS_RE = re.compile(
    r"(?P<point>[-+]?\(?\$?[\d,]+\.?\d*\)?%?)\s*[±\+\-]\s*(?P<delta>\$?[\d,]+\.?\d*%?)",
    re.IGNORECASE,
)
PLAIN_NUM_RE = re.compile(
    r"(?P<sign>[-+])?\s*(?P<paren>\()?\s*\$?\s*(?P<body>[\d,]+(?:\.\d+)?)\s*(?P<pct>%)?\s*(?P<close>\))?"
)
WORD_UNIT_RE = re.compile(
    r"(?P<num>[-+]?\(?\$?[\d,]+\.?\d*\)?)\s*(?P<unit>billion|million|thousand|billions|millions|thousands)s?\b",
    re.IGNORECASE,
)


def strip_footnote(raw: str) -> str:
    return FOOTNOTE_RE.sub("", raw.strip())


def parse_number(raw: str | None) -> float | None:
    """解析新闻稿中的数字文本。

    支持：$1,234.5、(0.12)、0.48(1)、$12.2 billion、± $300 million、42.5%。
    返回原始量级（不做单位换算，除非文本中自带 billion/million）。
    """
    if raw is None:
        return None
    text = strip_footnote(str(raw)).strip()
    if not text or text.lower() in {"null", "none", "n/a", "-"}:
        return None

    # 纯 ±delta 形式：± $300 million
    pm = re.match(r"^[±]\s*(.+)$", text)
    if pm:
        return parse_number(pm.group(1))

    word = WORD_UNIT_RE.search(text)
    if word:
        base = parse_number(word.group("num"))
        if base is None:
            return None
        scale = UNIT_SCALE[word.group("unit").lower().rstrip("s") + ("s" if not word.group("unit").lower().endswith("s") else "")]
        # normalize key
        unit_key = word.group("unit").lower()
        if not unit_key.endswith("s"):
            unit_key += "s"
        scale = UNIT_SCALE.get(unit_key, UNIT_SCALE.get(unit_key.rstrip("s"), 1.0))
        return base * scale

    m = PLAIN_NUM_RE.search(text.replace(" ", ""))
    if not m:
        # 尝试去掉千分位后直接 float
        cleaned = text.replace(",", "").replace("$", "").replace("%", "").strip()
        neg = cleaned.startswith("(") and cleaned.endswith(")")
        cleaned = cleaned.strip("()")
        try:
            val = float(cleaned)
            return -val if neg else val
        except ValueError:
            return None

    body = m.group("body").replace(",", "")
    try:
        val = float(body)
    except ValueError:
        return None

    if m.group("paren") and m.group("close"):
        val = -val
    elif m.group("sign") == "-":
        val = -val
    return val


def apply_unit(value: float | None, unit: str | None, *, is_eps: bool = False) -> float | None:
    if value is None:
        return None
    if is_eps:
        return value
    if not unit:
        return value
    key = unit.strip().lower()
    scale = UNIT_SCALE.get(key)
    if scale is None:
        # tolerate "in millions"
        for k, s in UNIT_SCALE.items():
            if k in key:
                return value * s
        return value
    return value * scale


def parse_plus_minus(raw: str | None) -> tuple[float | None, float | None, float | None]:
    """解析 '中值 ± X'，返回 (low, mid, high)。金额类与百分比均可。"""
    if raw is None:
        return None, None, None
    text = strip_footnote(str(raw)).strip()
    m = PLUS_MINUS_RE.search(text)
    if not m:
        point = parse_number(text)
        return point, point, point
    point = parse_number(m.group("point"))
    delta = parse_number(m.group("delta"))
    if point is None or delta is None:
        return None, None, None
    # 百分比符号一致性：若 point 是 42.5 且 delta 文本含 %，都当百分点
    return point - abs(delta), point, point + abs(delta)


def normalize_raw_for_match(raw: str) -> str:
    """用于 V1：忽略 $、空格、千分位后比对。"""
    return re.sub(r"[\s$,]", "", strip_footnote(raw))


def raw_in_text(raw: str | None, text: str) -> bool:
    if not raw:
        return False
    needle = normalize_raw_for_match(raw)
    hay = normalize_raw_for_match(text)
    if needle and needle in hay:
        return True
    # 括号负数 vs 负号
    alt = needle
    if needle.startswith("(") and needle.endswith(")"):
        alt = "-" + needle[1:-1]
    elif needle.startswith("-"):
        alt = f"({needle[1:]})"
    return bool(alt) and alt in hay


@dataclass
class ParsedAmount:
    raw: str | None
    unit: str | None
    value: float | None
    source_quote: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, *, is_eps: bool = False) -> "ParsedAmount":
        if not data:
            return cls(None, None, None, None)
        raw = data.get("raw")
        unit = data.get("unit")
        value = apply_unit(parse_number(raw), unit, is_eps=is_eps)
        return cls(raw=raw, unit=unit, value=value, source_quote=data.get("source_quote"))
