from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from pipeline import ROOT
from pipeline.compute.periods import build_period_info
from pipeline.config import data_dir, is_hk_listing, load_ticker_config, openrouter_api_key
from pipeline.earnings_config import resolve_earnings_spec
from pipeline.extract.numbers import ParsedAmount
from pipeline.extract.press_release import load_press_release
from pipeline.llm import LLMClient, LLMError, get_run_tracker, reset_run_tracker
from pipeline.schemas import ExtractFinancials, GuidanceItem, QAItem
from pipeline.compute.metrics import build_metric_block
from pipeline.sources.sec_edgar import SecEdgarClient, extract_quarterly_gaap_eps, extract_quarterly_revenue
from pipeline.sources.xbrl_history import quarterly_statements
from pipeline.state import empty_period_doc, load_period_json, save_period_json


_PROTECTED_STAGES = {"stage1_done", "stage2_done", "stage3_done", "transcript_found"}


def _block(node: dict[str, Any] | None, *, yoy: float | None, qoq: float | None, is_ratio: bool = False) -> dict[str, Any] | None:
    if not node or node.get("value") is None:
        return None
    block = build_metric_block(
        node["value"],
        yoy_base=yoy,
        qoq_base=qoq,
        is_ratio=is_ratio,
        source_quote=node.get("source_quote"),
    )
    block["comparatives_source"] = "xbrl"
    return block


def write_xbrl_history(ticker: str, *, start: str = "2016-01-01") -> dict[str, Any]:
    """用 companyfacts 回补单季 GAAP。不覆盖已经跑过新闻稿阶段的季度。"""
    cfg = load_ticker_config(ticker)
    if is_hk_listing(cfg):
        raise RuntimeError(f"{ticker} listing=hk，不用 SEC companyfacts 回补")
    if not cfg.get("cik"):
        raise RuntimeError("请先运行 init")
    start_d = date.fromisoformat(start)
    facts = SecEdgarClient().company_facts(cfg["cik"])
    series = quarterly_statements(facts)
    ends = sorted(end for end in series if end >= start_d)
    by_end = {end: series[end] for end in ends}
    written: list[str] = []
    skipped: list[str] = []
    eps_gaps: list[str] = []
    fye = int(cfg["fiscal_year_end_month"])

    def _val(end: date | None, key: str) -> float | None:
        if end is None or end not in by_end:
            return None
        node = by_end[end].get(key)
        if not node:
            return None
        return node.get("value")

    for end in ends:
        info = build_period_info(end, fye)
        period = info.fiscal_period
        existing = load_period_json(ticker, period)
        stage = ((existing or {}).get("status") or {}).get("stage")
        if existing and stage in _PROTECTED_STAGES:
            skipped.append(period)
            continue
        row = by_end[end]
        yoy_end = _match_end(ends, end.year - 1, end.month)
        prior_cands = [e for e in ends if e < end]
        prior_end = prior_cands[-1] if prior_cands else None
        # 只把紧邻的上一季当环比，避免中间缺季时跨季相减
        if prior_end is not None and (end - prior_end).days > 120:
            prior_end = None

        fin: dict[str, Any] = {}
        rev = _block(row.get("revenue"), yoy=_val(yoy_end, "revenue"), qoq=_val(prior_end, "revenue"))
        if rev:
            fin["revenue"] = rev
        gm = _block(
            row.get("gross_margin"),
            yoy=_val(yoy_end, "gross_margin"),
            qoq=_val(prior_end, "gross_margin"),
            is_ratio=True,
        )
        if gm:
            fin["gross_margin_gaap"] = gm
        oi = _block(row.get("operating_income"), yoy=_val(yoy_end, "operating_income"), qoq=_val(prior_end, "operating_income"))
        if oi:
            fin["operating_income_gaap"] = oi
        om = _block(
            row.get("operating_margin"),
            yoy=_val(yoy_end, "operating_margin"),
            qoq=_val(prior_end, "operating_margin"),
            is_ratio=True,
        )
        if om:
            fin["operating_margin_gaap"] = om
        ni = _block(row.get("net_income"), yoy=_val(yoy_end, "net_income"), qoq=_val(prior_end, "net_income"))
        if ni:
            fin["net_income_gaap"] = ni
        eps = _block(row.get("eps"), yoy=_val(yoy_end, "eps"), qoq=_val(prior_end, "eps"))
        if eps:
            fin["eps_gaap"] = eps
        elif end.month == fye:
            eps_gaps.append(period)
        ocf = _block(row.get("ocf"), yoy=_val(yoy_end, "ocf"), qoq=_val(prior_end, "ocf"))
        if ocf:
            fin["operating_cash_flow"] = ocf
        capex = _block(row.get("capex"), yoy=_val(yoy_end, "capex"), qoq=_val(prior_end, "capex"))
        if capex:
            fin["capex"] = capex
        fcf = _block(row.get("fcf"), yoy=_val(yoy_end, "fcf"), qoq=_val(prior_end, "fcf"))
        if fcf:
            fin["fcf"] = fcf
        if not fin:
            continue
        doc = empty_period_doc(
            ticker,
            period,
            calendar_quarter=info.calendar_quarter,
            period_start=info.period_start.isoformat(),
            period_end=info.period_end.isoformat(),
            eps_basis=cfg.get("eps_basis") or "gaap",
        )
        doc["financials"] = fin
        warnings = [
            "历史回补：GAAP 来自 SEC companyfacts（data.sec.gov/api/xbrl/companyfacts）。"
            "收入和利润优先用 80–100 天的单季事实；没有单季事实时用同一财年起点的累计值相减。"
            "经营现金流和资本开支多为年初至今，单季由相邻累计相减。自由现金流 = 经营现金流 − 资本开支（购建固定资产，取绝对值）。"
            "毛利率 =（营收 − 营业成本）/ 营收，营业成本用 CostOfRevenue。"
            "第四季稀释每股收益没有单季 XBRL 事实，未用全年减九个月倒挤。"
        ]
        doc["status"] = {"stage": "backfill_gaap", "needs_review": False, "warnings": warnings}
        doc["drivers"] = {"stage": 0, "metrics": [], "status": "历史回补不含变动原因"}
        save_period_json(ticker, period, doc)
        written.append(period)
    return {"written": written, "skipped": skipped, "eps_gaps": eps_gaps, "start": start}


