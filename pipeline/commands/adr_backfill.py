"""ADR 历史回补：只解析 6-K 新闻稿里的官方美元，不调用模型，不覆盖已有季度。"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from pipeline.commands.backfill_cmd import _block
from pipeline.compute.periods import build_period_info
from pipeline.compute.scorecard import build_scorecard
from pipeline.config import data_dir, load_ticker_config
from pipeline.earnings_config import resolve_earnings_spec
from pipeline.extract.adr_statement import parse_adr_press_statement
from pipeline.extract.press_release import load_press_release
from pipeline.sources.earnings_6k import classify_6k, form_matches
from pipeline.sources.history_filings import _submission_blocks
from pipeline.sources.sec_edgar import SecEdgarClient
from pipeline.state import empty_period_doc, load_period_json, save_period_json


def _rescue_press_html(client: SecEdgarClient, cik: str, row: dict[str, Any], dest: Path):
    """同一份 6-K 里若先存到幻灯片，再试其它 HTML，直到读出 US$ per ADR。"""
    acc = row["accessionNumber"]
    names = []
    primary = str(row.get("primaryDocument") or "")
    if primary:
        names.append(primary)
    try:
        index = client._cached_filing_index(cik, acc)
    except Exception:
        index = {}
    for item in (index.get("directory") or {}).get("item") or []:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
    seen = set()
    for name in names:
        low = name.lower()
        if name in seen or not low.endswith((".htm", ".html", ".txt")):
            continue
        if low.endswith("-index.html") or "index-headers" in low:
            continue
        seen.add(name)
        try:
            blob = client._cached_filing_bytes(cik, acc, name)
        except Exception:
            continue
        tmp = dest.with_suffix(".try.html")
        tmp.write_bytes(blob)
        try:
            parsed = load_press_release(tmp)
        except Exception:
            tmp.unlink(missing_ok=True)
            continue
        statement = parse_adr_press_statement(parsed.get("combined") or "")
        tmp.unlink(missing_ok=True)
        if statement.get("period_end") and "eps_gaap" in (statement.get("amounts") or {}):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(blob)
            return statement
    return None


def _filing_rows(client: SecEdgarClient, cik: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for block in _submission_blocks(client, cik):
        forms = block.get("form") or []
        accepted = block.get("acceptanceDateTime") or []
        accessions = block.get("accessionNumber") or []
        primary = block.get("primaryDocument") or []
        filed = block.get("filingDate") or []
        report = block.get("reportDate") or []
        for index, form in enumerate(forms):
            if index >= len(accessions):
                continue
            rows.append(
                {
                    "form": form,
                    "acceptanceDateTime": accepted[index] if index < len(accepted) else "",
                    "accessionNumber": accessions[index],
                    "primaryDocument": primary[index] if index < len(primary) else "",
                    "filingDate": str(filed[index] or "")[:10] if index < len(filed) else "",
                    "reportDate": str(report[index] or "")[:10] if index < len(report) else "",
                }
            )
    rows.sort(key=lambda row: (row["acceptanceDateTime"], row["accessionNumber"]), reverse=True)
    return rows


def _metric_block(amount: dict[str, Any] | None, *, yoy: float | None, qoq: float | None, is_ratio: bool = False):
    if not amount or amount.get("value") is None:
        return None
    block = _block({"value": amount["value"], "source_quote": amount.get("source_quote")}, yoy=yoy, qoq=qoq, is_ratio=is_ratio)
    if block:
        block["comparatives_source"] = "press_release"
    return block


def write_adr_press_history(
    ticker: str,
    *,
    start: str = "2016-01-01",
    client: SecEdgarClient | None = None,
) -> dict[str, Any]:
    """下载业绩 6-K 新闻稿并写入缺季。已有 FY*.json 不覆盖。台币没有美元对照的科目留空。"""
    cfg = load_ticker_config(ticker)
    spec = resolve_earnings_spec(cfg)
    if spec.listing != "adr":
        raise RuntimeError(f"{ticker} 不是 ADR，不用 6-K 新闻稿回补")
    if not cfg.get("cik"):
        raise RuntimeError("请先运行 init")
    sec = client or SecEdgarClient()
    filters = spec.earnings_6k.model_dump() if spec.earnings_6k is not None else {}
    start_day = date.fromisoformat(start)
    written: list[str] = []
    skipped: list[str] = []
    gaps: list[dict[str, str]] = []
    seen: set[str] = set()
    fye = int(cfg["fiscal_year_end_month"])

    candidates = []
    for row in _filing_rows(sec, str(cfg["cik"])):
        if not form_matches(str(row.get("form") or ""), list(spec.earnings_forms), amendments=True):
            continue
        filed = row.get("filingDate") or ""
        if filed and filed < start:
            continue
        decision, reason = classify_6k(str(row.get("primaryDocument") or ""), [], filters)
        if decision == "skip" and reason == "primary_doc":
            continue
        candidates.append(row)

    parsed_rows: list[dict[str, Any]] = []
    for row in candidates:
        acc = row["accessionNumber"]
        raw_dir = data_dir(ticker) / "raw" / "_backfill" / acc.replace("-", "")
        raw = raw_dir / "press_release.html"
        try:
            decision, reason, press, _presentation = sec._classify_6k_row(str(cfg["cik"]), row, filters)
        except Exception as exc:
            gaps.append({"accessionNumber": acc, "error": str(exc)})
            continue
        try:
            if decision == "hit" and press is not None:
                sec.download_press_release(
                    str(cfg["cik"]),
                    acc,
                    raw,
                    press_release_file=press.get("name") or None,
                )
            else:
                # 2019 年前业绩常直接写在 6-K 正文，没有 EX-99.1。只接受能解析出 US$ per ADR 的正文。
                primary = str(row.get("primaryDocument") or "")
                if not primary.lower().endswith((".htm", ".html", ".txt")):
                    continue
                raw.parent.mkdir(parents=True, exist_ok=True)
                raw.write_bytes(sec._cached_filing_bytes(str(cfg["cik"]), acc, primary))
            parsed = load_press_release(raw)
            statement = parse_adr_press_statement(parsed.get("combined") or "")
            if not statement.get("period_end") or "eps_gaap" not in (statement.get("amounts") or {}):
                rescued = _rescue_press_html(sec, str(cfg["cik"]), row, raw)
                if rescued is not None:
                    statement = rescued
        except Exception as exc:
            gaps.append({"accessionNumber": acc, "error": str(exc)})
            continue
        if decision != "hit" and "eps_gaap" not in (statement.get("amounts") or {}):
            if raw.exists():
                raw.unlink()
            continue
        end_text = statement.get("period_end")
        if not end_text:
            gaps.append({"accessionNumber": acc, "error": "新闻稿没有 quarter ended 日期"})
            continue
        end = date.fromisoformat(end_text)
        if end < start_day:
            continue
        info = build_period_info(end, fye)
        parsed_rows.append(
            {
                "period": info.fiscal_period,
                "info": info,
                "end": end,
                "statement": statement,
                "accession": acc,
                "accepted": row.get("acceptanceDateTime") or "",
                "raw": raw,
            }
        )

    parsed_rows.sort(key=lambda item: (item["accepted"], item["accession"]), reverse=True)
    by_period: dict[str, dict[str, Any]] = {}
    for item in parsed_rows:
        by_period.setdefault(item["period"], item)

    def _amount(period: str, key: str) -> float | None:
        item = by_period.get(period)
        if item is None:
            return None
        node = (item["statement"].get("amounts") or {}).get(key)
        if not node:
            return None
        return node.get("value")

    for period, item in sorted(by_period.items()):
        if load_period_json(ticker, period):
            skipped.append(period)
            continue
        if period in seen:
            continue
        statement = item["statement"]
        amounts = statement.get("amounts") or {}
        if "revenue" not in amounts and "eps_gaap" not in amounts:
            gaps.append({"period": period, "accessionNumber": item["accession"], "error": "没有美元营收，也没有 US$ per ADR"})
            continue
        info = item["info"]
        end = item["end"]
        yoy_period = f"FY{info.fiscal_year - 1}Q{info.fiscal_quarter}"
        prior_q = info.fiscal_quarter - 1
        prior_year = info.fiscal_year
        if prior_q == 0:
            prior_q = 4
            prior_year -= 1
        prior_period = f"FY{prior_year}Q{prior_q}"
        fin: dict[str, Any] = {}
        rev = _metric_block(amounts.get("revenue"), yoy=_amount(yoy_period, "revenue"), qoq=_amount(prior_period, "revenue"))
        if rev:
            fin["revenue"] = rev
        margins = statement.get("margins") or {}
        if margins.get("gross") is not None:
            fin["gross_margin_gaap"] = _metric_block(
                {"value": margins["gross"], "source_quote": margins.get("quote")},
                yoy=None,
                qoq=None,
                is_ratio=True,
            )
            revenue_value = (amounts.get("revenue") or {}).get("value")
            if revenue_value is not None and "gross_profit_gaap" not in amounts:
                fin["gross_profit_gaap"] = _metric_block(
                    {
                        "value": revenue_value * margins["gross"],
                        "source_quote": f"美元营收 × 新闻稿毛利率。{margins.get('quote') or ''}"[:200],
                    },
                    yoy=None,
                    qoq=None,
                )
        if margins.get("operating") is not None:
            fin["operating_margin_gaap"] = _metric_block(
                {"value": margins["operating"], "source_quote": margins.get("quote")},
                yoy=None,
                qoq=None,
                is_ratio=True,
            )
            revenue_value = (amounts.get("revenue") or {}).get("value")
            if revenue_value is not None and "operating_income_gaap" not in amounts:
                fin["operating_income_gaap"] = _metric_block(
                    {
                        "value": revenue_value * margins["operating"],
                        "source_quote": f"美元营收 × 新闻稿营业利润率。{margins.get('quote') or ''}"[:200],
                    },
                    yoy=None,
                    qoq=None,
                )
        for src_key, dest_key in (
            ("gross_profit_gaap", "gross_profit_gaap"),
            ("operating_income_gaap", "operating_income_gaap"),
            ("net_income_gaap", "net_income_gaap"),
        ):
            block = _metric_block(amounts.get(src_key), yoy=_amount(yoy_period, src_key), qoq=_amount(prior_period, src_key))
            if block:
                fin[dest_key] = block
        eps = _metric_block(amounts.get("eps_gaap"), yoy=_amount(yoy_period, "eps_gaap"), qoq=_amount(prior_period, "eps_gaap"))
        if eps:
            fin["eps_gaap"] = eps
        capex = _metric_block(amounts.get("capex_gross"), yoy=_amount(yoy_period, "capex_gross"), qoq=_amount(prior_period, "capex_gross"))
        if capex:
            fin["capex"] = capex
        if not fin:
            gaps.append({"period": period, "accessionNumber": item["accession"], "error": "解析后没有可写入的美元科目"})
            continue
        doc = empty_period_doc(
            ticker,
            period,
            calendar_quarter=info.calendar_quarter,
            period_start=info.period_start.isoformat(),
            period_end=info.period_end.isoformat(),
            release_at_utc=item["accepted"],
            accession=item["accession"],
            press_release_url=f"https://www.sec.gov/Archives/edgar/data/{int(cfg['cik'])}/{item['accession'].replace('-', '')}/",
            eps_basis=cfg.get("eps_basis") or "gaap",
        )
        doc["meta"]["sec_form"] = "6-K"
        doc["meta"]["reporting_currency"] = spec.reporting_currency
        doc["meta"]["accounting_basis"] = spec.accounting_basis
        doc["meta"]["extract_method"] = "adr_press"
        warnings = [
            "历史回补：数字来自 SEC 6-K 新闻稿。只写入写明的美元金额和 US$ per ADR。",
            "没有美元对照的台币科目留空，不按汇率换算。",
            *list(statement.get("warnings") or []),
        ]
        doc["status"] = {"stage": "backfill_press", "needs_review": False, "warnings": warnings}
        doc["financials"] = fin
        doc["scorecard"] = build_scorecard(
            revenue_actual=(fin.get("revenue") or {}).get("value"),
            revenue_estimate=None,
            eps_actual=(fin.get("eps_gaap") or {}).get("value"),
            eps_estimate=None,
            eps_basis="gaap",
            gross_margin=(fin.get("gross_margin_gaap") or {}).get("value"),
            operating_margin=(fin.get("operating_margin_gaap") or {}).get("value"),
        )
        doc["drivers"] = {"stage": 0, "metrics": [], "status": "历史回补不含变动原因"}
        save_period_json(ticker, period, doc)
        period_raw = data_dir(ticker) / "raw" / period / "press_release.html"
        if not period_raw.exists():
            period_raw.parent.mkdir(parents=True, exist_ok=True)
            period_raw.write_bytes(Path(item["raw"]).read_bytes())
        written.append(period)
        seen.add(period)
    return {
        "listing": "adr",
        "source": "sec_6k_press_release",
        "xbrl_used": False,
        "llm_used": False,
        "written": written,
        "skipped": skipped,
        "gaps": gaps,
        "start": start,
    }
