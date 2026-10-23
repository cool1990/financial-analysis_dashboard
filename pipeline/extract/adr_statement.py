"""从 ADR 业绩新闻稿抽出官方美元科目。

只认新闻稿写明的 US$ / $ 金额，以及 US$ per ADR。
台币科目没有美元对照时留空，不按汇率换算。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from pipeline.extract.currency_guard import (
    find_adr_diluted_eps,
    find_official_usd_amount,
    stated_margin_ratios,
)
from pipeline.extract.numbers import parse_number


_QUARTER_ENDED = re.compile(
    r"(?:quarter|three months)\s+ended\s+"
    r"(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),\s+(?P<year>\d{4})",
    re.IGNORECASE,
)
_SCALE = {
    "billion": 1_000_000_000,
    "million": 1_000_000,
    "thousand": 1_000,
}


def _scale(raw: str, unit: str | None) -> float | None:
    number = parse_number(raw)
    if number is None:
        return None
    key = (unit or "").lower().rstrip("s")
    return number * _SCALE.get(key, 1)


def period_end_from_press(text: str) -> date | None:
    """业绩稿首段的 quarter ended。指引句没有 ended，不会被当成本期。"""
    match = _QUARTER_ENDED.search(text or "")
    if match is None:
        return None
    try:
        parsed = datetime.strptime(
            f"{match.group('month')} {int(match.group('day'))} {match.group('year')}",
            "%B %d %Y",
        )
    except ValueError:
        return None
    return parsed.date()


def _official_amount(text: str, key: str) -> dict[str, Any] | None:
    found = find_official_usd_amount(text, key)
    if not found:
        return None
    value = _scale(found["raw"], found.get("unit"))
    if value is None:
        return None
    return {"value": value, "source_quote": found["quote"], "unit": found.get("unit")}


def parse_adr_press_statement(text: str) -> dict[str, Any]:
    """返回本期结束日和已写明的美元科目。解析不到的键不出现。"""
    body = text or ""
    out: dict[str, Any] = {"period_end": None, "amounts": {}, "margins": {}, "warnings": []}
    end = period_end_from_press(body)
    out["period_end"] = end.isoformat() if end else None
    for key in ("revenue", "net_income_gaap", "gross_profit_gaap", "operating_income_gaap", "capex_gross"):
        amount = _official_amount(body, key)
        if amount:
            out["amounts"][key] = amount
    eps = find_adr_diluted_eps(body)
    if eps:
        raw, quote = eps
        value = parse_number(raw)
        if value is not None:
            out["amounts"]["eps_gaap"] = {"value": value, "source_quote": quote, "unit": "per_adr"}
    margins = stated_margin_ratios(body)
    if margins:
        out["margins"] = margins
    if "revenue" not in out["amounts"]:
        out["warnings"].append("新闻稿没有写明本期美元营收")
    if "eps_gaap" not in out["amounts"]:
        out["warnings"].append("新闻稿没有写明 US$ per ADR，每股收益留空")
    if "net_income_gaap" not in out["amounts"]:
        out["warnings"].append("净利润没有美元对照，留空，不按汇率换算")
    return out
