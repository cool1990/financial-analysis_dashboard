"""ADR 新闻稿常同时给 NT$ 与 US$。reporting_currency=USD 时不能把台币当成美元。

稀释 EPS 若写成 NT$x.xx (US$y.yy per ADR)，采用官方 ADR 美元，不用汇率把普通股 EPS 换算。
"""

from __future__ import annotations

import re
from typing import Any

from pipeline.extract.numbers import parse_number


_FOREIGN = re.compile(r"NT\$|\bNTD\b|\bTWD\b|新台币", re.IGNORECASE)
_USD = re.compile(r"US\$|\bUSD\b|U\.S\.\s*dollars|in US dollars", re.IGNORECASE)
# 台积电等 ADR 新闻稿首段：diluted earnings per share of NT$19.50 (US$3.14 per ADR unit)
_DILUTED_ADR_EPS = re.compile(
    r"diluted earnings per share of NT\$\s*[\d,]+(?:\.\d+)?"
    r"\s*\(\s*(?P<phrase>US\$\s*(?P<usd>[\d,]+\.\d+)\s+per\s+ADR(?:\s+unit)?)\s*\)"
    r"(?:\s+for\s+the\s+[^.]{0,90})?",
    re.IGNORECASE,
)
_ANY_ADR_EPS = re.compile(
    r"(?P<phrase>US\$\s*(?P<usd>[\d,]+\.\d+)\s+per\s+ADR(?:\s+unit)?)",
    re.IGNORECASE,
)
_NT_EPS = re.compile(r"NT\$\s*([\d,]+(?:\.\d+)?)")
_PER_ADR = re.compile(r"per\s+ADR", re.IGNORECASE)

_AMOUNT_KEYS = (
    "revenue",
    "net_income_gaap",
    "diluted_shares",
    "cost_of_revenue",
    "gross_profit_gaap",
    "gross_profit_nongaap",
    "rd_expense",
    "sga_expense",
    "operating_income_gaap",
    "operating_income_nongaap",
    "eps_gaap_diluted",
    "eps_nongaap_diluted",
    "operating_cash_flow",
    "capex_gross",
    "capex_net",
)


def currency_prompt_note(reporting_currency: str | None) -> str:
    currency = (reporting_currency or "USD").upper()
    if currency == "USD":
        return (
            "\n\nreporting_currency=USD。"
            "若新闻稿同时出现 NT$ 与 US$ / in US dollars，raw 只填美元金额，不要填 NT$。\n"
            "稀释 EPS 若写成 NT$x.xx (US$y.yy per ADR)，eps_gaap_diluted.raw 填 y.yy，"
            "source_quote 必须包含 US$y.yy per ADR。不要用汇率把 NT$ 换算成美元。\n"
        )
    return f"\n\nreporting_currency={currency}。raw 只填该币种金额。\n"


def _clip_quote(quote: str, phrase: str, limit: int = 200) -> str:
    text = re.sub(r"\s+", " ", quote).strip()
    needle = re.sub(r"\s+", " ", phrase).strip()
    if len(text) <= limit and needle.lower() in text.lower():
        return text
    idx = text.lower().find(needle.lower())
    if idx < 0 or len(needle) >= limit:
        return needle[:limit]
    spare = limit - len(needle)
    left = min(idx, spare // 2)
    start = idx - left
    end = min(len(text), start + limit)
    start = max(0, end - limit)
    return text[start:end].strip()


def find_adr_diluted_eps(full_text: str) -> tuple[str, str] | None:
    """官方 ADR 美元稀释 EPS。返回 (raw 数字, 含 US$x.xx per ADR 的引文)。

    优先「diluted earnings per share of NT$… (US$… per ADR)」；没有这句时才用文中第一处 US$ per ADR。
    不根据汇率或 ADR 股数换算。
    """
    if not full_text:
        return None
    match = _DILUTED_ADR_EPS.search(full_text)
    if match is not None:
        usd = match.group("usd").replace(",", "")
        return usd, _clip_quote(match.group(0), match.group("phrase"))
    match = _ANY_ADR_EPS.search(full_text)
    if match is None:
        return None
    usd = match.group("usd").replace(",", "")
    start = max(0, match.start() - 90)
    end = min(len(full_text), match.end() + 60)
    return usd, _clip_quote(full_text[start:end], match.group("phrase"))


def _same_amount(raw: Any, usd: str) -> bool:
    left = parse_number(None if raw is None else str(raw))
    right = parse_number(usd)
    return left is not None and right is not None and abs(left - right) < 1e-6


def _quote_has_adr_usd(quote: str, usd: str) -> bool:
    return bool(
        re.search(rf"US\$\s*{re.escape(usd)}\s+per\s+ADR", quote or "", re.IGNORECASE)
    )


def ordinary_share_eps(node: dict[str, Any] | None) -> float | None:
    """引文同时有 NT$ 普通股 EPS 与 US$ per ADR 时，返回 NT$ 数字。

    净利润和股数通常是台币口径，不能拿去和 ADR 美元 EPS 比。
    """
    if not isinstance(node, dict):
        return None
    quote = str(node.get("source_quote") or "")
    if not _PER_ADR.search(quote):
        return None
    match = _NT_EPS.search(quote)
    if not match:
        return None
    return parse_number(match.group(1))


def _prefer_adr_gaap_eps(extracted: dict[str, Any], full_text: str, warnings: list[str]) -> bool:
    found = find_adr_diluted_eps(full_text)
    if found is None:
        return False
    usd, quote = found
    node = extracted.get("eps_gaap_diluted")
    if not isinstance(node, dict):
        node = {}
        extracted["eps_gaap_diluted"] = node
    already = _same_amount(node.get("raw"), usd) and _quote_has_adr_usd(str(node.get("source_quote") or ""), usd)
    node["raw"] = usd
    node["unit"] = None
    if not _quote_has_adr_usd(str(node.get("source_quote") or ""), usd):
        node["source_quote"] = quote
    if not already:
        warnings.append(
            f"eps_gaap_diluted 采用新闻稿 US${usd} per ADR，优先于 NT$ 普通股 EPS，不做汇率换算"
        )
    return True


def _nt_only(text: str) -> bool:
    return bool(_FOREIGN.search(text or "")) and not _USD.search(text or "")


def _clear_amount(node: dict[str, Any], warnings: list[str], label: str) -> None:
    blob = f"{node.get('raw') or ''} {node.get('source_quote') or ''}"
    if not _nt_only(blob):
        return
    node["raw"] = None
    warnings.append(f"{label} 的引用只有 NT$/TWD、没有美元，已清空以免当成 USD")


def apply_currency_guard(
    extracted: dict[str, Any],
    full_text: str,
    reporting_currency: str | None,
) -> list[str]:
    """返回应写入 status.warnings 的句子，并清掉只有台币的金额。"""
    currency = (reporting_currency or "USD").upper()
    if currency != "USD":
        return []
    warnings: list[str] = []
    if _FOREIGN.search(full_text or ""):
        warnings.append("新闻稿含 NT$/TWD。reporting_currency=USD，未标明美元的金额不能当成 USD。")
    adopted_adr_eps = _prefer_adr_gaap_eps(extracted, full_text or "", warnings)
    for key in _AMOUNT_KEYS:
        if key == "eps_gaap_diluted" and adopted_adr_eps:
            continue
        node = extracted.get(key)
        if isinstance(node, dict):
            _clear_amount(node, warnings, key)
    kpis = extracted.get("kpis")
    if isinstance(kpis, dict):
        for key, node in kpis.items():
            if isinstance(node, dict):
                _clear_amount(node, warnings, f"kpis.{key}")
    return warnings
