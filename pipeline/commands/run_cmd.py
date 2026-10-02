from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pipeline.commands.snapshot_cmd import load_pre_earnings_snapshot
from pipeline.compute.comparatives import apply_press_comparatives
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
    load_failures,
    record_filing_failure,
    empty_period_doc,
    load_period_json,
    load_processed,
    mark_stage,
    record_error,
    save_period_json,
)
from pipeline.validate import validate_extraction
from pipeline.extract.guidance import normalize_period, parse_guidance_item
from pipeline.compute.guidance_review import change_vs_prior, position_in_range
from pipeline.analyze.drivers import analyze_drivers, strip_driver_warnings
from pipeline.analyze.qa import structure_qa
from pipeline.analyze.summary import summarize_period
from pipeline.notify import format_scorecard_text, maybe_notify


def _guidance_merge_key(item: dict[str, Any]) -> tuple:
    """合并键：other 类按标签区分，避免「全年展望」与「出货锁定」被当成同一条冲突。"""
    mk = item.get("metric_key") or ""
    period = normalize_period(item.get("period"))
    # 数值指引与方向性表述分开：同 metric_key 的「R&D 将上升」不能并进「运营费用 $2.31B」
    numeric = _numeric_mid(item) is not None
    if mk == "other":
        label = (item.get("metric_label") or item.get("statement") or "").strip().lower()
        label = re.sub(r"\s+", " ", label)[:80]
        return (mk, period, numeric, label)
    return (mk, period, numeric)


def _numeric_mid(item: dict[str, Any]) -> float | None:
    mid = item.get("mid")
    if mid is None:
        return None
    try:
        return float(mid)
    except (TypeError, ValueError):
        return None


def _guidance_conflict_warning(press: dict[str, Any], call: dict[str, Any]) -> str | None:
    """仅当双方都有数值且明显不一致时告警；文案用中文。"""
    a, b = _numeric_mid(press), _numeric_mid(call)
    if a is None or b is None:
        return None
    if a == 0:
        differ = abs(b - a) > 1e-9
    else:
        differ = abs(b - a) / abs(a) > 0.02 and abs(b - a) > 1e-6
    if not differ:
        return None
    label = press.get("metric_label") or call.get("metric_label") or press.get("metric_key") or "指标"
    period = press.get("period") or call.get("period") or "—"
    return f"电话会与新闻稿数值指引不一致：{label}（{period}）新闻稿中值 {a} vs 电话会 {b}"
from pipeline.compute.revisions import compute_analyst_revisions


def merge_guidance_items(
    base_items: list[dict[str, Any]],
    new_items: list[dict[str, Any]],
    warnings: list[str],
) -> list[dict[str, Any]]:
    """把电话会指引并入新闻稿指引：同指标同期间只保留一条（新闻稿数值优先）。"""
    merged: dict[tuple, dict[str, Any]] = {}
    for item in base_items:
        merged.setdefault(_guidance_merge_key(item), item)
    for ci in new_items:
        key = _guidance_merge_key(ci)
        if key not in merged:
            merged[key] = ci
            continue
        base = merged[key]
        warn = _guidance_conflict_warning(base, ci)
        if warn:
            base["call_variant"] = ci
            if warn not in warnings:
                warnings.append(warn)
            continue
        # 同键但无数值冲突：用电话会内容丰富字段（保留新闻稿来源优先的数值）
        if base.get("source") != ci.get("source"):
            base["confirmed_by"] = ci.get("source")
        for field in ("statement", "source_quote", "direction", "metric_label"):
            if not base.get(field) and ci.get(field):
                base[field] = ci.get(field)
        if _numeric_mid(base) is None and _numeric_mid(ci) is not None:
            for field in ("low", "mid", "high", "type", "basis", "source"):
                if ci.get(field) is not None:
                    base[field] = ci.get(field)
    return list(merged.values())


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


