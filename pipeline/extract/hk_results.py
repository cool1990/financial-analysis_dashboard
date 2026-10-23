"""港股业绩公告（文本）→ ExtractFinancials 形状的字典。

规则见 prompts/extract_financials_hk.md。优先抄三个月单季表；
中报 / 年报没有单季列时抛 HkCumulativeOnly，由调用方决定是否残差。
每股盈利不做累计相减。
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from pipeline.extract.hk_statement import HkCumulativeOnly, HkImportError, parse_hk_quarter
from pipeline.extract.hk_text import parse_cn_date, read_pdf_text

__all__ = ["HkCumulativeOnly", "HkImportError", "extract_hk_results", "load_press_text", "pdf_to_text"]

_FCF = re.compile(r"自由现金流为人民币\s*([\d,]+(?:\.\d+)?)\s*亿元")
_IFRS_EPS = re.compile(r"每股摊薄盈利为人民币\s*([\d.]+)\s*元")


def pdf_to_text(path: Path) -> str:
    text = read_pdf_text(path)
    if len(text.strip()) < 200:
        raise HkImportError(f"PDF 抽不出足够文本: {path}")
    return text


def load_press_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return pdf_to_text(path)
    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8")
    raise HkImportError(f"不认识的业绩公告文件: {path.name}")


def _release_day(text: str) -> date | None:
    matched = re.search(r"香港，(.{0,30}?日)", text)
    if matched:
        return parse_cn_date(matched.group(1))
    matched = re.search(r"Hong Kong,\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})", text, re.I)
    if not matched:
        return None
    from pipeline.extract.hk_text import parse_en_date

    return parse_en_date(matched.group(1))


def _ifrs_eps_sentence(text: str) -> str | None:
    """国际财务报告准则摘要里的摊薄每股盈利，用来和收益表核对。"""
    for start in ("按国际财务报告准则", "按国际财务报告准则："):
        at = text.find(start)
        if at < 0:
            continue
        matched = _IFRS_EPS.search(text[at:at + 800])
        if matched:
            return matched.group(1)
    return None


def extract_hk_results(
    text: str,
    fiscal_period: str,
    *,
    fiscal_year_end_month: int = 12,
) -> dict[str, Any]:
    """从业绩公告全文抽出本期字段。口径不对时抛 HkImportError，不写单季。"""
    parsed = parse_hk_quarter(text, fiscal_period, fiscal_year_end_month=fiscal_year_end_month)
    rows: dict[str, Any] = parsed["rows"]
    warnings: list[str] = []
    if "yoy" not in parsed["roles"] and not (rows.get("revenue") or {}).get("year_ago_raw"):
        warnings.append("收益表没有去年同季列，同比留空")
    if rows.get("eps_gaap") is None:
        warnings.append("收益表没有摊薄每股盈利行")
    ifrs_eps = _ifrs_eps_sentence(text)
    gaap = rows.get("eps_gaap")
    if gaap and ifrs_eps and gaap["raw"] != ifrs_eps:
        warnings.append(
            f"收益表摊薄 EPS {gaap['raw']} 与国际财务报告准则摘要 {ifrs_eps} 不一致，采用收益表"
        )

    kpis: dict[str, Any] = {}
    for key in (
        "vas_revenue",
        "advertising_revenue",
        "fintech_biz_revenue",
        "selling_expense",
        "ga_expense",
        "rd_expense",
        "non_ifrs_operating_profit",
    ):
        node = rows.get(key)
        if node:
            kpis[key] = node
    fcf_matched = _FCF.search(parsed["prepared_text"])
    if fcf_matched is None:
        fcf_matched = _FCF.search(text)
    if fcf_matched:
        kpis["free_cash_flow"] = {
            "raw": fcf_matched.group(1),
            "unit": "亿元",
            "currency": "CNY",
            "period_type": "3m",
            "months": 3,
            "source_quote": fcf_matched.group(0)[:200],
        }
        warnings.append("自由现金流只记入 kpis.free_cash_flow；公告未单列经营现金流，不写入 financials.fcf")
    else:
        warnings.append("公告未单列经营现金流，operating_cash_flow 与 fcf 留空")

    release = _release_day(text) or _release_day(parsed["prepared_text"])
    end: date = parsed["period_end"]
    revenue = rows["revenue"]
    return {
        "period_label": fiscal_period,
        "period_end_date": end.isoformat(),
        "release_date": release.isoformat() if release else None,
        "period_type": "3m",
        "currency": "CNY",
        "column_labels": parsed["labels"],
        "column_roles": parsed["roles"],
        "extract_mode": parsed["extract_mode"],
        "table_mode": parsed["mode"],
        "revenue": revenue,
        "cost_of_revenue": rows.get("cost_of_revenue"),
        "gross_profit_gaap": rows.get("gross_profit"),
        "gross_profit_nongaap": None,
        "rd_expense": rows.get("rd_expense"),
        "operating_income_gaap": rows.get("operating_income"),
        "operating_income_nongaap": rows.get("non_ifrs_operating_profit"),
        "net_income_gaap": rows.get("net_income"),
        "eps_gaap_diluted": rows.get("eps_gaap"),
        "eps_nongaap_diluted": rows.get("eps_nongaap"),
        "has_nongaap_eps": rows.get("eps_nongaap") is not None,
        "capex_gross": rows.get("capex"),
        "operating_cash_flow": None,
        "kpis": kpis,
        "prior_year_comparables": {
            "revenue": {"raw": revenue.get("year_ago_raw"), "unit": "millions"}
        },
        "warnings": warnings,
    }
