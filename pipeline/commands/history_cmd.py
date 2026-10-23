"""生成 data/{TICKER}/segment_history.json、statement_history.json、xbrl_activity.json。"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

from pipeline.compute.periods import build_period_info, make_fiscal_period
from pipeline.config import data_dir, list_tickers, load_ticker_config
from pipeline.sources.history_filings import collect_filings, latest_accession, list_periodic_filings
from pipeline.sources.sec_edgar import SecEdgarClient
from pipeline.sources.segment_history import assemble_geography, assemble_periods, fiscal_quarter_key
from pipeline.sources.xbrl_history import COST_OF_REVENUE_TAGS, REVENUE_TAGS, quarterly_values

SKIP_LISTINGS = {"hk", "adr"}
HISTORY_FILES = ("segment_history.json", "statement_history.json", "xbrl_activity.json")

STATEMENT_SOURCE = (
    "营收、营业成本、研发、销售、管理费用来自 SEC companyfacts 的单季 us-gaap。"
    "毛利为营收减营业成本，申报没有单独的毛利科目，也没有把营业成本再拆开。"
    "地区收入来自 10-Q/10-K XBRL 的地区轴。"
    "第四季没有单季事实时用全年减前九个月；地区口径对不上时留空。"
)
OPEX_SPECS = (
    ("rd", "研发", ["ResearchAndDevelopmentExpense"]),
    ("sales", "销售", ["SellingAndMarketingExpense"]),
    ("ga", "管理", ["GeneralAndAdministrativeExpense"]),
)


def skip_reason(cfg: dict[str, Any]) -> str | None:
    listing = str(cfg.get("listing") or "").strip().lower()
    if listing in SKIP_LISTINGS:
        return f"listing={listing}，不从 SEC 10-Q/10-K 生成历史"
    if not str(cfg.get("cik") or "").strip():
        return "没有 CIK"
    return None


def _now_stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _whole(value: float | int | None) -> int | float | None:
    if value is None:
        return None
    number = float(value)
    rounded = round(number)
    if abs(number - rounded) < 0.5:
        return int(rounded)
    return number


def _clean_lines(lines: list[dict[str, Any]], *, keep_tree: bool) -> list[dict[str, Any]]:
    cleaned = []
    for line in lines:
        item = {
            "key": line["key"],
            "name": line["name"],
            "value": _whole(line.get("value")),
        }
        if keep_tree:
            item["depth"] = int(line.get("depth") or 0)
            item["stack"] = bool(line.get("stack"))
        cleaned.append(item)
    return cleaned


def _segment_source(quarters: list[dict[str, Any]]) -> str:
    bits = ["SEC EDGAR 10-Q/10-K XBRL。"]
    table_periods = [row["period"] for row in quarters if row.get("method") == "table"]
    if table_periods == ["FY2016Q1", "FY2016Q2", "FY2016Q3"]:
        bits.append("2016 年前三季来自 10-Q 收入构成表。")
    elif table_periods:
        bits.append("部分季度来自 10-Q 收入构成表。")
    if any(row.get("method") == "residual" for row in quarters):
        bits.append("部分第四季是全年减去前九个月或前三季。")
    return "".join(bits)


def _label_segment_rows(
    rows: list[dict[str, Any]],
    fiscal_year_end_month: int,
    start: str,
    *,
    annual: bool,
) -> list[dict[str, Any]]:
    best: dict[str, tuple[int, dict[str, Any]]] = {}
    for row in rows:
        end = str(row.get("end") or "")
        if not end or end < start:
            continue
        key = fiscal_quarter_key(end, fiscal_year_end_month)
        if key is None:
            continue
        fiscal_year, fiscal_quarter = key
        if annual and fiscal_quarter != 4:
            continue
        period = f"FY{fiscal_year}" if annual else make_fiscal_period(fiscal_year, fiscal_quarter)
        anchor = build_period_info(end, fiscal_year_end_month).period_end
        distance = abs((date.fromisoformat(end) - anchor).days)
        chosen = {
            "period": period,
            "end": end,
            "method": row.get("method") or "",
            "filed": row.get("filed") or "",
            "form": row.get("form") or "",
            "accession": row.get("accession") or "",
            "lines": _clean_lines(row.get("lines") or [], keep_tree=True),
        }
        prev = best.get(period)
        if prev is None or distance < prev[0]:
            best[period] = (distance, chosen)
    labeled = [item[1] for item in best.values()]
    labeled.sort(key=lambda item: item["end"])
    return labeled


def _series_node(series: dict[date, dict[str, Any]], end: date) -> tuple[int | float | None, str | None]:
    node = series.get(end)
    if not node or node.get("value") is None:
        return None, None
    return _whole(node["value"]), "residual" if node.get("derived") else "xbrl"


def _statement_quarters(
    facts: dict[str, Any],
    geo_rows: list[dict[str, Any]],
    fiscal_year_end_month: int,
    start: str,
) -> list[dict[str, Any]]:
    revenue = quarterly_values(facts, REVENUE_TAGS, "USD")
    cost = quarterly_values(facts, COST_OF_REVENUE_TAGS, "USD")
    opex_series = [(key, name, quarterly_values(facts, tags, "USD")) for key, name, tags in OPEX_SPECS]
    geo_best: dict[str, tuple[int, dict[str, Any]]] = {}
    for row in geo_rows:
        end = str(row.get("end") or "")
        if not end or end < start:
            continue
        key = fiscal_quarter_key(end, fiscal_year_end_month)
        if key is None:
            continue
        period = make_fiscal_period(*key)
        anchor = build_period_info(end, fiscal_year_end_month).period_end
        distance = abs((date.fromisoformat(end) - anchor).days)
        prev = geo_best.get(period)
        if prev is None or distance < prev[0]:
            geo_best[period] = (distance, row)

    ends: set[date] = set()
    for series in (revenue, cost, *(item[2] for item in opex_series)):
        ends.update(series)
    for _period, (_distance, row) in geo_best.items():
        ends.add(date.fromisoformat(row["end"]))

    by_period: dict[str, dict[str, Any]] = {}
    for end in sorted(ends):
        if end.isoformat() < start:
            continue
        key = fiscal_quarter_key(end.isoformat(), fiscal_year_end_month)
        if key is None:
            continue
        period = make_fiscal_period(*key)
        anchor = build_period_info(end, fiscal_year_end_month).period_end
        distance = abs((end - anchor).days)
        rev_value, rev_method = _series_node(revenue, end)
        cost_value, cost_method = _series_node(cost, end)
        gross = None
        if rev_value is not None and cost_value is not None:
            gross = _whole(float(rev_value) - float(cost_value))
        opex_lines = []
        for line_key, line_name, series in opex_series:
            value, _method = _series_node(series, end)
            if value is None:
                continue
            opex_lines.append({"key": line_key, "name": line_name, "value": value})
        opex = _whole(sum(float(line["value"]) for line in opex_lines)) if len(opex_lines) == len(OPEX_SPECS) else None
        geo_row = (geo_best.get(period) or (None, None))[1]
        record = {
            "period": period,
            "end": end.isoformat(),
            "revenue": rev_value,
            "revenue_method": rev_method,
            "cost_of_revenue": cost_value,
            "cost_method": cost_method,
            "gross_profit": gross,
            "opex_lines": opex_lines,
            "opex": opex,
            "geo": None if geo_row is None else _clean_lines(geo_row.get("lines") or [], keep_tree=False),
            "geo_method": None if geo_row is None else geo_row.get("method") or "",
        }
        prev = by_period.get(period)
        if prev is None or distance < prev[0]:
            by_period[period] = (distance, record)
    rows = [item[1] for item in by_period.values()]
    rows.sort(key=lambda item: item["end"])
    return rows


def _activity_rows(
    facts: dict[str, Any],
    fiscal_year_end_month: int,
    start: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    gaap = (facts.get("facts") or {}).get("us-gaap") or {}

    def _accept_end(end: str) -> bool:
        if not end or end < start:
            return False
        return fiscal_quarter_key(end, fiscal_year_end_month) is not None

    share_rows = ((gaap.get("WeightedAverageNumberOfDilutedSharesOutstanding") or {}).get("units") or {}).get("shares") or []
    shares_best: dict[str, dict[str, Any]] = {}
    for row in share_rows:
        start_on = str(row.get("start") or "")[:10]
        end = str(row.get("end") or "")[:10]
        if not start_on or not _accept_end(end) or row.get("val") is None:
            continue
        try:
            days = (date.fromisoformat(end) - date.fromisoformat(start_on)).days
        except ValueError:
            continue
        if not 80 <= days <= 100:
            continue
        prev = shares_best.get(end)
        if prev is None or str(row.get("filed") or "") >= str(prev.get("filed") or ""):
            shares_best[end] = row
    shares = [
        {
            "start": str(row.get("start") or "")[:10],
            "end": end,
            "value": _whole(row.get("val")),
            "form": row.get("form") or "",
            "filed": str(row.get("filed") or "")[:10],
        }
        for end, row in sorted(shares_best.items())
    ]

    ar_rows = ((gaap.get("AccountsReceivableNetCurrent") or {}).get("units") or {}).get("USD") or []
    ar_best: dict[str, dict[str, Any]] = {}
    for row in ar_rows:
        if row.get("start") or row.get("val") is None:
            continue
        end = str(row.get("end") or "")[:10]
        if not _accept_end(end):
            continue
        prev = ar_best.get(end)
        if prev is None or str(row.get("filed") or "") >= str(prev.get("filed") or ""):
            ar_best[end] = row
    receivables = [
        {
            "end": end,
            "value": _whole(row.get("val")),
            "form": row.get("form") or "",
            "filed": str(row.get("filed") or "")[:10],
        }
        for end, row in sorted(ar_best.items())
    ]
    return shares, receivables


def build_history_documents(
    ticker: str,
    cfg: dict[str, Any],
    segment_filings: list[dict[str, Any]],
    geo_filings: list[dict[str, Any]],
    company_facts: dict[str, Any],
    *,
    latest: str,
    generated_at: str,
    start: str = "2016-01-01",
) -> dict[str, dict[str, Any]]:
    """三份文件。没有事实的季度不补数。"""
    fiscal_year_end_month = int(cfg.get("fiscal_year_end_month") or 12)
    cik = str(cfg.get("cik") or "").zfill(10)
    assembled = assemble_periods(segment_filings, fiscal_year_end_month)
    quarters = _label_segment_rows(assembled.get("quarters") or [], fiscal_year_end_month, start, annual=False)
    years = _label_segment_rows(assembled.get("years") or [], fiscal_year_end_month, start, annual=True)
    geography = assemble_geography(geo_filings, fiscal_year_end_month)
    segment = {
        "ticker": ticker.upper(),
        "cik": cik,
        "source": _segment_source(quarters),
        "generated_at": generated_at,
        "latest_accession": latest,
        "quarters": quarters,
        "years": years,
    }
    statement = {
        "ticker": ticker.upper(),
        "cik": cik,
        "source": STATEMENT_SOURCE,
        "generated_at": generated_at,
        "latest_accession": latest,
        "quarters": _statement_quarters(company_facts, geography, fiscal_year_end_month, start),
    }
    shares, receivables = _activity_rows(company_facts, fiscal_year_end_month, start)
    activity = {
        "source": f"SEC companyfacts CIK{cik}",
        "generated_at": generated_at,
        "latest_accession": latest,
        "diluted_shares_tag": "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
        "accounts_receivable_tag": "us-gaap:AccountsReceivableNetCurrent",
        "diluted_shares": shares,
        "accounts_receivable": receivables,
    }
    return {
        "segment_history": segment,
        "statement_history": statement,
        "xbrl_activity": activity,
    }


def _write_documents(ticker: str, documents: dict[str, dict[str, Any]]) -> list[str]:
    folder = data_dir(ticker)
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for name in HISTORY_FILES:
        key = name[: -len(".json")]
        path = folder / name
        path.write_text(json.dumps(documents[key], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(str(path))
    return written


def _stored_accession(ticker: str) -> str:
    for name in HISTORY_FILES:
        path = data_dir(ticker) / name
        if not path.exists():
            continue
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(loaded, dict) and loaded.get("latest_accession"):
            return str(loaded["latest_accession"])
    return ""


def _history_complete(ticker: str) -> bool:
    return all((data_dir(ticker) / name).exists() for name in HISTORY_FILES)


def refresh_history(
    ticker: str,
    *,
    start: str = "2016-01-01",
    client: SecEdgarClient | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """总是重写三份文件。港股和 ADR 跳过，不访问 SEC。"""
    cfg = load_ticker_config(ticker)
    reason = skip_reason(cfg)
    if reason:
        return {"ticker": ticker.upper(), "skipped": True, "reason": reason}
    sec = client or SecEdgarClient()
    rows = list_periodic_filings(sec, str(cfg["cik"]), start=start)
    if not rows:
        return {"ticker": ticker.upper(), "skipped": True, "reason": "没有 10-Q/10-K"}
    latest = latest_accession(rows)
    segment_filings, geo_filings, warnings = collect_filings(sec, str(cfg["cik"]), rows)
    facts = sec.company_facts(str(cfg["cik"]))
    stamp = generated_at or _now_stamp()
    documents = build_history_documents(
        ticker,
        cfg,
        segment_filings,
        geo_filings,
        facts,
        latest=latest,
        generated_at=stamp,
        start=start,
    )
    paths = _write_documents(ticker, documents)
    segment = documents["segment_history"]
    return {
        "ticker": ticker.upper(),
        "skipped": False,
        "generated_at": stamp,
        "latest_accession": latest,
        "quarters": len(segment.get("quarters") or []),
        "years": len(segment.get("years") or []),
        "statement_quarters": len(documents["statement_history"].get("quarters") or []),
        "warnings": warnings,
        "paths": paths,
    }


def refresh_history_if_new(
    ticker: str | None = None,
    *,
    start: str = "2016-01-01",
    client: SecEdgarClient | None = None,
) -> dict[str, Any]:
    """已有历史文件时，只在最新 10-Q/10-K accession 变了才重写。

    全池扫描（daily）不为还没有这三份文件的股票做首次全量抓取。
    指定 --ticker 时，文件不齐也会生成。
    """
    explicit = ticker is not None
    tickers = [ticker] if ticker else list_tickers()
    sec = client
    results = []
    for name in tickers:
        cfg = load_ticker_config(name)
        reason = skip_reason(cfg)
        if reason:
            results.append({"ticker": name.upper(), "skipped": True, "refreshed": False, "reason": reason})
            continue
        if not explicit and not _history_complete(name):
            results.append(
                {
                    "ticker": name.upper(),
                    "skipped": True,
                    "refreshed": False,
                    "reason": "还没有历史文件。用 history --ticker、Stage1 或 backfill --xbrl-only 首次生成",
                }
            )
            continue
        if sec is None:
            sec = SecEdgarClient()
        rows = list_periodic_filings(sec, str(cfg["cik"]), start=start)
        latest = latest_accession(rows)
        if not latest:
            results.append({"ticker": name.upper(), "skipped": True, "refreshed": False, "reason": "没有 10-Q/10-K"})
            continue
        stored = _stored_accession(name)
        if stored == latest and _history_complete(name):
            results.append(
                {
                    "ticker": name.upper(),
                    "skipped": False,
                    "refreshed": False,
                    "latest_accession": latest,
                    "reason": "没有新的 10-Q/10-K",
                }
            )
            continue
        refreshed = refresh_history(name, start=start, client=sec)
        refreshed["refreshed"] = not refreshed.get("skipped")
        results.append(refreshed)
    return {"results": results}

# 全池模式不覆盖已有 SEC / 幻灯片历史。指定 ticker 时仍按下面的规则重写。
_PERIOD_HISTORY_PRESERVE = {"META", "TSM"}


def _write_json(ticker: str, name: str, payload: dict[str, Any]) -> str:
    folder = data_dir(ticker)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def refresh_history_from_periods(
    ticker: str | None = None,
    *,
    start: str = "2016-01-01",
    client: SecEdgarClient | None = None,
) -> dict[str, Any]:
    """不修改季度 JSON，不调 LLM。

    指定美股时用 SEC companyfacts / 10-Q / 10-K 重建分部、报表、稀释股本和应收账款。
    港股、ADR 不访问 SEC，只汇总已有季度和新闻稿里已经写明的数字，缺的留空。
    不指定 ticker 时离线处理：META / TSM 和已有 statement_history 的标的保留；
    还没有这份文件的美股用 FY*.json 组装。
    """
    from pipeline.sources.period_history import (
        build_hk_histories_from_periods,
        extend_adr_history_from_periods,
        load_period_docs,
        load_tsmc_slide_histories,
        write_period_histories,
    )

    names = [ticker] if ticker else list_tickers()
    sec = client
    results: list[dict[str, Any]] = []
    for name in names:
        cfg = load_ticker_config(name)
        upper = name.upper()
        listing = str(cfg.get("listing") or "").strip().lower()
        folder = data_dir(upper)
        statement_path = folder / "statement_history.json"
        if ticker is None and upper in _PERIOD_HISTORY_PRESERVE and statement_path.exists():
            results.append({"ticker": upper, "skipped": True, "reason": "保留现有 SEC/幻灯片历史"})
            continue
        if listing == "hk":
            statement, segment = build_hk_histories_from_periods(name)
            paths = [
                _write_json(name, "statement_history.json", statement),
                _write_json(name, "segment_history.json", segment),
            ]
            results.append(
                {
                    "ticker": upper,
                    "skipped": False,
                    "source": "periods",
                    "quarters": len(statement.get("quarters") or []),
                    "paths": paths,
                }
            )
            continue
        if listing == "adr":
            docs = load_period_docs(name)
            loaded = load_tsmc_slide_histories(name) if upper == "TSM" else None
            if loaded is None:
                statement, segment = {"ticker": upper, "quarters": [], "source": ""}, {
                    "ticker": upper,
                    "quarters": [],
                    "years": [],
                    "source": "",
                }
            else:
                statement, segment = loaded
            statement, segment = extend_adr_history_from_periods(statement, segment, docs)
            paths = [
                _write_json(name, "statement_history.json", statement),
                _write_json(name, "segment_history.json", segment),
            ]
            results.append(
                {
                    "ticker": upper,
                    "skipped": False,
                    "source": "periods",
                    "quarters": len(statement.get("quarters") or []),
                    "paths": paths,
                    "sec": False,
                }
            )
            continue
        if ticker is None and statement_path.exists():
            results.append({"ticker": upper, "skipped": True, "reason": "已有 statement_history.json"})
            continue
        if ticker is None:
            if not any(folder.glob("FY*.json")):
                results.append({"ticker": upper, "skipped": True, "reason": "没有 FY*.json"})
                continue
            currency = str(cfg.get("reporting_currency") or "USD")
            unit = "人民币" if currency == "CNY" else "美元"
            statement_file, segment_file = write_period_histories(
                folder, ticker=upper, currency=currency, unit=unit, write_segment=True
            )
            statement = json.loads(statement_file.read_text(encoding="utf-8"))
            segment = json.loads(segment_file.read_text(encoding="utf-8")) if segment_file else {"quarters": []}
            results.append(
                {
                    "ticker": upper,
                    "skipped": False,
                    "source": "period_json",
                    "statement_quarters": len(statement.get("quarters") or []),
                    "revenue_filled": sum(1 for q in (statement.get("quarters") or []) if q.get("revenue") is not None),
                    "segment_filled": sum(1 for q in (segment.get("quarters") or []) if q.get("lines")),
                    "paths": [str(statement_file)] + ([str(segment_file)] if segment_file else []),
                }
            )
            continue
        reason = skip_reason(cfg)
        if reason:
            results.append({"ticker": upper, "skipped": True, "reason": reason})
            continue
        if sec is None:
            sec = SecEdgarClient()
        refreshed = refresh_history(name, start=start, client=sec)
        refreshed["source"] = "sec"
        results.append(refreshed)
    return {"results": results}
