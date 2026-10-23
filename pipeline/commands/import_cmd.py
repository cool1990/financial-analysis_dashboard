"""港股业绩公告 PDF + 本地电话会 → raw/{period} 与 Stage1 PeriodDoc。

不调用 SecEdgar，不把人民币换成美元，不把六个月中期写成单季。
电话会只落盘；Stage2 的问答 / 变动原因仍要另跑 `run --stage 2`（需要 OPENROUTER_API_KEY）。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from dateutil.relativedelta import relativedelta

from pipeline.compute.metrics import build_metric_block, ratio
from pipeline.compute.scorecard import build_scorecard
from pipeline.config import data_dir, is_hk_listing, load_ticker_config
from pipeline.extract.hk_residual import residual_flows, residual_from_quarter_sum
from pipeline.extract.hk_results import HkCumulativeOnly, HkImportError, extract_hk_results, load_press_text
from pipeline.extract.numbers import parse_number
from pipeline.sources.transcripts import save_transcript
from pipeline.state import add_processed, empty_period_doc, load_period_json, mark_stage, save_period_json
from pipeline.validate import validate_extraction


def _pdf_created_utc(path: Path) -> str | None:
    """PDF 创建时间。只有它的香港日历日和公告日同一天才采用，避免乱填钟点。"""
    if path.suffix.lower() != ".pdf":
        return None
    from datetime import datetime, timedelta, timezone

    from pypdf import PdfReader

    raw = str((PdfReader(str(path)).metadata or {}).get("/CreationDate") or "")
    matched = re.match(
        r"D:(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})([+-])(\d{2})'(\d{2})'",
        raw,
    )
    if not matched:
        return None
    year, month, day, hour, minute, second, sign, zh, zm = matched.groups()
    offset = timedelta(hours=int(zh), minutes=int(zm))
    if sign == "-":
        offset = -offset
    local = datetime(
        int(year), int(month), int(day), int(hour), int(minute), int(second),
        tzinfo=timezone(offset),
    )
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _scaled(raw: str | None, unit: str | None, *, eps: bool = False) -> float | None:
    if raw is None:
        return None
    number = parse_number(str(raw).strip("()"))
    if number is None:
        return None
    if eps or unit in {None, "yuan", "CNY"}:
        return number
    key = (unit or "").lower()
    if "usd" in key or "$" in key:
        raise HkImportError("拒绝把美元写入港股财务字段")
    if key in {"millions", "million"}:
        return number * 1_000_000
    if unit == "亿元":
        return number * 100_000_000
    raise HkImportError(f"未知金额单位: {unit}")


def _abs_node(node: dict[str, Any] | None, *, eps: bool = False) -> dict[str, float | None]:
    if not node:
        return {"value": None, "year_ago": None, "prior_q": None}
    unit = node.get("unit")
    return {
        "value": _scaled(node.get("raw"), unit, eps=eps),
        "year_ago": _scaled(node.get("year_ago_raw"), unit, eps=eps),
        "prior_q": _scaled(node.get("prior_quarter_raw"), unit, eps=eps),
    }


def _block(
    amounts: dict[str, float | None],
    *,
    quote: str | None,
    label: str,
    is_ratio: bool = False,
) -> dict[str, Any]:
    block = build_metric_block(
        amounts["value"],
        yoy_base=amounts["year_ago"],
        qoq_base=amounts["prior_q"],
        is_ratio=is_ratio,
        source_quote=quote,
    )
    block["year_ago"] = amounts["year_ago"]
    block["prior_q"] = amounts["prior_q"]
    block["comparatives_source"] = "press_release"
    block["display_label"] = label
    block["currency"] = "CNY"
    return block


def _kpi_value(node: dict[str, Any]) -> dict[str, Any]:
    out = dict(node)
    eps = node.get("unit") == "yuan"
    out["value"] = _scaled(node.get("raw"), node.get("unit"), eps=eps)
    if node.get("year_ago_raw"):
        out["year_ago"] = _scaled(node.get("year_ago_raw"), node.get("unit"), eps=eps)
    if node.get("prior_quarter_raw"):
        out["prior_q"] = _scaled(node.get("prior_quarter_raw"), node.get("unit"), eps=eps)
    return out


def _level_block(node: dict[str, Any] | None, label: str, *, eps: bool = False) -> dict[str, Any] | None:
    if not node or not node.get("raw"):
        return None
    return _block(_abs_node(node, eps=eps), quote=node.get("source_quote"), label=label, is_ratio=False)


def _margin_block(
    profit: dict[str, Any] | None,
    revenue: dict[str, Any],
    label: str,
) -> dict[str, Any] | None:
    if not profit:
        return None
    prof = _abs_node(profit)
    rev = _abs_node(revenue)
    amounts = {
        "value": ratio(prof["value"], rev["value"]),
        "year_ago": ratio(prof["year_ago"], rev["year_ago"]),
        "prior_q": ratio(prof["prior_q"], rev["prior_q"]),
    }
    if amounts["value"] is None:
        return None
    return _block(amounts, quote=profit.get("source_quote"), label=label, is_ratio=True)


def assemble_hk_stage1(
    ticker: str,
    fiscal_period: str,
    extracted: dict[str, Any],
    press_text: str,
    *,
    accession: str,
    press_release_url: str,
    transcript_source: str | None = None,
) -> dict[str, Any]:
    cfg = load_ticker_config(ticker)
    warnings = list(extracted.get("warnings") or [])
    revenue = extracted["revenue"]
    rev_n = _abs_node(revenue)
    cogs = _abs_node(extracted.get("cost_of_revenue"))
    gp = _abs_node(extracted.get("gross_profit_gaap"))
    if None not in (rev_n["value"], cogs["value"], gp["value"]):
        gap = abs((rev_n["value"] - cogs["value"]) - gp["value"])
        if gap > 1_000_000:
            warnings.append(f"收入减收入成本与毛利相差 {gap:.0f}，请核对收益表")

    basis = cfg.get("eps_basis_override") or cfg.get("eps_basis") or "gaap"
    if basis != "gaap":
        warnings.append("港股记分卡固定用摊薄每股盈利（IFRS），忽略非 gaap 的 eps_basis")
        basis = "gaap"
    eps = _abs_node(extracted.get("eps_gaap_diluted"), eps=True)
    eps_ng = _abs_node(extracted.get("eps_nongaap_diluted"), eps=True)
    op_ng_raw = (extracted.get("operating_income_nongaap") or {}).get("raw")
    if op_ng_raw and extracted.get("eps_nongaap_diluted") and op_ng_raw == extracted["eps_nongaap_diluted"].get("raw"):
        raise HkImportError("非国际财务报告准则经营盈利不能写入 eps_nongaap")

    financials: dict[str, Any] = {
        "revenue": _block(rev_n, quote=revenue.get("source_quote"), label="收入"),
    }
    for key, node, label, eps_flag in (
        ("gross_profit_gaap", extracted.get("gross_profit_gaap"), "毛利（IFRS）", False),
        ("operating_income_gaap", extracted.get("operating_income_gaap"), "经营盈利（IFRS）", False),
        ("operating_income_nongaap", extracted.get("operating_income_nongaap"), "经营盈利（Non-IFRS）", False),
        ("net_income_gaap", extracted.get("net_income_gaap"), "期内盈利（IFRS）", False),
        ("eps_gaap", extracted.get("eps_gaap_diluted"), "摊薄每股盈利（IFRS）", True),
        ("eps_nongaap", extracted.get("eps_nongaap_diluted"), "摊薄每股盈利（Non-IFRS）", True),
        ("capex", extracted.get("capex_gross"), "资本开支", False),
    ):
        block = _level_block(node, label, eps=eps_flag)
        if block:
            financials[key] = block
    for key, node, label in (
        ("gross_margin_gaap", extracted.get("gross_profit_gaap"), "毛利率（IFRS）"),
        ("operating_margin_gaap", extracted.get("operating_income_gaap"), "经营利润率（IFRS）"),
        ("operating_margin_nongaap", extracted.get("operating_income_nongaap"), "经营利润率（Non-IFRS）"),
    ):
        block = _margin_block(node, revenue, label)
        if block:
            financials[key] = block
    financials["kpis"] = {key: _kpi_value(node) for key, node in (extracted.get("kpis") or {}).items()}

    release = extracted.get("release_date") or ""
    release_at = extracted.get("release_at_utc") or (f"{release}T00:00:00Z" if release else "")
    checked = validate_extraction(extracted, press_text, eps_actual=eps["value"], release_at=release_at or None)
    warnings.extend(checked["warnings"])
    warnings.append("listing=hk，Stage1 按收益表规则抽取，未调用 LLM，也未接一致预期")
    if any((node or {}).get("currency") not in {None, "CNY"} for node in (
        extracted.get("revenue"),
        extracted.get("eps_gaap_diluted"),
        extracted.get("eps_nongaap_diluted"),
    )):
        raise HkImportError("抽取结果含非人民币币种")

    scorecard = build_scorecard(
        revenue_actual=rev_n["value"],
        revenue_estimate=None,
        eps_actual=eps["value"],
        eps_estimate=None,
        eps_basis="gaap",
        gross_margin=financials.get("gross_margin_gaap", {}).get("value"),
        operating_margin=financials.get("operating_margin_gaap", {}).get("value"),
    )
    doc = empty_period_doc(
        ticker,
        fiscal_period,
        calendar_quarter=build_period_calendar(extracted["period_end_date"], int(cfg["fiscal_year_end_month"])),
        period_start=_period_start(extracted["period_end_date"]),
        period_end=extracted["period_end_date"],
        release_at_utc=release_at,
        accession=accession,
        press_release_url=press_release_url,
        transcript_source=transcript_source,
        eps_basis="gaap",
    )
    doc["meta"]["listing"] = "hk"
    doc["meta"]["reporting_currency"] = "CNY"
    doc["meta"]["sec_form"] = None
    doc["meta"]["extract_method"] = "hk_results"
    doc["meta"]["extract_mode"] = extracted.get("extract_mode") or "disclosed"
    doc["status"] = {
        "stage": "stage1_done",
        "needs_review": bool(checked["needs_review"]),
        "warnings": warnings,
    }
    doc["scorecard"] = scorecard
    doc["financials"] = financials
    doc["drivers"] = {"stage": 0, "metrics": [], "status": "港股手工导入未调用变动原因模型"}
    return doc


def build_period_calendar(period_end: str, fiscal_year_end_month: int) -> str:
    from pipeline.compute.periods import build_period_info

    return build_period_info(period_end, fiscal_year_end_month).calendar_quarter


def _period_start(period_end: str) -> str:
    end = date.fromisoformat(period_end)
    start = end - relativedelta(months=3) + timedelta(days=1)
    return start.isoformat()


def _raw_dir(ticker: str, period: str) -> Path:
    path = data_dir(ticker) / "raw" / period
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_manifest(raw: Path, payload: dict[str, Any]) -> None:
    (raw / "import_manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _copy_transcript(raw: Path, source: Path) -> dict[str, Any]:
    text = source.read_text(encoding="utf-8")
    save_transcript(raw, text, source="local_import", source_url=str(source))
    return {"sha256": _sha256(source), "filename": source.name, "chars": len(text)}


_FLOW_FROM_DOC = (
    ("revenue", ("revenue",)),
    ("gross_profit", ("gross_profit_gaap",)),
    ("operating_income", ("operating_income_gaap",)),
    ("operating_income_nongaap", ("operating_income_nongaap",)),
    ("net_income", ("net_income_gaap",)),
    ("capex", ("capex",)),
    ("vas_revenue", ("kpis", "vas_revenue")),
    ("advertising_revenue", ("kpis", "advertising_revenue")),
    ("fintech_biz_revenue", ("kpis", "fintech_biz_revenue")),
    ("selling_expense", ("kpis", "selling_expense")),
    ("ga_expense", ("kpis", "ga_expense")),
    ("rd_expense", ("kpis", "rd_expense")),
)


def _millions_of(node: dict[str, Any] | None) -> float | None:
    if not node or node.get("value") is None:
        return None
    if node.get("unit") == "yuan":
        return None
    millions = float(node["value"]) / 1_000_000
    if abs(millions - round(millions)) < 1e-4:
        return float(round(millions))
    return millions


def _doc_flows(doc: dict[str, Any]) -> dict[str, float]:
    financials = doc.get("financials") or {}
    flows: dict[str, float] = {}
    for key, path in _FLOW_FROM_DOC:
        cursor: Any = financials
        for part in path:
            if not isinstance(cursor, dict):
                cursor = None
                break
            cursor = cursor.get(part)
        amount = _millions_of(cursor if isinstance(cursor, dict) else None)
        if amount is not None:
            flows[key] = amount
    return flows


def _fmt_millions(value: float) -> str:
    if abs(value - round(value)) < 1e-6:
        return f"{round(value):,}"
    text = f"{value:,.3f}".rstrip("0").rstrip(".")
    return text


def _residual_node(raw: str, quote: str) -> dict[str, Any]:
    return {
        "raw": raw,
        "unit": "millions",
        "currency": "CNY",
        "period_type": "3m",
        "months": 3,
        "derived": True,
        "source_quote": quote[:200],
        "year_ago_raw": None,
        "prior_quarter_raw": None,
    }


def _saved_quarter_flows(ticker: str) -> list[dict[str, Any]]:
    folder = data_dir(ticker)
    saved = []
    for path in sorted(folder.glob("FY*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        meta = doc.get("meta") or {}
        end = meta.get("period_end")
        start = meta.get("period_start")
        if not end or not start:
            continue
        saved.append({
            "period": path.stem,
            "start": date.fromisoformat(start),
            "end": date.fromisoformat(end),
            "flows": _doc_flows(doc),
        })
    return saved


def _extracted_from_flows(
    fiscal_period: str,
    period_end: date,
    flows: dict[str, float],
    quote: str,
) -> dict[str, Any] | None:
    needed = ("revenue", "gross_profit", "net_income")
    if any(flows.get(key) is None for key in needed):
        return None
    def take(key: str) -> dict[str, Any] | None:
        if flows.get(key) is None:
            return None
        return _residual_node(_fmt_millions(flows[key]), quote)

    rows = {key: take(key) for key in (
        "revenue", "cost_of_revenue", "gross_profit", "operating_income",
        "operating_income_nongaap", "net_income", "vas_revenue", "advertising_revenue",
        "fintech_biz_revenue", "selling_expense", "ga_expense", "rd_expense", "capex",
    )}
    kpis = {
        key: rows[key]
        for key in (
            "vas_revenue", "advertising_revenue", "fintech_biz_revenue",
            "selling_expense", "ga_expense", "rd_expense", "operating_income_nongaap",
        )
        if rows.get(key)
    }
    if "operating_income_nongaap" in kpis:
        kpis["non_ifrs_operating_profit"] = kpis.pop("operating_income_nongaap")
    return {
        "period_label": fiscal_period,
        "period_end_date": period_end.isoformat(),
        "release_date": None,
        "period_type": "3m",
        "currency": "CNY",
        "column_labels": [],
        "column_roles": {"current": 0},
        "extract_mode": "residual",
        "table_mode": "residual",
        "revenue": rows["revenue"],
        "cost_of_revenue": rows["cost_of_revenue"],
        "gross_profit_gaap": rows["gross_profit"],
        "gross_profit_nongaap": None,
        "rd_expense": rows["rd_expense"],
        "operating_income_gaap": rows["operating_income"],
        "operating_income_nongaap": rows["operating_income_nongaap"],
        "net_income_gaap": rows["net_income"],
        "eps_gaap_diluted": None,
        "eps_nongaap_diluted": None,
        "has_nongaap_eps": False,
        "capex_gross": rows["capex"],
        "operating_cash_flow": None,
        "kpis": kpis,
        "prior_year_comparables": {"revenue": {"raw": None, "unit": "millions"}},
        "warnings": [
            f"{quote}。每股盈利未做累计相减。",
            "公告未单列经营现金流，operating_cash_flow 与 fcf 留空",
        ],
    }


def complete_residual_extraction(
    ticker: str,
    fiscal_period: str,
    cumulative: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    """用已入库的前序季度，把六个月或全年累计收成单季。跨度不对则留空。"""
    end = date.fromisoformat(cumulative["period_end"])
    start = date.fromisoformat(cumulative["period_start"])
    from pipeline.extract.hk_statement import _fiscal_from_end

    try:
        inferred = _fiscal_from_end(end, 12)
    except HkImportError as exc:
        return None, str(exc)
    if inferred != fiscal_period.upper():
        return None, f"累计期末 {end.isoformat()} 对应 {inferred}，与请求的 {fiscal_period} 不一致"
    flows = cumulative.get("flows") or {}
    saved = _saved_quarter_flows(ticker)
    same_origin = [
        item for item in saved
        if item["start"] == start and item["end"] < end and item["flows"]
    ]
    same_origin.sort(key=lambda item: item["end"])
    if same_origin:
        prior = same_origin[-1]
        got = residual_flows(
            flows,
            prior["flows"],
            later_end=end,
            earlier_end=prior["end"],
            later_start=start,
            earlier_start=prior["start"],
        )
        if got:
            quote = (
                f"累计相减 {prior['end'].isoformat()}→{end.isoformat()}"
                f"（{prior['period']} 至本公告累计，跨度 {(end - prior['end']).days} 天）"
            )
            return _extracted_from_flows(fiscal_period, end, got, quote), ""
        if cumulative.get("months") != 12:
            gap = (end - prior["end"]).days
            return None, (
                f"最近一段同期累计止于 {prior['end'].isoformat()}，与 {end.isoformat()} 相距 {gap} 天，"
                "不是一个季度，留空"
            )
    if cumulative.get("months") == 12:
        year = end.year
        quarters = []
        missing = []
        for month, day, name in ((3, 31, "Q1"), (6, 30, "Q2"), (9, 30, "Q3")):
            target = date(year, month, day)
            hit = next((item for item in saved if item["end"] == target), None)
            if hit is None:
                missing.append(f"FY{year}{name}")
            else:
                quarters.append((hit["end"], hit["flows"]))
        if missing:
            return None, f"全年没有单季列，且缺少 {'、'.join(missing)}，不能倒挤 {fiscal_period}"
        got = residual_from_quarter_sum(
            flows,
            quarters,
            year_start=start,
            year_end=end,
        )
        if not got:
            return None, f"全年减前三季的跨度不是一个季度，{fiscal_period} 留空"
        quote = f"累计相减 FY{year}Q1+Q2+Q3→{end.isoformat()}（全年减前三季）"
        return _extracted_from_flows(fiscal_period, end, got, quote), ""
    return None, "没有可相减的前序累计，且跨度不能确认为一个季度，留空"


def import_period(
    ticker: str,
    fiscal_period: str,
    *,
    pdf: Path | None = None,
    transcript: Path | None = None,
) -> dict[str, Any]:
    cfg = load_ticker_config(ticker)
    if not is_hk_listing(cfg):
        raise HkImportError(f"{ticker} 不是 listing=hk，不能走手工导入")
    if pdf is None and transcript is None:
        raise HkImportError("至少要给 --pdf 或 --transcript")
    period = fiscal_period.upper()
    raw = _raw_dir(ticker, period)
    transcript_meta = None
    if transcript is not None:
        transcript_meta = _copy_transcript(raw, Path(transcript))

    if pdf is None:
        existing = load_period_json(ticker, period)
        if existing and transcript_meta:
            existing.setdefault("meta", {})["transcript_source"] = "local_import"
            existing.setdefault("status", {}).setdefault("warnings", []).append(
                "电话会已导入（local_import）。电话会里的数字未写入财务字段。"
            )
            save_period_json(ticker, period, existing)
        _write_manifest(raw, {
            "ticker": ticker.upper(),
            "period": period,
            "stage1": "skipped",
            "reason": "没有业绩公告。电话会里的数字未写入财务字段。",
            "transcript": transcript_meta,
            "transcript_source": "local_import" if transcript_meta else None,
        })
        return {
            "ticker": ticker.upper(),
            "period": period,
            "stage1": "skipped",
            "transcript_source": "local_import" if transcript_meta else None,
            "reason": "没有业绩公告 PDF，未写单季财务数字",
        }

    press = Path(pdf)
    text = load_press_text(press)
    try:
        extracted = extract_hk_results(text, period, fiscal_year_end_month=int(cfg["fiscal_year_end_month"]))
    except HkCumulativeOnly as err:
        extracted, reason = complete_residual_extraction(ticker, period, err.cumulative)
        if extracted is None:
            _write_manifest(raw, {
                "ticker": ticker.upper(),
                "period": period,
                "stage1": "skipped",
                "reason": reason or str(err),
                "extract_mode": "blank",
                "source_pdf": press.name,
                "cumulative": {
                    "period_start": (err.cumulative or {}).get("period_start"),
                    "period_end": (err.cumulative or {}).get("period_end"),
                    "months": (err.cumulative or {}).get("months"),
                },
                "transcript": transcript_meta,
                "transcript_source": "local_import" if transcript_meta else None,
            })
            return {
                "ticker": ticker.upper(),
                "period": period,
                "stage1": "skipped",
                "extract_mode": "blank",
                "source_pdf": press.name,
                "transcript_source": "local_import" if transcript_meta else None,
                "reason": reason or str(err),
            }
    stamped = _pdf_created_utc(press)
    if stamped and extracted.get("release_date") and stamped[:10] == extracted["release_date"]:
        extracted["release_at_utc"] = stamped
    (raw / "press_release.txt").write_text(text, encoding="utf-8")
    if press.suffix.lower() == ".pdf":
        dest = raw / "press_release.pdf"
        if press.resolve() != dest.resolve():
            shutil.copyfile(press, dest)
        blob = dest.read_bytes()
    else:
        blob = text.encode("utf-8")
    accession = "manual:" + hashlib.sha256(blob).hexdigest()[:12]
    (raw / "extracted_financials.json").write_text(
        json.dumps(extracted, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    source_name = "local_import" if transcript_meta or (raw / "transcript.txt").exists() else None
    doc = assemble_hk_stage1(
        ticker,
        period,
        extracted,
        text,
        accession=accession,
        press_release_url=f"file:data/{ticker.upper()}/raw/{period}/press_release.pdf",
        transcript_source=source_name,
    )
    if source_name:
        doc["status"]["warnings"].append(
            "电话会已导入（local_import）。其中的美元口径未写入财务字段；Stage2 解读需另跑 run --stage 2。"
        )
    save_period_json(ticker, period, doc)
    mark_stage(ticker, period, "stage1_done")
    add_processed(ticker, accession)
    _write_manifest(raw, {
        "ticker": ticker.upper(),
        "period": period,
        "stage1": "stage1_done",
        "accession": accession,
        "currency": "CNY",
        "period_end": extracted.get("period_end_date"),
        "release_date": extracted.get("release_date"),
        "pdf_sha256": hashlib.sha256(blob).hexdigest(),
        "source_pdf": press.name,
        "extract_mode": extracted.get("extract_mode") or "disclosed",
        "table_mode": extracted.get("table_mode"),
        "revenue_million": (extracted.get("revenue") or {}).get("raw"),
        "eps_gaap": (extracted.get("eps_gaap_diluted") or {}).get("raw"),
        "eps_nongaap": (extracted.get("eps_nongaap_diluted") or {}).get("raw"),
        "operating_income_nongaap_million": (extracted.get("operating_income_nongaap") or {}).get("raw"),
        "transcript": transcript_meta,
        "transcript_source": source_name,
    })
    return {
        "ticker": doc["meta"]["ticker"],
        "period": period,
        "stage1": "stage1_done",
        "accession": accession,
        "currency": "CNY",
        "revenue": (doc["financials"]["revenue"] or {}).get("value"),
        "eps_gaap": (doc["financials"].get("eps_gaap") or {}).get("value"),
        "eps_nongaap": (doc["financials"].get("eps_nongaap") or {}).get("value"),
        "transcript_source": source_name,
        "warnings": doc["status"]["warnings"],
    }


def rerun_hk_stage1(ticker: str, fiscal_period: str | None) -> dict[str, Any]:
    """listing=hk 的 Stage1：只读本地 raw，不查询 SEC。"""
    cfg = load_ticker_config(ticker)
    if not is_hk_listing(cfg):
        raise HkImportError(f"{ticker} 不是 listing=hk")
    folder = data_dir(ticker) / "raw"
    period = fiscal_period.upper() if fiscal_period else ""
    if not period:
        found = sorted(
            path.parent.name
            for path in folder.glob("FY*/press_release.*")
            if path.suffix.lower() in {".pdf", ".txt", ".md"}
        )
        if not found:
            raise HkImportError(f"{ticker} 没有本地业绩公告，请先 import-period --pdf")
        period = found[-1]
    raw = folder / period
    press = next(
        (raw / name for name in ("press_release.pdf", "press_release.txt", "press_release.md") if (raw / name).exists()),
        None,
    )
    if press is None:
        raise HkImportError(f"{ticker} {period} 没有 press_release.pdf / .txt")
    transcript = raw / "transcript.txt"
    out = import_period(
        ticker,
        period,
        pdf=press,
        transcript=transcript if transcript.exists() else None,
    )
    if out.get("stage1") != "stage1_done":
        raise HkImportError(out.get("reason") or f"{ticker} {period} 没有可重跑的业绩公告")
    doc = load_period_json(ticker, out["period"])
    if not doc:
        raise HkImportError(f"{ticker} {period} Stage1 未写入")
    return doc