def _accepted_dt(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def detect_new_filings(ticker: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """只返回「近期」且未处理、未超失败上限的 Item 2.02 8-K。

    历史 8-K 不在 processed.json 里是常态（新加股票时尤其如此）；若不按发布时间过滤，
    poll 每天财报窗口都会逐份回溯历史新闻稿并调用 LLM。历史数据请走 backfill。
    """
    cfg = load_ticker_config(ticker)
    if not cfg.get("cik"):
        raise RuntimeError(f"{ticker} 尚未 init（缺少 CIK）")
    polling = load_settings().get("polling") or {}
    max_age = timedelta(days=float(polling.get("new_filing_max_age_days", 4)))
    max_fail = int(polling.get("max_retries", 5))
    now = now or datetime.now(timezone.utc)
    client = SecEdgarClient()
    processed = load_processed(ticker)
    failures = load_failures(ticker)
    out = []
    for f in client.find_earnings_8k(cfg["cik"]):
        acc = f["accessionNumber"]
        if acc in processed or failures.get(acc, 0) >= max_fail:
            continue
        accepted = _accepted_dt(f.get("acceptanceDateTime") or "")
        if accepted is None or now - accepted > max_age:
            continue
        out.append(f)
    return out


def run_stage1(ticker: str, fiscal_period: str | None = None, accession: str | None = None) -> dict[str, Any]:
    cfg = load_ticker_config(ticker)
    client = SecEdgarClient()
    if accession:
        filings = [f for f in client.find_earnings_8k(cfg["cik"]) if f["accessionNumber"] == accession]
    else:
        filings = detect_new_filings(ticker)
    if not filings:
        raise RuntimeError("未发现可处理的近期 8-K Item 2.02（历史季度请用 backfill 或 --accession）")
    filing = filings[0]
    try:
        return _run_stage1_filing(ticker, cfg, client, filing, fiscal_period)
    except Exception:
        # 失败计数：poll 每 15 分钟一次，若不封顶会对同一份 8-K 反复调用 LLM
        record_filing_failure(ticker, filing["accessionNumber"])
        raise


def _run_stage1_filing(
    ticker: str,
    cfg: dict[str, Any],
    client: SecEdgarClient,
    filing: dict[str, Any],
    fiscal_period: str | None,
) -> dict[str, Any]:
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
        # 净利润只用于页面的「现金转化」检查（自由现金流 / 净利润）
        "net_income_gaap": build_metric_block(
            ParsedAmount.from_dict(extracted.get("net_income_gaap")).value,
            source_quote=(extracted.get("net_income_gaap") or {}).get("source_quote"),
        ),
        "kpis": extracted.get("kpis") or {},
    }

    # 没有历史季度 JSON 时，用新闻稿表格里的上季 / 去年同季列补同比、环比（不调用 LLM）
    apply_press_comparatives(financials, extracted, capex_definition=capex_def)

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
    # EPS 指引按 eps_basis 选口径（新闻稿通常 GAAP 在前，不能简单取第一条）
    eps_keys = ["eps_gaap", "eps_nongaap"] if basis == "gaap" else ["eps_nongaap", "eps_gaap"]
    eps_guides: dict[str, float] = {}
    for gi in guidance_items:
        if gi.get("metric_key") == "revenue" and gi.get("mid") is not None and next_q_rev_guide is None:
            next_q_rev_guide = gi["mid"]
        if gi.get("metric_key") in eps_keys and gi.get("mid") is not None:
            eps_guides.setdefault(gi["metric_key"], gi["mid"])
    next_q_eps_guide = next((eps_guides[k] for k in eps_keys if k in eps_guides), None)

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

    # stage1 drivers from press only：Stage2 拿到文字稿后会整体重算，
    # 只想要最终版解读、进一步省钱时可在 settings.yaml 关掉 llm.stage1_drivers
    if (load_settings().get("llm") or {}).get("stage1_drivers", True):
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


def run_stage2(ticker: str, fiscal_period: str, *, force: bool = False) -> dict[str, Any]:
    import time as _time

    cfg = load_ticker_config(ticker)
    doc = load_period_json(ticker, fiscal_period)
    if not doc:
        raise RuntimeError(f"缺少 {ticker} {fiscal_period} 数据，请先跑 Stage 1")

    stage_now = (doc.get("status") or {}).get("stage") or ""
    qa_items = ((doc.get("qa") or {}).get("items")) or []
    driver_metrics = ((doc.get("drivers") or {}).get("metrics")) or []
    # 已完成且产物齐全时默认跳过，避免 oneshot/误触重跑长时间烧 OpenRouter
    complete = (
        stage_now in {"stage2_done", "stage3_done"}
        and qa_items
        and driver_metrics
        and (doc.get("summary") or {}).get("headline")
    )
    if not force and complete and any(q.get("parse_failed") for q in qa_items):
        return _repair_failed_qa(ticker, fiscal_period, cfg, doc)
    if not force and complete:
        print(
            f"[stage2] 跳过：{ticker} {fiscal_period} 已是 {stage_now} "
            f"(qa={len(qa_items)}, drivers={len(driver_metrics)})。需要重跑请加 --force",
            flush=True,
        )
        return doc

    raw_dir = __import__("pipeline.config", fromlist=["data_dir"]).data_dir(ticker) / "raw" / fiscal_period
    release_at = (doc.get("meta") or {}).get("release_at_utc")
    text, source = fetch_transcript(
        cfg,
        raw_dir,
        fiscal_period=fiscal_period,
        release_at=release_at,
    )
    if not text:
        doc["status"]["warnings"].append(
            "文字稿未取得（已试 Motley Fool / IR / manual）。"
            f"可手动放入 raw/{fiscal_period}/transcript.txt 后重跑 Stage2"
        )
        save_period_json(ticker, fiscal_period, doc)
        raise RuntimeError("文字稿未取得")

    doc["meta"]["transcript_source"] = source
    mark_stage(ticker, fiscal_period, "transcript_found")

    def _step(name: str):
        t0 = _time.monotonic()
        print(f"[stage2] start {name}", flush=True)
        return t0

    def _done(name: str, t0: float) -> None:
        print(f"[stage2] done {name} in {_time.monotonic() - t0:.1f}s", flush=True)

    # guidance merge from call
    llm = LLMClient()
    g_prompt = llm.load_prompt("extract_guidance.md", guidance_keys=", ".join(load_guidance_keys()))
    t0 = _step("extract_guidance")
    try:
        # 控制输入体积：80k 字符对长文本模型仍会拖很久；40k 通常够覆盖指引段落
        models = llm.complete_json_list(
            "extract_guidance.md",
            g_prompt + "\n\n----\n" + text[:40000],
            GuidanceItem,
        )
        call_items = [parse_guidance_item(m.model_dump()) for m in models]
    except Exception as e:
        call_items = []
        doc["status"]["warnings"].append(f"电话会指引抽取失败: {e}")
    _done("extract_guidance", t0)

    doc["guidance"]["items"] = merge_guidance_items(
        doc.get("guidance", {}).get("items", []), call_items, doc["status"]["warnings"]
    )

    # Q&A
    press_nums = {
        "revenue": (doc.get("financials") or {}).get("revenue", {}).get("value"),
        "eps": next((c.get("actual") for c in doc.get("scorecard", []) if c.get("metric") == "eps"), None),
    }
    t0 = _step("structure_qa")
    qa = structure_qa(text, press_nums)
    _done("structure_qa", t0)
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
    t0 = _step("analyze_drivers")
    try:
        drivers = analyze_drivers(
            doc.get("financials") or {},
            press_text,
            qa.get("prepared_remarks") or "",
            qa.get("qa_text") or "",
            stage=2,
        )
        doc["drivers"] = {"stage": 2, "metrics": drivers.get("metrics", [])}
        doc["status"]["warnings"] = strip_driver_warnings(doc["status"]["warnings"]) + (drivers.get("warnings") or [])
    except Exception as e:
        doc["status"]["warnings"].append(f"Stage2 drivers 失败: {e}")
    _done("analyze_drivers", t0)

    # summary
    t0 = _step("summarize")
    try:
        prior_watch = (prior or {}).get("summary", {}).get("next_watchlist") or []
        doc["summary"] = summarize_period(doc, prior_watch)
    except Exception as e:
        doc["status"]["warnings"].append(f"总结失败: {e}")
    _done("summarize", t0)

    t0 = _step("review_theses")
    try:
        _apply_thesis_review(ticker, doc)
    except Exception as e:
        doc["status"]["warnings"].append(f"论点评估失败: {e}")
    _done("review_theses", t0)

    doc["status"]["stage"] = "stage2_done"
    save_period_json(ticker, fiscal_period, doc)
    mark_stage(ticker, fiscal_period, "stage2_done")
    maybe_notify(f"{ticker} {fiscal_period} Stage2 完成")
    return doc


def _apply_thesis_review(ticker: str, doc: dict[str, Any], *, force: bool = False) -> bool:
    """有论点文件时评估论点；论点和本季数据都没变则跳过（不花钱）。返回是否调用了 LLM。"""
    from pipeline.analyze.thesis import review_fingerprint, review_theses
    from pipeline.config import load_theses

    theses = load_theses(ticker)
    if not theses:
        return False
    old = doc.get("thesis_review") or {}
    if not force and old.get("fingerprint") == review_fingerprint(theses, doc):
        print("[thesis] 论点与本季数据都没变，跳过评估", flush=True)
        return False
    doc["thesis_review"] = review_theses(theses, doc)
    return True


def run_thesis_review(ticker: str, fiscal_period: str, *, force: bool = False) -> dict[str, Any]:
    """只做论点评估（1 次小调用）：用于已完成的季度，或修改论点文件之后。"""
    doc = load_period_json(ticker, fiscal_period)
    if not doc:
        raise RuntimeError(f"缺少 {ticker} {fiscal_period} 数据")
    if not ((doc.get("qa") or {}).get("items") or (doc.get("summary") or {}).get("headline")):
        raise RuntimeError("Stage2 尚未完成，论点评估需要电话会结果")
    if _apply_thesis_review(ticker, doc, force=force):
        save_period_json(ticker, fiscal_period, doc)
    return doc


def _repair_failed_qa(ticker: str, fiscal_period: str, cfg: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    """Stage2 已完成但有问答轮次结构化失败：只重跑这几轮（通常 1 次调用），其余不动。"""
    from pipeline.analyze.qa import repair_failed_items
    from pipeline.compute.topics import compute_topic_stats

    raw_dir = __import__("pipeline.config", fromlist=["data_dir"]).data_dir(ticker) / "raw" / fiscal_period
    text, _source = fetch_transcript(
        cfg, raw_dir, fiscal_period=fiscal_period, release_at=(doc.get("meta") or {}).get("release_at_utc")
    )
    if not text:
        raise RuntimeError("文字稿未取得，无法修复失败的问答")
    items = doc["qa"]["items"]
    n_failed = sum(1 for q in items if q.get("parse_failed"))
    print(f"[stage2] 仅修复 {n_failed} 轮失败的问答（其余结果保留；全量重跑请加 --force）", flush=True)
    press_nums = {
        "revenue": (doc.get("financials") or {}).get("revenue", {}).get("value"),
        "eps": next((c.get("actual") for c in doc.get("scorecard", []) if c.get("metric") == "eps"), None),
    }
    new_items, repaired = repair_failed_items(text, items, press_nums)
    prior = load_period_json(ticker, prior_fiscal_period(fiscal_period))
    stats = compute_topic_stats(new_items, (prior or {}).get("qa", {}).get("items"))
    doc["qa"] = {
        "items": new_items,
        **{k: stats[k] for k in ["topic_stats", "new_topics", "dropped_topics", "hot_topics", "evasive_list"]},
    }
    save_period_json(ticker, fiscal_period, doc)
    print(f"[stage2] 已修复 {repaired}/{n_failed} 轮", flush=True)
    return doc


def _snapshot_at(data: dict[str, Any]) -> datetime | None:
    raw = data.get("snapshot_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _iter_snapshots(ticker: str) -> list[tuple[datetime, dict[str, Any], str]]:
    snap_dir = __import__("pipeline.config", fromlist=["data_dir"]).data_dir(ticker) / "snapshots"
    if not snap_dir.exists():
        return []
    rows: list[tuple[datetime, dict[str, Any], str]] = []
    for path in snap_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        ts = _snapshot_at(data)
        if ts is None:
            continue
        rows.append((ts, data, path.name))
    rows.sort(key=lambda x: x[0])
    return rows


def _point_from_snap(data: dict[str, Any], *, row: str) -> dict[str, Any]:
    ee = estimate_row(data.get("earnings_estimate") or {}, row) or {}
    re = estimate_row(data.get("revenue_estimate") or {}, row) or {}
    ts = _snapshot_at(data)
    return {
        "eps": ee.get("avg"),
        "revenue": re.get("avg"),
        "date": ts.date().isoformat() if ts else None,
        "row": row,
    }


def _first_snap_on_or_after(
    rows: list[tuple[datetime, dict[str, Any], str]], threshold: datetime
) -> dict[str, Any] | None:
    for ts, data, _ in rows:
        if ts >= threshold:
            return data
    return None


def _benchmark_reaction(cfg: dict[str, Any], release: str, timing: str, own: dict[str, Any]) -> dict[str, Any]:
    """同一对交易日里对照指数（ETF）的涨跌，以及个股的超额涨跌。失败时只返回 benchmark 名称。"""
    from pipeline.sources.yfinance_src import YFinanceSource

    symbol = (cfg.get("benchmark") or "SPY").upper()
    out: dict[str, Any] = {"benchmark": symbol, "benchmark_pct": None, "excess_pct": None}
    try:
        bench = YFinanceSource(symbol).next_day_reaction(release, release_timing=timing)
    except Exception:
        return out
    # 交易日要对得上才可比
    if bench.get("before_date") != own.get("before_date") or bench.get("after_date") != own.get("after_date"):
        return out
    b, s = bench.get("next_day_pct"), own.get("next_day_pct")
    out["benchmark_pct"] = b
    if b is not None and s is not None:
        out["excess_pct"] = s - b
    return out


def run_stage3(ticker: str, fiscal_period: str) -> dict[str, Any]:
    """股价反应 + 分析师修正（按发布前后快照对齐）。"""
    from datetime import timedelta

    from pipeline.sources.yfinance_src import YFinanceSource

    doc = load_period_json(ticker, fiscal_period)
    if not doc:
        raise RuntimeError("缺少季度数据")
    release = doc["meta"].get("release_at_utc") or ""
    if not release:
        raise RuntimeError("缺少 release_at_utc，无法对齐分析师修正")
    release_dt = datetime.fromisoformat(release.replace("Z", "+00:00"))

    yf = YFinanceSource(ticker)
    cfg = load_ticker_config(ticker)
    timing = cfg.get("release_timing") or "amc"
    try:
        doc["price_reaction"] = yf.next_day_reaction(release, release_timing=timing)
        doc["price_reaction"].update(_benchmark_reaction(cfg, release, timing, doc["price_reaction"]))
    except Exception as e:
        doc["price_reaction"] = {
            "next_day_pct": None,
            "close_before": None,
            "close_after": None,
            "note": f"股价反应计算失败: {e}",
        }
        warn = f"股价反应失败: {e}"
        if warn not in doc["status"]["warnings"]:
            doc["status"]["warnings"].append(warn)

    points: dict[str, dict[str, Any]] = {
        "t_minus_1": {},
        "t_plus_1": {},
        "t_plus_3": {},
        "t_plus_7": {},
    }
    notes: list[str] = []

    # 财报前：本季一致预期用 0q；发布后 Yahoo 会把 0q 滚到下季，
    # 因此 T+N 用发布后快照的 0q 追踪「下季」预期变化（对应发布前的 +1q）。
    pre = load_pre_earnings_snapshot(ticker, release)
    if pre:
        points["t_minus_1"] = _point_from_snap(pre, row="0q")
        points["t_minus_1"]["next_q"] = _point_from_snap(pre, row="+1q")
    else:
        notes.append("缺少财报前快照，T-1 一致预期为空")

    snaps = _iter_snapshots(ticker)
    for key, days in [("t_plus_1", 1), ("t_plus_3", 3), ("t_plus_7", 7)]:
        data = _first_snap_on_or_after(snaps, release_dt + timedelta(days=days))
        if data:
            # 发布后 0q ≈ 发布前 +1q（下季）
            points[key] = _point_from_snap(data, row="0q")
        else:
            notes.append(f"缺少发布后 T+{days} 快照")

    nq_eps = next(
        (c.get("actual") for c in doc.get("scorecard", []) if c.get("metric") == "next_q_eps_guidance"),
        None,
    )
    nq_rev = next(
        (c.get("actual") for c in doc.get("scorecard", []) if c.get("metric") == "next_q_revenue_guidance"),
        None,
    )
    # gap_closure 更适合看「下季」预期是否向指引靠拢：用发布前 +1q 作为 T-1
    gap_points = dict(points)
    if pre:
        gap_points["t_minus_1"] = _point_from_snap(pre, row="+1q")
    revisions = compute_analyst_revisions(
        gap_points,
        guidance_mid_eps=float(nq_eps) if nq_eps is not None else None,
        guidance_mid_rev=float(nq_rev) if nq_rev is not None else None,
    )
    # 页面同时保留「本季」发布前 0q，避免只显示下季造成误解
    if points.get("t_minus_1"):
        revisions["t_minus_1_current_quarter"] = {
            "eps": points["t_minus_1"].get("eps"),
            "revenue": points["t_minus_1"].get("revenue"),
            "date": points["t_minus_1"].get("date"),
            "note": "财报前对本季（当时 0q）的一致预期",
        }
    revisions["status_notes"] = notes
    revisions["methodology"] = (
        "T-1 本季=发布前 0q；下季轨迹用发布前 +1q 与发布后 0q 对齐。"
        "T+1/3/7 需对应日期之后的 snapshot 才会填入。"
    )
    doc.setdefault("guidance", {})["analyst_revisions"] = revisions
    # 仍在等文字稿（stage1_done）时不推进阶段，否则 poll 会认为 Stage2 已完成而不再尝试
    if doc["status"].get("stage") in {"stage2_done", "stage3_done"}:
        doc["status"]["stage"] = "stage3_done"
        save_period_json(ticker, fiscal_period, doc)
        mark_stage(ticker, fiscal_period, "stage3_done")
    else:
        save_period_json(ticker, fiscal_period, doc)
    return doc


def refresh_comparatives(ticker: str | None = None) -> dict[str, Any]:
    """对已有季度 JSON 重算新闻稿对比列的同比 / 环比。幂等，不联网、不调用 LLM。"""
    from pipeline.config import data_dir, list_tickers

    out: dict[str, list[str]] = {}
    for t in [ticker.upper()] if ticker else list_tickers():
        cfg = load_ticker_config(t)
        for path in sorted(data_dir(t).glob("FY*.json")):
            extracted_path = data_dir(t) / "raw" / path.stem / "extracted_financials.json"
            if not extracted_path.exists():
                continue
            doc = json.loads(path.read_text(encoding="utf-8"))
            extracted = json.loads(extracted_path.read_text(encoding="utf-8"))
            fin = doc.setdefault("financials", {})
            filled = []
            ni = ParsedAmount.from_dict(extracted.get("net_income_gaap"))
            if "net_income_gaap" not in fin and ni.value is not None:
                fin["net_income_gaap"] = build_metric_block(ni.value, source_quote=ni.source_quote)
                filled.append("net_income_gaap")
            filled += apply_press_comparatives(
                fin,
                extracted,
                capex_definition=cfg.get("capex_definition") or "gross",
            )
            if filled:
                save_period_json(t, path.stem, doc)
                out[f"{t} {path.stem}"] = filled
    return {"filled": out}


def refresh_recent_stage3(now: datetime | None = None) -> dict[str, Any]:
    """daily 调用：发布后 N 天内的季度重算 Stage3，让 T+1/T+3/T+7 随快照自动补齐。不调用 LLM。"""
    from pipeline.config import data_dir, list_tickers

    now = now or datetime.now(timezone.utc)
    days = float((load_settings().get("polling") or {}).get("stage3_refresh_days", 10))
    out: list[dict[str, Any]] = []
    for ticker in list_tickers():
        for path in sorted(data_dir(ticker).glob("FY*.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            stage = (doc.get("status") or {}).get("stage")
            if stage not in {"stage1_done", "stage2_done", "stage3_done"}:
                continue
            release = _accepted_dt((doc.get("meta") or {}).get("release_at_utc") or "")
            if release is None or not (timedelta(0) <= now - release <= timedelta(days=days)):
                continue
            try:
                run_stage3(ticker, path.stem)
                out.append({"ticker": ticker, "period": path.stem, "ok": True})
            except Exception as e:
                out.append({"ticker": ticker, "period": path.stem, "ok": False, "error": str(e)})
    return {"refreshed": out}


def run_pipeline(
    ticker: str,
    period: str | None,
    stage: int,
    accession: str | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    try:
        if stage == 1:
            return run_stage1(ticker, period, accession=accession)
        if stage == 2:
            if not period:
                raise RuntimeError("Stage2 需要 --period")
            return run_stage2(ticker, period, force=force)
        if stage == 3:
            if not period:
                raise RuntimeError("Stage3 需要 --period")
            return run_stage3(ticker, period)
        raise RuntimeError(f"未知 stage: {stage}")
    except Exception as e:
        if period:
            record_error(ticker, period, f"stage{stage}", str(e))
        raise
