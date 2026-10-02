from __future__ import annotations

import json
from datetime import date
from typing import Any

from pipeline.compute.periods import build_period_info, make_fiscal_period
from pipeline.config import data_dir, load_ticker_config
from pipeline.extract.numbers import ParsedAmount
from pipeline.extract.press_release import load_press_release
from pipeline.llm import LLMClient
from pipeline.schemas import ExtractFinancials
from pipeline.sources.sec_edgar import SecEdgarClient, extract_quarterly_gaap_eps, extract_quarterly_revenue
from pipeline.state import empty_period_doc, save_period_json


def backfill(ticker: str, from_period: str | None = None, limit: int = 8) -> list[str]:
    """历史回补：GAAP 优先 XBRL；Non-GAAP/KPI 走新闻稿抽取。"""
    cfg = load_ticker_config(ticker)
    if not cfg.get("cik"):
        raise RuntimeError("请先运行 init")
    client = SecEdgarClient()
    facts = client.company_facts(cfg["cik"])
    revs = extract_quarterly_revenue(facts)
    eps = extract_quarterly_gaap_eps(facts)
    by_end = {r["end"]: r for r in revs}
    written: list[str] = []

    # Map XBRL quarters
    for row in eps[:limit]:
        end = row["end"]
        info = build_period_info(end, cfg["fiscal_year_end_month"], row.get("start"))
        period = info.fiscal_period
        if from_period and period < from_period:
            continue
        doc = empty_period_doc(
            ticker,
            period,
            calendar_quarter=info.calendar_quarter,
            period_start=info.period_start.isoformat(),
            period_end=info.period_end.isoformat(),
            eps_basis=cfg.get("eps_basis"),
        )
        rev = by_end.get(end)
        doc["financials"] = {
            "revenue": {"value": rev.get("val") if rev else None, "yoy": None, "qoq": None, "benchmark": {}, "source_quote": "xbrl"},
            "eps_gaap": {"value": row.get("val"), "yoy": None, "qoq": None, "benchmark": {}, "source_quote": "xbrl"},
        }
        doc["status"]["stage"] = "backfill_gaap"
        doc["status"]["warnings"].append("历史回补：仅 GAAP XBRL，Non-GAAP/KPI 需新闻稿补全")
        save_period_json(ticker, period, doc)
        written.append(period)

    # Attempt recent 8-K nongaap enrichment
    filings = client.find_earnings_8k(cfg["cik"])[:limit]
    llm = None
    for filing in filings:
        try:
            raw = data_dir(ticker) / "raw" / "_backfill" / filing["accessionNumber"].replace("-", "") / "press_release.html"
            client.download_press_release(cfg["cik"], filing["accessionNumber"], raw)
            parsed = load_press_release(raw)
            if llm is None:
                llm = LLMClient()
            prompt = llm.load_prompt("extract_financials.md", kpis=json.dumps(cfg.get("kpis") or [], ensure_ascii=False))
            extracted = llm.complete_json(
                "extract_financials.md",
                prompt + "\n\n----\n" + parsed["combined"][:80000],
                ExtractFinancials,
            ).model_dump()
            end = extracted.get("period_end_date")
            if not end:
                continue
            info = build_period_info(end, cfg["fiscal_year_end_month"])
            period = info.fiscal_period
            path = data_dir(ticker) / f"{period}.json"
            if path.exists():
                doc = json.loads(path.read_text(encoding="utf-8"))
            else:
                doc = empty_period_doc(ticker, period, calendar_quarter=info.calendar_quarter, period_start=info.period_start.isoformat(), period_end=info.period_end.isoformat(), eps_basis=cfg.get("eps_basis"))
            eps_ng = ParsedAmount.from_dict(extracted.get("eps_nongaap_diluted"), is_eps=True)
            if eps_ng.value is not None:
                doc.setdefault("financials", {})["eps_nongaap"] = {
                    "value": eps_ng.value,
                    "source_quote": (extracted.get("eps_nongaap_diluted") or {}).get("source_quote"),
                }
            doc.setdefault("financials", {})["kpis"] = extracted.get("kpis") or {}
            save_period_json(ticker, period, doc)
            if period not in written:
                written.append(period)
        except Exception:
            continue
    return written
