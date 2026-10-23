"""ADR 新闻稿常同时给 NT$ 与 US$。reporting_currency=USD 时不能把台币当成美元。"""

from __future__ import annotations

import re
from typing import Any


_FOREIGN = re.compile(r"NT\$|\bNTD\b|\bTWD\b|新台币", re.IGNORECASE)
_USD = re.compile(r"US\$|\bUSD\b|U\.S\.\s*dollars|in US dollars", re.IGNORECASE)

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
        )
    return f"\n\nreporting_currency={currency}。raw 只填该币种金额。\n"


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
    for key in _AMOUNT_KEYS:
        node = extracted.get(key)
        if isinstance(node, dict):
            _clear_amount(node, warnings, key)
    kpis = extracted.get("kpis")
    if isinstance(kpis, dict):
        for key, node in kpis.items():
            if isinstance(node, dict):
                _clear_amount(node, warnings, f"kpis.{key}")
    return warnings