def _match_end(ends: list[date], year: int, month: int) -> date | None:
    cands = [e for e in ends if e.year == year and e.month == month]
    if not cands:
        return None
    return cands[-1]


def _backfill_adr_press_releases(
    ticker: str,
    cfg: dict[str, Any],
    *,
    from_period: str | None,
    limit: int,
    dry_run: bool,
    model: str | None,
) -> dict[str, Any]:
    """用业绩 6-K 的 Press release 走现有 Stage1。不写 us-gaap companyfacts。"""
    import os

    from pipeline.commands.run_cmd import _run_stage1_filing

    if model:
        os.environ["PIPELINE_LLM_MODEL"] = model

    if not cfg.get("cik"):
        raise RuntimeError("请先运行 init")
    client = SecEdgarClient()
    hits = client.scan_earnings_filings(cfg["cik"], cfg, limit=limit).hits
    spec = resolve_earnings_spec(cfg)
    written: list[str] = []
    skipped: list[str] = []
    errors: list[dict[str, str]] = []
    filings: list[dict[str, Any]] = []
    llm_calls = 0
    est_prompt_tokens = 0
    for filing in hits:
        acc = filing["accessionNumber"]
        filings.append(
            {
                "accessionNumber": acc,
                "acceptanceDateTime": filing.get("acceptanceDateTime"),
                "primaryDocument": filing.get("primaryDocument"),
                "press_release_file": filing.get("press_release_file"),
                "press_description": filing.get("press_description"),
            }
        )
        raw = data_dir(ticker) / "raw" / "_backfill" / acc.replace("-", "") / "press_release.html"
        try:
            if not dry_run or not raw.exists():
                client.download_press_release(
                    cfg["cik"],
                    acc,
                    raw,
                    press_release_file=filing.get("press_release_file") or None,
                )
            parsed = load_press_release(raw)
            text_len = len(parsed.get("combined") or "")
            est_prompt_tokens += text_len // 4 + 800
            llm_calls += 1
            if dry_run:
                continue
            doc = _run_stage1_filing(ticker, cfg, client, filing, None)
            period = doc["meta"]["fiscal_period"]
            if from_period and period < from_period:
                skipped.append(period)
                continue
            if period not in written:
                written.append(period)
        except Exception as exc:
            errors.append({"accessionNumber": acc, "error": str(exc)})
    return {
        "listing": "adr",
        "source": "sec_6k_press_release",
        "reporting_currency": spec.reporting_currency,
        "accounting_basis": spec.accounting_basis,
        "xbrl_used": False,
        "written": written,
        "skipped": skipped,
        "errors": errors,
        "filings": filings,
        "dry_run": dry_run,
        "llm_calls": llm_calls,
        "est_prompt_tokens": est_prompt_tokens,
        "note": "ADR 历史只走 SEC 6-K 新闻稿。未使用 us-gaap companyfacts，也未使用 TWSE。",
    }


def backfill(
    ticker: str,
    from_period: str | None = None,
    limit: int = 8,
    *,
    dry_run: bool = False,
    model: str | None = None,
    xbrl_only: bool = False,
    start: str = "2016-01-01",
) -> dict[str, Any]:
    """历史回补。

    国内：GAAP 优先 us-gaap XBRL，Non-GAAP/KPI 再走 8-K 新闻稿。
    ADR：只走 6-K 新闻稿。不用 us-gaap companyfacts 当主历史，也不接 TWSE。
    dry_run=True 时不调用 LLM，只统计将发生的调用与粗略 token/花费。
    xbrl_only=True 时只写 companyfacts 单季 GAAP，不调用模型（ADR 会拒绝）。
    """
    cfg = load_ticker_config(ticker)
    if is_hk_listing(cfg):
        raise RuntimeError(f"{ticker} listing=hk，不用 SEC 回补")
    spec = resolve_earnings_spec(cfg)
    if spec.listing == "adr":
        if xbrl_only:
            raise RuntimeError(
                "ADR 不以 us-gaap XBRL / companyfacts 作为主历史。"
                f"请用 6-K 新闻稿回补：python -m pipeline backfill --ticker {ticker.upper()} --limit {limit}"
            )
        return _backfill_adr_press_releases(
            ticker,
            cfg,
            from_period=from_period,
            limit=limit,
            dry_run=dry_run,
            model=model,
        )
    if xbrl_only:
        out = write_xbrl_history(ticker, start=start)
        try:
            from pipeline.commands.history_cmd import refresh_history

            out["history"] = refresh_history(ticker, start=start)
        except Exception as exc:
            out["history"] = {"ticker": ticker.upper(), "skipped": False, "ok": False, "error": str(exc)}
        return out
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
