from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pipeline.commands.snapshot_cmd import load_pre_earnings_snapshot
from pipeline.compute.metrics import build_metric_block, ratio, ytd_to_quarterly
from pipeline.compute.periods import build_period_info, prior_fiscal_period, yoy_fiscal_period
from pipeline.compute.scorecard import build_scorecard
from pipeline.config import load_guidance_keys, load_settings, load_ticker_config
from pipeline.extract.numbers import ParsedAmount
from pipeline.extract.press_release import load_press_release
from pipeline.llm import LLMClient
from pipeline.schemas import ExtractFinancials, GuidanceItem
from pipeline.sources.sec_edgar import SecEdgarClient
from pipeline.sources.transcripts import fetch_transcript
from pipeline.sources.yfinance_src import estimate_row
from pipeline.state import (
    add_processed,
    empty_period_doc,
    load_period_json,
    load_processed,
    mark_stage,
    record_error,
    save_period_json,
)
from pipeline.validate import validate_extraction
from pipeline.compute.guidance_review import parse_guidance_item, change_vs_prior, position_in_range
from pipeline.analyze.drivers import analyze_drivers
from pipeline.analyze.qa import structure_qa
from pipeline.analyze.summary import summarize_period
from pipeline.notify import format_scorecard_text, maybe_notify


ET = ZoneInfo("America/New_York")


