from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from pipeline import ROOT
from pipeline.compute.periods import build_period_info
from pipeline.config import data_dir, load_ticker_config, openrouter_api_key
from pipeline.extract.numbers import ParsedAmount
from pipeline.extract.press_release import load_press_release
from pipeline.llm import LLMClient, LLMError, get_run_tracker, reset_run_tracker
from pipeline.schemas import ExtractFinancials, GuidanceItem, QAItem
from pipeline.sources.sec_edgar import SecEdgarClient, extract_quarterly_gaap_eps, extract_quarterly_revenue
from pipeline.state import empty_period_doc, save_period_json


def backfill(
    ticker: str,
    from_period: str | None = None,
    limit: int = 8,
    *,
    dry_run: bool = False,
    model: str | None = None,
) -> dict[str, Any]:
    """历史回补：GAAP 优先 XBRL；Non-GAAP/KPI 走新闻稿抽取。

    dry_run=True 时不调用 LLM，只统计将发生的调用与粗略 token/花费。
    """
    cfg = load_ticker_config(ticker)
    if not cfg.get("cik"):
        raise RuntimeError("请先运行 init")
    client = SecEdgarClient()
    facts = client.company_facts(cfg["cik"])
    revs = extract_quarterly_revenue(facts)
    eps = extract_quarterly_gaap_eps(facts)
    by_end = {r["end"]: r for r in revs}
    written: list[str] = []

    for row in eps[:limit]:
        end = row["end"]
        info = build_period_info(end, cfg["fiscal_year_end_month"], row.get("start"))
        period = info.fiscal_period
        if from_period and period < from_period:
            continue
        if dry_run:
            written.append(period)
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
            "revenue": {
                "value": rev.get("val") if rev else None,
                "yoy_pct": None,
                "qoq_pct": None,
                "yoy_pp": None,
                "qoq_pp": None,
                "benchmark": {"value": None, "source": "none", "diff": None, "diff_pct": None, "diff_pp": None},
                "source_quote": "xbrl",
            },
            "eps_gaap": {
                "value": row.get("val"),
                "yoy_pct": None,
                "qoq_pct": None,
                "yoy_pp": None,
                "qoq_pp": None,
                "benchmark": {"value": None, "source": "none", "diff": None, "diff_pct": None, "diff_pp": None},
                "source_quote": "xbrl",
            },
        }
        doc["status"]["stage"] = "backfill_gaap"
        doc["status"]["warnings"].append("历史回补：仅 GAAP XBRL，Non-GAAP/KPI 需新闻稿补全")
        save_period_json(ticker, period, doc)
        written.append(period)

    filings = client.find_earnings_8k(cfg["cik"])[:limit]
    llm_calls = 0
    est_prompt_tokens = 0
    llm = None

    for filing in filings:
        try:
            raw = data_dir(ticker) / "raw" / "_backfill" / filing["accessionNumber"].replace("-", "") / "press_release.html"
            if not dry_run or not raw.exists():
                client.download_press_release(cfg["cik"], filing["accessionNumber"], raw)
            parsed = load_press_release(raw)
            text_len = len(parsed.get("combined") or "")
            # 粗略：约 4 字符/token + prompt 开销
            est_prompt_tokens += text_len // 4 + 800
            llm_calls += 1
            if dry_run:
                continue
            if llm is None:
                llm = LLMClient(model=model, use_cache=True, require_key=True)
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
                doc = empty_period_doc(
                    ticker,
                    period,
                    calendar_quarter=info.calendar_quarter,
                    period_start=info.period_start.isoformat(),
                    period_end=info.period_end.isoformat(),
                    eps_basis=cfg.get("eps_basis"),
                )
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

    pricing = None
    est_cost = None
    if dry_run:
        try:
            from pipeline.llm import find_model_info, pricing_from_model_info

            slug = model or __import__("pipeline.config", fromlist=["load_settings"]).load_settings()["llm"]["model"]
            pricing = pricing_from_model_info(find_model_info(slug))
            if pricing:
                # 假设输出约 1500 token / 次
                est_cost = llm_calls * (
                    est_prompt_tokens / max(llm_calls, 1) * pricing.get("prompt", 0)
                    + 1500 * pricing.get("completion", 0)
                )
        except Exception:
            pricing = None

    return {
        "written": written if not dry_run else [],
        "dry_run": dry_run,
        "would_write_gaap_periods": written if dry_run else written,
        "llm_calls": llm_calls,
        "est_prompt_tokens": est_prompt_tokens,
        "est_completion_tokens_assumed": llm_calls * 1500,
        "est_cost_usd": None if est_cost is None else round(est_cost, 4),
        "pricing": pricing,
    }
