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
    # 已经归一过的 FY2026Q4，或 FY2026 Q4
    re.compile(r"\bFY\s*['’]?(?P<y>\d{4})\s*Q(?P<q>[1-4])\b", re.I),
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
    # fourth quarter 2026 / fourth quarter of 2026（电话会常不写 fiscal）
    re.compile(
        r"\b(?P<qw>first|second|third|fourth)\s+quarter(?:\s+of)?\s+['’]?(?P<y>\d{2}|\d{4})\b",
        re.I,
    ),
]
# 「full year 2026」和「fiscal 2026」是同一期间；calendar year 不在这里，避免和财年并成一条。
_FY_ONLY = re.compile(
    r"^\s*(?:full[\s\-]?year(?:\s+(?:fiscal(?:\s+year)?|fy))?|fiscal(?:\s+year)?|fy)\s*['’]?(?P<y>\d{2}|\d{4})\s*$",
    re.I,
)
_YEAR_ONLY = re.compile(r"^\s*(20\d{2})\s*$")


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
    m = _YEAR_ONLY.match(text)
    if m:
        return f"FY{m.group(1)}"
    return re.sub(r"\s+", " ", text.lower())


# 标准键 → 去重主题。GAAP / Non-GAAP 合成一个主题。
_TOPIC_FAMILY = {
    "revenue": "revenue",
    "gross_margin": "gross_margin",
    "gross_margin_gaap": "gross_margin",
    "gross_margin_nongaap": "gross_margin",
    "operating_margin": "operating_margin",
    "operating_margin_gaap": "operating_margin",
    "operating_margin_nongaap": "operating_margin",
    "opex": "opex",
    "opex_gaap": "opex",
    "opex_nongaap": "opex",
    "eps": "eps",
    "eps_gaap": "eps",
    "eps_nongaap": "eps",
    "share_count": "share_count",
    "tax_rate": "tax_rate",
    "capex": "capex",
    "fcf": "fcf",
    "operating_cash_flow": "operating_cash_flow",
}

# metric_key 为 other 时，用内容而不是中英文标签对齐同一件事。
_OTHER_TOPIC_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("rl_loss", re.compile(r"reality labs", re.I)),
    ("fx", re.compile(r"foreign currency|汇率|外汇|currency headwind", re.I)),
    ("split", re.compile(r"stock split|non-?voting|无投票权|拆股|2-for-1|1\s*拆\s*2", re.I)),
    ("muse_eff", re.compile(r"compute cost|计算成本|cost efficiency", re.I)),
    ("muse_arpu", re.compile(r"revenue per user|每用户", re.I)),
    ("muse_users", re.compile(r"serve billions|数十亿|user growth", re.I)),
    ("muse_profit", re.compile(r"highly profitable|高盈利", re.I)),
    ("ai_return", re.compile(r"long[- ]term potential|长期回报", re.I)),
    ("legal", re.compile(r"scrutiny|监管|青少年", re.I)),
    ("rev_growth", re.compile(r"revenue growth|营收增长|收入增长|强劲增长", re.I)),
    ("oi", re.compile(r"operating income|营业利润(?!率)", re.I)),
    ("pricing", re.compile(r"\bpric(?:e|ing)\b|定价|价格", re.I)),
    ("rd", re.compile(r"\br&d\b|研发", re.I)),
]


def guidance_topic(item: dict[str, Any]) -> str:
    """去重用的主题。同一指标的中英文表述返回同一个主题，不同的事不并在一起。"""
    mk = (item.get("metric_key") or "").strip().lower()
    blob = " ".join(
        str(item.get(name) or "")
        for name in ("metric_key", "metric_label", "statement", "source_quote")
    )
    if mk in _TOPIC_FAMILY:
        # 全年研发增加额有时被标成 opex，不能和运营费用水平值合成一条
        if mk.startswith("opex") and re.search(r"\br&d\b|研发", blob, re.I):
            return "rd"
        return _TOPIC_FAMILY[mk]
    for name, pat in _OTHER_TOPIC_RULES:
        if pat.search(blob):
            return name
    label = re.sub(r"\s+", " ", (item.get("metric_label") or item.get("statement") or "").strip().lower())[:80]
    return f"other:{label}" if label else "other"
