"""从新闻稿表格行推出上季 / 去年同季数值，用于同比、环比。不调用 LLM。

抽取结果里每个数字都带 source_quote，即新闻稿表格的整行，例如：
    "Revenue $ 54,229 $ 41,456 $ 11,315 $ 54,229 $ 41,456 $ 11,315"
列顺序因公司而异（本季 / 上季 / 去年同季，或只有本季 / 去年同季）。
LLM 已经单独给出了去年同季的营收（prior_year_comparables.revenue），
用它在营收行里定位「去年同季」所在列，即可确定版式，再套用到其它行。
每行都要求第一个数字等于本季抽取值，否则不使用该行。
"""

from __future__ import annotations

import re
from typing import Any

from pipeline.extract.numbers import ParsedAmount, parse_number

_TOKEN_RE = re.compile(r"\(?\$?\s*\d[\d,]*(?:\.\d+)?\)?")

# ExtractFinancials 字段 → 是否为 EPS
FIELDS = {
    "revenue": False,
    "gross_profit_gaap": False,
    "gross_profit_nongaap": False,
    "operating_income_gaap": False,
    "operating_income_nongaap": False,
    "eps_gaap_diluted": True,
    "eps_nongaap_diluted": True,
    "operating_cash_flow": False,
    "capex_gross": False,
    "capex_net": False,
}


def row_numbers(quote: str | None) -> list[float]:
    """按出现顺序解析一行里的数字；括号视为负数。"""
    out: list[float] = []
    for tok in _TOKEN_RE.findall(quote or ""):
        val = parse_number(tok.replace(" ", ""))
        if val is not None:
            out.append(val)
    return out


def _close(a: float, b: float) -> bool:
    return abs(abs(a) - abs(b)) <= max(1e-9, 1e-6 * abs(b))


def _cells_from_anchor(item: dict[str, Any] | None) -> list[float] | None:
    """从本季数值所在位置开始的数字序列；找不到本季数值则返回 None。"""
    if not item or not item.get("raw") or not item.get("source_quote"):
        return None
    current = parse_number(item["raw"])
    if current is None:
        return None
    nums = row_numbers(item["source_quote"])
    for i, n in enumerate(nums):
        if _close(n, current):
            return nums[i:]
    return None


def detect_layout(extracted: dict[str, Any]) -> dict[str, int] | None:
    """返回 {"yoy": 列号, "qoq": 列号}（相对本季列的偏移）；无法确定时返回 None。"""
    cells = _cells_from_anchor(extracted.get("revenue"))
    prior = (extracted.get("prior_year_comparables") or {}).get("revenue") or {}
    year_ago = parse_number(prior.get("raw")) if prior.get("raw") else None
    if not cells or year_ago is None:
        return None
    for idx in (1, 2):
        if idx < len(cells) and _close(cells[idx], year_ago):
            return {"yoy": idx, "qoq": 1} if idx == 2 else {"yoy": 1}
    return None


def press_release_comparatives(extracted: dict[str, Any]) -> dict[str, dict[str, float | None]]:
    """{字段: {"prior_q": 值, "year_ago": 值}}，数值已按单位换算（与本季口径一致）。"""
    layout = detect_layout(extracted)
    if not layout:
        return {}
    out: dict[str, dict[str, float | None]] = {}
    for field, is_eps in FIELDS.items():
        item = extracted.get(field)
        cells = _cells_from_anchor(item)
        if not cells or (item or {}).get("period_type") == "ytd":
            continue
        row: dict[str, float | None] = {}
        for name, key in (("year_ago", "yoy"), ("prior_q", "qoq")):
            idx = layout.get(key)
            if idx is None or idx >= len(cells):
                row[name] = None
                continue
            raw = cells[idx]
            # 亏损等负值要保留符号；资本开支在表里常写成 (11,110)，统一取正
            if field.startswith("capex"):
                raw = abs(raw)
            parsed = ParsedAmount.from_dict({"raw": repr(raw), "unit": (item or {}).get("unit")}, is_eps=is_eps)
            row[name] = parsed.value
        out[field] = row
    return out


def comparative_base(
    comps: dict[str, dict[str, float | None]],
    which: str,
    numer: str,
    denom: str | None = None,
) -> float | None:
    """取上季/去年同季的数值；给 denom 时返回比率（如毛利 / 营收）。"""
    a = (comps.get(numer) or {}).get(which)
    if denom is None:
        return a
    b = (comps.get(denom) or {}).get(which)
    if a is None or not b:
        return None
    return a / b


def _fcf(comps: dict[str, dict[str, float | None]], which: str, capex_field: str) -> float | None:
    ocf = comparative_base(comps, which, "operating_cash_flow")
    capex = comparative_base(comps, which, capex_field)
    if ocf is None or capex is None:
        return None
    return ocf - abs(capex)


def apply_press_comparatives(
    financials: dict[str, Any],
    extracted: dict[str, Any],
    *,
    capex_definition: str = "gross",
) -> list[str]:
    """用新闻稿对比列补齐 financials 中缺失的同比 / 环比，返回被补齐的指标名。

    已有值（来自历史季度 JSON）优先，不覆盖。上季 / 去年同季原值写入
    prior_q / year_ago，供页面画趋势图。
    """
    from pipeline.compute.metrics import pct_change, pp_change

    comps = press_release_comparatives(extracted)
    if not comps:
        return []
    capex_field = "capex_net" if capex_definition == "net" and "capex_net" in comps else "capex_gross"
    spec: dict[str, tuple[str, str | None]] = {
        "revenue": ("revenue", None),
        "gross_margin_gaap": ("gross_profit_gaap", "revenue"),
        "gross_margin_nongaap": ("gross_profit_nongaap", "revenue"),
        "operating_margin_gaap": ("operating_income_gaap", "revenue"),
        "operating_margin_nongaap": ("operating_income_nongaap", "revenue"),
        "eps_gaap": ("eps_gaap_diluted", None),
        "eps_nongaap": ("eps_nongaap_diluted", None),
        "operating_cash_flow": ("operating_cash_flow", None),
        "capex": (capex_field, None),
    }
    filled: list[str] = []
    for key, block in financials.items():
        if not isinstance(block, dict) or "value" not in block:
            continue
        if key == "fcf":
            bases = {w: _fcf(comps, w, capex_field) for w in ("year_ago", "prior_q")}
        elif key in spec:
            numer, denom = spec[key]
            bases = {w: comparative_base(comps, w, numer, denom) for w in ("year_ago", "prior_q")}
        else:
            continue
        is_ratio = key.startswith(("gross_margin", "operating_margin"))
        changed = False
        for which, base in bases.items():
            if base is None:
                continue
            block.setdefault(which, base)
            prefix = "yoy" if which == "year_ago" else "qoq"
            field = f"{prefix}_pp" if is_ratio else f"{prefix}_pct"
            if block.get(field) is None:
                val = pp_change(block.get("value"), base) if is_ratio else pct_change(block.get("value"), base)
                if val is not None:
                    block[field] = val
                    changed = True
        if changed:
            block["comparatives_source"] = "press_release"
            filled.append(key)
    return filled