def _in_window(timing: str, now: datetime | None = None) -> bool:
    settings = load_settings()["polling"]
    now = now or datetime.now(tz=ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    else:
        now = now.astimezone(ET)
    key = "amc_window_et" if timing == "amc" else "bmo_window_et"
    start_s, end_s = settings[key]
    sh, sm = map(int, start_s.split(":"))
    eh, em = map(int, end_s.split(":"))
    minutes = now.hour * 60 + now.minute
    return (sh * 60 + sm) <= minutes <= (eh * 60 + em)


def detect_new_filings(ticker: str) -> list[dict[str, Any]]:
    cfg = load_ticker_config(ticker)
    if not cfg.get("cik"):
        raise RuntimeError(f"{ticker} 尚未 init（缺少 CIK）")
    client = SecEdgarClient()
    processed = load_processed(ticker)
    filings = client.find_earnings_8k(cfg["cik"])
    return [f for f in filings if f["accessionNumber"] not in processed]


def run_stage1(ticker: str, fiscal_period: str | None = None, accession: str | None = None) -> dict[str, Any]:
    cfg = load_ticker_config(ticker)
    client = SecEdgarClient()
    filings = detect_new_filings(ticker)
    if accession:
        filings = [f for f in client.find_earnings_8k(cfg["cik"]) if f["accessionNumber"] == accession]
    if not filings:
        raise RuntimeError("未发现可处理的 8-K Item 2.02")
    filing = filings[0]
    acc = filing["accessionNumber"]
    release_at = filing["acceptanceDateTime"]
    if release_at.endswith("Z") is False and "T" in release_at:
        # SEC format often 2024-09-25T16:05:12.000000000
        release_at = release_at.replace("00000000", "") + ("Z" if not release_at.endswith("Z") else "")

    # period label temporary until extraction
    period = fiscal_period or "PENDING"
    raw_dir = __import__("pipeline.config", fromlist=["data_dir"]).data_dir(ticker) / "raw" / period
    # download first to _tmp then rename after period known
    tmp_dir = __import__("pipeline.config", fromlist=["data_dir"]).data_dir(ticker) / "raw" / "_tmp" / acc.replace("-", "")
    html_path = client.download_press_release(cfg["cik"], acc, tmp_dir / "press_release.html")
    parsed = load_press_release(html_path)
    if parsed.get("is_preliminary"):
        add_processed(ticker, acc)
        raise RuntimeError("检测到业绩预告（preliminary），已跳过")

    # LLM extract
    llm = LLMClient()
    kpis = json.dumps(cfg.get("kpis") or [], ensure_ascii=False)
    prompt = llm.load_prompt("extract_financials.md", kpis=kpis)
    user = prompt + "\n\n----\n" + parsed["combined"][:80000]
    extracted_model = llm.complete_json("extract_financials.md", user, ExtractFinancials)
    extracted = extracted_model.model_dump()

    # regex vs llm nongaap
    warnings: list[str] = []
    regex_flag = parsed.get("has_nongaap_eps_regex")
    if extracted.get("has_nongaap_eps") != regex_flag:
        warnings.append("LLM has_nongaap_eps 与正则不一致，以正则为准")
        extracted["has_nongaap_eps"] = regex_flag

    # determine period
    period_end = extracted.get("period_end_date")
    if not period_end:
        # fallback: use acceptance date quarter approximation — better require LLM
        period_end = release_at[:10]
    info = build_period_info(period_end, cfg["fiscal_year_end_month"])
    period = fiscal_period or info.fiscal_period

    # move raw files
    final_raw = __import__("pipeline.config", fromlist=["data_dir"]).data_dir(ticker) / "raw" / period
    final_raw.mkdir(parents=True, exist_ok=True)
    dest = final_raw / "press_release.html"
    dest.write_bytes(html_path.read_bytes())
    (final_raw / "extracted_financials.json").write_text(
        json.dumps(extracted, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # parse numbers
    revenue = ParsedAmount.from_dict(extracted.get("revenue"))
    eps_gaap = ParsedAmount.from_dict(extracted.get("eps_gaap_diluted"), is_eps=True)
    eps_ng = ParsedAmount.from_dict(extracted.get("eps_nongaap_diluted"), is_eps=True)
    gp_gaap = ParsedAmount.from_dict(extracted.get("gross_profit_gaap"))
    gp_ng = ParsedAmount.from_dict(extracted.get("gross_profit_nongaap"))
    oi_gaap = ParsedAmount.from_dict(extracted.get("operating_income_gaap"))
    oi_ng = ParsedAmount.from_dict(extracted.get("operating_income_nongaap"))
    ocf = ParsedAmount.from_dict(extracted.get("operating_cash_flow"))
    capex_g = ParsedAmount.from_dict(extracted.get("capex_gross"))
    capex_n = ParsedAmount.from_dict(extracted.get("capex_net"))

    basis = cfg.get("eps_basis_override") or cfg.get("eps_basis") or "unknown"
    if basis == "gaap":
        eps_actual = eps_gaap.value
    elif basis == "non_gaap":
        eps_actual = eps_ng.value
        if eps_actual is None:
            eps_actual = eps_gaap.value
            warnings.append("本季未披露 Non-GAAP EPS，改用 GAAP")
    else:
        eps_actual = eps_ng.value or eps_gaap.value
        warnings.append("eps_basis=unknown，记分卡待核对")

    # estimates from pre-earnings snapshot
    snap = load_pre_earnings_snapshot(ticker, release_at if "T" in release_at else release_at + "T00:00:00Z")
    rev_est = eps_est = None
    n_analysts = None
    next_rev = next_eps = None
    if snap:
        er = estimate_row(snap.get("revenue_estimate") or {}, "0q")
        ee = estimate_row(snap.get("earnings_estimate") or {}, "0q")
        if er:
            rev_est = er.get("avg")
            n_analysts = er.get("numberOfAnalysts")
        if ee:
            eps_est = ee.get("avg")
            n_analysts = n_analysts or ee.get("numberOfAnalysts")
        nr = estimate_row(snap.get("revenue_estimate") or {}, "+1q")
        ne = estimate_row(snap.get("earnings_estimate") or {}, "+1q")
        next_rev = nr.get("avg") if nr else None
        next_eps = ne.get("avg") if ne else None

    # margins
    gm_gaap = ratio(gp_gaap.value, revenue.value)
    gm_ng = ratio(gp_ng.value, revenue.value)
    om_gaap = ratio(oi_gaap.value, revenue.value)
    om_ng = ratio(oi_ng.value, revenue.value)
    primary_gm = gm_ng if basis == "non_gaap" and gm_ng is not None else gm_gaap
    primary_om = om_ng if basis == "non_gaap" and om_ng is not None else om_gaap

    # FCF
    capex_def = cfg.get("capex_definition") or "gross"
    capex_val = capex_n.value if capex_def == "net" and capex_n.value is not None else capex_g.value
    # YTD handling for cash flow
    ocf_val = ocf.value
    if ocf.raw and (extracted.get("operating_cash_flow") or {}).get("period_type") == "ytd":
        prior = load_period_json(ticker, prior_fiscal_period(period))
        prior_ocf = None
        if prior:
            prior_ocf = (prior.get("financials", {}).get("operating_cash_flow") or {}).get("value")
        ocf_val = ytd_to_quarterly(ocf.value, prior_ocf, is_first_quarter=period.endswith("Q1"))
    fcf = (ocf_val - abs(capex_val)) if ocf_val is not None and capex_val is not None else None

    # historical comps
    yoy_doc = load_period_json(ticker, yoy_fiscal_period(period))
    qoq_doc = load_period_json(ticker, prior_fiscal_period(period))

    def hist(metric_path: str, doc: dict | None) -> float | None:
        if not doc:
            return None
        cur: Any = doc.get("financials") or {}
        for part in metric_path.split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
        if isinstance(cur, dict):
            return cur.get("value")
        return cur if isinstance(cur, (int, float)) else None

    financials = {
        "revenue": build_metric_block(
            revenue.value,
            yoy_base=hist("revenue", yoy_doc),
            qoq_base=hist("revenue", qoq_doc),
            benchmark=rev_est,
            benchmark_source="consensus" if rev_est is not None else "none",
            source_quote=(extracted.get("revenue") or {}).get("source_quote"),
        ),
        "gross_margin_gaap": build_metric_block(gm_gaap, yoy_base=hist("gross_margin_gaap", yoy_doc), qoq_base=hist("gross_margin_gaap", qoq_doc), is_ratio=True),
        "gross_margin_nongaap": build_metric_block(gm_ng, yoy_base=hist("gross_margin_nongaap", yoy_doc), qoq_base=hist("gross_margin_nongaap", qoq_doc), is_ratio=True),
        "operating_margin_gaap": build_metric_block(om_gaap, yoy_base=hist("operating_margin_gaap", yoy_doc), qoq_base=hist("operating_margin_gaap", qoq_doc), is_ratio=True),
        "operating_margin_nongaap": build_metric_block(om_ng, yoy_base=hist("operating_margin_nongaap", yoy_doc), qoq_base=hist("operating_margin_nongaap", qoq_doc), is_ratio=True),
        "eps_gaap": build_metric_block(eps_gaap.value, yoy_base=hist("eps_gaap", yoy_doc), qoq_base=hist("eps_gaap", qoq_doc), benchmark=eps_est if basis == "gaap" else None, benchmark_source="consensus" if basis == "gaap" else "none", source_quote=(extracted.get("eps_gaap_diluted") or {}).get("source_quote")),
        "eps_nongaap": build_metric_block(eps_ng.value, yoy_base=hist("eps_nongaap", yoy_doc), qoq_base=hist("eps_nongaap", qoq_doc), benchmark=eps_est if basis == "non_gaap" else None, benchmark_source="consensus" if basis == "non_gaap" else "none", source_quote=(extracted.get("eps_nongaap_diluted") or {}).get("source_quote")),
        "operating_cash_flow": build_metric_block(ocf_val, yoy_base=hist("operating_cash_flow", yoy_doc), qoq_base=hist("operating_cash_flow", qoq_doc)),
        "capex": build_metric_block(capex_val, yoy_base=hist("capex", yoy_doc), qoq_base=hist("capex", qoq_doc)),
        "fcf": build_metric_block(fcf, yoy_base=hist("fcf", yoy_doc), qoq_base=hist("fcf", qoq_doc)),
        "kpis": extracted.get("kpis") or {},
    }

    # guidance from press release (module C stage1)
    g_prompt = llm.load_prompt("extract_guidance.md", guidance_keys=", ".join(load_guidance_keys()))
    g_user = g_prompt + "\n\n----\n" + parsed["combined"][:80000]
    try:
        g_items_models = llm.complete_json_list("extract_guidance.md", g_user, GuidanceItem)
        guidance_items = [parse_guidance_item(m.model_dump()) for m in g_items_models]
    except Exception as e:
        warnings.append(f"指引抽取失败: {e}")
        guidance_items = []

    next_q_rev_guide = next_q_eps_guide = None
    for gi in guidance_items:
        if gi.get("metric_key") == "revenue" and gi.get("mid") is not None and next_q_rev_guide is None:
            next_q_rev_guide = gi["mid"]
        if gi.get("metric_key") in {"eps_nongaap", "eps_gaap"} and gi.get("mid") is not None and next_q_eps_guide is None:
            next_q_eps_guide = gi["mid"]

    scorecard = build_scorecard(
        revenue_actual=revenue.value,
        revenue_estimate=float(rev_est) if rev_est is not None else None,
        eps_actual=eps_actual,
        eps_estimate=float(eps_est) if eps_est is not None else None,
        eps_basis=basis,
        gross_margin=primary_gm,
        operating_margin=primary_om,
        fcf=fcf,
        next_q_revenue_guidance_mid=next_q_rev_guide,
        next_q_revenue_consensus=float(next_rev) if next_rev is not None else None,
        next_q_eps_guidance_mid=next_q_eps_guide,
        next_q_eps_consensus=float(next_eps) if next_eps is not None else None,
    )

    v = validate_extraction(
        extracted,
        parsed["combined"],
        eps_actual=eps_actual,
        snapshot_at=(snap or {}).get("snapshot_at"),
        release_at=release_at,
        n_analysts=int(n_analysts) if n_analysts is not None else None,
    )
    warnings.extend(v["warnings"])

    doc = empty_period_doc(
        ticker,
        period,
        calendar_quarter=info.calendar_quarter,
        period_start=info.period_start.isoformat(),
        period_end=info.period_end.isoformat(),
        release_at_utc=release_at,
        accession=acc,
        press_release_url=f"https://www.sec.gov/Archives/edgar/data/{int(cfg['cik'])}/{acc.replace('-', '')}/",
        eps_basis=basis,
    )
    doc["status"] = {
        "stage": "stage1_done",
        "needs_review": v["needs_review"] or basis == "unknown",
        "warnings": warnings,
    }
    doc["scorecard"] = scorecard
    doc["financials"] = financials
    doc["guidance"]["items"] = guidance_items
    doc["drivers"] = {"stage": 1, "metrics": [], "status": "等待变动原因分析"}

    # stage1 drivers from press only (optional if LLM available)
    try:
        drivers = analyze_drivers(financials, parsed["combined"], stage=1)
        doc["drivers"] = {"stage": 1, "metrics": drivers.get("metrics", [])}
        doc["status"]["warnings"].extend(drivers.get("warnings") or [])
    except Exception as e:
        doc["status"]["warnings"].append(f"Stage1 drivers 跳过: {e}")

    save_period_json(ticker, period, doc)
    add_processed(ticker, acc)
    mark_stage(ticker, period, "stage1_done")
    maybe_notify(format_scorecard_text(ticker, period, scorecard))
    return doc


def run_stage2(ticker: str, fiscal_period: str) -> dict[str, Any]:
    cfg = load_ticker_config(ticker)
    doc = load_period_json(ticker, fiscal_period)
    if not doc:
        raise RuntimeError(f"缺少 {ticker} {fiscal_period} 数据，请先跑 Stage 1")
    raw_dir = __import__("pipeline.config", fromlist=["data_dir"]).data_dir(ticker) / "raw" / fiscal_period
    text, source = fetch_transcript(cfg, raw_dir)
    if not text:
        doc["status"]["warnings"].append("文字稿未取得，可手动放入 raw/{period}/transcript.txt")
        save_period_json(ticker, fiscal_period, doc)
        raise RuntimeError("文字稿未取得")

    doc["meta"]["transcript_source"] = source
    mark_stage(ticker, fiscal_period, "transcript_found")

    # guidance merge from call
    llm = LLMClient()
    g_prompt = llm.load_prompt("extract_guidance.md", guidance_keys=", ".join(load_guidance_keys()))
    try:
        models = llm.complete_json_list("extract_guidance.md", g_prompt + "\n\n----\n" + text[:80000], GuidanceItem)
        call_items = [parse_guidance_item(m.model_dump()) for m in models]
    except Exception as e:
        call_items = []
        doc["status"]["warnings"].append(f"电话会指引抽取失败: {e}")

    merged = {(i.get("metric_key"), i.get("period")): i for i in doc.get("guidance", {}).get("items", [])}
    for ci in call_items:
        key = (ci.get("metric_key"), ci.get("period"))
        if key in merged:
            if ci.get("mid") != merged[key].get("mid"):
                merged[key]["call_variant"] = ci
                doc["status"]["warnings"].append(f"电话会指引与新闻稿不一致: {key}")
        else:
            merged[key] = ci
    doc["guidance"]["items"] = list(merged.values())

    # Q&A
    press_nums = {
        "revenue": (doc.get("financials") or {}).get("revenue", {}).get("value"),
        "eps": next((c.get("actual") for c in doc.get("scorecard", []) if c.get("metric") == "eps"), None),
    }
    qa = structure_qa(text, press_nums)
    prior = load_period_json(ticker, prior_fiscal_period(fiscal_period))
    stats = __import__("pipeline.compute.topics", fromlist=["compute_topic_stats"]).compute_topic_stats(
        qa.get("items") or [], (prior or {}).get("qa", {}).get("items")
    )
    doc["qa"] = {
        "items": qa.get("items") or [],
        **{k: stats[k] for k in ["topic_stats", "new_topics", "dropped_topics", "hot_topics", "evasive_list"]},
    }

    # drivers stage2
    press_path = raw_dir / "press_release.html"
    press_text = load_press_release(press_path)["combined"] if press_path.exists() else ""
    try:
        drivers = analyze_drivers(
            doc.get("financials") or {},
            press_text,
            qa.get("prepared_remarks") or "",
            qa.get("qa_text") or "",
            stage=2,
        )
        doc["drivers"] = {"stage": 2, "metrics": drivers.get("metrics", [])}
        doc["status"]["warnings"].extend(drivers.get("warnings") or [])
    except Exception as e:
        doc["status"]["warnings"].append(f"Stage2 drivers 失败: {e}")

    # summary
    try:
        prior_watch = (prior or {}).get("summary", {}).get("next_watchlist") or []
        doc["summary"] = summarize_period(doc, prior_watch)
    except Exception as e:
        doc["status"]["warnings"].append(f"总结失败: {e}")

    doc["status"]["stage"] = "stage2_done"
    save_period_json(ticker, fiscal_period, doc)
    mark_stage(ticker, fiscal_period, "stage2_done")
    maybe_notify(f"{ticker} {fiscal_period} Stage2 完成")
    return doc


def run_stage3(ticker: str, fiscal_period: str) -> dict[str, Any]:
    """股价反应 + 分析师修正骨架 + 延迟校验占位。"""
    from pipeline.sources.yfinance_src import YFinanceSource
    from pipeline.compute.revisions import compute_analyst_revisions

    doc = load_period_json(ticker, fiscal_period)
    if not doc:
        raise RuntimeError("缺少季度数据")
    release = doc["meta"].get("release_at_utc") or ""
    yf = YFinanceSource(ticker)
    # next day reaction: crude using history around release date
    try:
        day = release[:10]
        hist = yf.history(start=day, end=day)
        # leave null if insufficient; detailed calc can refine
        doc["price_reaction"] = doc.get("price_reaction") or {}
        doc["price_reaction"]["note"] = "详细次日涨跌在 daily 任务中根据盘前/盘后规则补全"
    except Exception as e:
        doc["status"]["warnings"].append(f"股价反应失败: {e}")

    # analyst revisions if snapshots exist
    snap_dir = __import__("pipeline.config", fromlist=["data_dir"]).data_dir(ticker) / "snapshots"
    snaps = {}
    if snap_dir.exists():
        for p in snap_dir.glob("*.json"):
            try:
                snaps[p.stem[:10]] = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    doc["guidance"]["analyst_revisions"] = compute_analyst_revisions(
        snaps,
        t_dates={
            "t_minus_1": "",
            "t_plus_1": "",
            "t_plus_3": "",
            "t_plus_7": "",
        },
    )
    doc["status"]["stage"] = "stage3_done"
    save_period_json(ticker, fiscal_period, doc)
    mark_stage(ticker, fiscal_period, "stage3_done")
    return doc


def run_pipeline(ticker: str, period: str | None, stage: int, accession: str | None = None) -> dict[str, Any]:
    try:
        if stage == 1:
            return run_stage1(ticker, period, accession=accession)
        if stage == 2:
            if not period:
                raise RuntimeError("Stage2 需要 --period")
            return run_stage2(ticker, period)
        if stage == 3:
            if not period:
                raise RuntimeError("Stage3 需要 --period")
            return run_stage3(ticker, period)
        raise RuntimeError(f"未知 stage: {stage}")
    except Exception as e:
        if period:
            record_error(ticker, period, f"stage{stage}", str(e))
        raise
