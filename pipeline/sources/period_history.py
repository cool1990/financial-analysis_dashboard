"""从已有 FY*.json Stage1 组装 statement_history / segment_history（不联网、不调 LLM）。

缺字段的季度留空，不补数。地区拆分 period JSON 里没有就 geo 留空。
业务柱只在 financials.kpis 里出现可加总的 *_revenue 时写入；否则 segment lines 为空。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pipeline.config import data_dir
from pipeline.sources.tencent_statement import parse_tencent_statement
from pipeline.sources.tsmc_slides import build_tsmc_histories

_PERIOD_FILE = re.compile(r"^FY(20\d{2})Q([1-4])$")

# kpi key → (stack key, 中文名)。未列出的 *_revenue 用 key 本身做名。
_KPI_SEGMENT_NAMES: dict[str, tuple[str, str]] = {
    "vas_revenue": ("vas", "增值服务"),
    "advertising_revenue": ("marketing", "营销服务"),
    "fintech_biz_revenue": ("fintech", "金融科技及企业服务"),
    "dram_revenue": ("dram", "DRAM"),
    "nand_revenue": ("nand", "NAND"),
    "aws_revenue": ("aws", "AWS"),
    "reality_labs_revenue": ("rl", "Reality Labs"),
}


def _next(period: str) -> str:
    year = int(period[2:6])
    quarter = int(period[-1]) + 1
    if quarter == 5:
        year += 1
        quarter = 1
    return f"FY{year}Q{quarter}"


def _span(start: str, end: str) -> list[str]:
    labels: list[str] = []
    cursor = start
    while True:
        labels.append(cursor)
        if cursor == end:
            return labels
        cursor = _next(cursor)


def _money(value: Any) -> int | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(round(number))


def _block_value(fin: dict[str, Any], key: str) -> int | None:
    block = fin.get(key)
    if not isinstance(block, dict):
        return None
    return _money(block.get("value"))


def _block_float(fin: dict[str, Any], key: str) -> float | None:
    block = fin.get(key)
    if not isinstance(block, dict):
        return None
    value = block.get("value")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _kpi_amount(kpis: dict[str, Any], name: str) -> int | None:
    item = kpis.get(name)
    if not isinstance(item, dict):
        return None
    if item.get("value") is not None:
        return _money(item.get("value"))
    raw = item.get("raw")
    if raw is None or raw == "":
        return None
    unit = str(item.get("unit") or "").lower()
    try:
        number = float(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None
    if unit in {"millions", "million", "mn", "m"}:
        number *= 1_000_000
    elif unit in {"billions", "billion", "bn", "b"}:
        number *= 1_000_000_000
    elif unit in {"thousands", "thousand", "k"}:
        number *= 1_000
    return int(round(number))


def _period_end(period: str, doc: dict[str, Any]) -> str:
    meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
    end = meta.get("period_end")
    if isinstance(end, str) and len(end) >= 10:
        return end[:10]
    year = int(period[2:6])
    quarter = int(period[-1])
    month = quarter * 3
    # 回退：自然季末（仅在 meta 缺失时）
    day = {3: 31, 6: 30, 9: 30, 12: 31}[month]
    return f"{year:04d}-{month:02d}-{day:02d}"


def _segment_lines(revenue: int | None, kpis: dict[str, Any]) -> list[dict[str, Any]]:
    if revenue is None or revenue <= 0 or not kpis:
        return []
    parts: list[tuple[str, str, int]] = []
    for name, item in kpis.items():
        if not name.endswith("_revenue"):
            continue
        amount = _kpi_amount(kpis, name)
        if amount is None:
            continue
        key, label = _KPI_SEGMENT_NAMES.get(name, (name.removesuffix("_revenue"), name.removesuffix("_revenue")))
        parts.append((key, label, amount))
    if len(parts) < 2:
        return []
    lines = []
    for key, label, value in parts:
        lines.append(
            {
                "key": key,
                "name": label,
                "value": value,
                "depth": 0,
                "stack": True,
                "share_pct": round(value / revenue * 100, 1),
            }
        )
    return lines


def _quarter_from_doc(period: str, doc: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    fin = doc.get("financials") if isinstance(doc.get("financials"), dict) else {}
    kpis = fin.get("kpis") if isinstance(fin.get("kpis"), dict) else {}
    end = _period_end(period, doc)
    revenue = _block_value(fin, "revenue")
    gross = _block_value(fin, "gross_profit_gaap")
    margin = _block_float(fin, "gross_margin_gaap")

    statement: dict[str, Any] = {
        "period": period,
        "end": end,
        "geo": [],
        "geo_method": "undisclosed",
    }
    if revenue is None:
        statement.update(
            {
                "revenue": None,
                "revenue_method": "missing",
                "cost_of_revenue": None,
                "cost_method": "missing",
            }
        )
    else:
        statement["revenue"] = revenue
        statement["revenue_method"] = "period"
        # 营业成本不写进 period 历史：留给页面用新闻稿对比列 / 毛利率倒算，才能带出同比环比。
        statement["cost_of_revenue"] = None
        statement["cost_method"] = "missing"
        if gross is None and margin is not None:
            gross = int(round(revenue * margin))
        if gross is not None:
            statement["gross_profit"] = gross

    lines = _segment_lines(revenue, kpis)
    segment = {
        "period": period,
        "end": end,
        "method": "period_kpis" if lines else "missing",
        "lines": lines,
    }
    return statement, segment


def build_histories_from_periods(
    folder: Path,
    *,
    ticker: str,
    currency: str = "USD",
    unit: str = "美元",
) -> tuple[dict[str, Any], dict[str, Any]]:
    by_period: dict[str, dict[str, Any]] = {}
    for path in sorted(folder.glob("FY*.json")):
        if not _PERIOD_FILE.match(path.stem):
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        statement, segment = _quarter_from_doc(path.stem, doc)
        by_period[path.stem] = {"statement": statement, "segment": segment}

    if not by_period:
        raise ValueError(f"{folder} 下没有可用的 FY*Q*.json")

    ordered = _span(min(by_period), max(by_period))
    quarters: list[dict[str, Any]] = []
    segment_quarters: list[dict[str, Any]] = []
    for period in ordered:
        packed = by_period.get(period)
        if packed is None:
            quarters.append(
                {
                    "period": period,
                    "end": None,
                    "revenue": None,
                    "revenue_method": "missing",
                    "cost_of_revenue": None,
                    "cost_method": "missing",
                    "geo": [],
                    "geo_method": "undisclosed",
                }
            )
            segment_quarters.append({"period": period, "end": None, "method": "missing", "lines": []})
            continue
        quarters.append(packed["statement"])
        segment_quarters.append(packed["segment"])

    source = (
        f"金额来自 data/{ticker} 各季 Stage1 JSON（不联网、不调模型）。"
        "营业收入取 financials.revenue；有毛利或毛利率时写入毛利。营业成本留给建站时从新闻稿或毛利率倒算，以便保留同比环比。"
        "缺季度文件或字段时留空。地区拆分未写入 period JSON，geo 留空。"
    )
    statement = {
        "ticker": ticker,
        "source": source,
        "currency": currency,
        "unit": unit,
        "quarters": quarters,
    }
    segment = {
        "ticker": ticker,
        "source": "业务分部取自各季 financials.kpis 中带 _revenue 的项；不足两项则留空。没有地区拆分。",
        "quarters": segment_quarters,
        "years": [],
    }
    return statement, segment


def write_period_histories(
    folder: Path,
    *,
    ticker: str,
    currency: str = "USD",
    unit: str = "美元",
    write_segment: bool = True,
) -> tuple[Path, Path | None]:
    statement, segment = build_histories_from_periods(
        folder, ticker=ticker, currency=currency, unit=unit
    )
    statement_path = folder / "statement_history.json"
    statement_path.write_text(json.dumps(statement, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    segment_path = folder / "segment_history.json"
    if write_segment:
        # 有任意一季 lines 才值得写；否则仍写空壳，页面按业务全 gap。
        segment_path.write_text(json.dumps(segment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return statement_path, segment_path
    return statement_path, None


# --- 港股 / ADR：只汇总已经写明的数字。港股「其他」不倒挤。ADR 不把新台币换算成美元。 ---

def load_period_docs(ticker: str, *, start: str = "FY2016Q1") -> dict[str, dict[str, Any]]:
    folder = data_dir(ticker)
    docs: dict[str, dict[str, Any]] = {}
    if not folder.exists():
        return docs
    for path in sorted(folder.glob("FY*.json")):
        if path.stem < start:
            continue
        try:
            docs[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return docs


def _whole(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    rounded = round(number)
    if abs(number - rounded) < 0.5:
        return int(rounded)
    return number


def _metric(financials: dict[str, Any], key: str) -> int | float | None:
    node = financials.get(key)
    if not isinstance(node, dict):
        return None
    return _whole(node.get("value"))


def _kpi(financials: dict[str, Any], key: str) -> int | float | None:
    node = (financials.get("kpis") or {}).get(key)
    if not isinstance(node, dict):
        return None
    return _whole(node.get("value"))


def _label_span(start: str, end: str) -> list[str]:
    labels = []
    year = int(start[2:6])
    quarter = int(start[-1])
    last_year = int(end[2:6])
    last_quarter = int(end[-1])
    while (year, quarter) <= (last_year, last_quarter):
        labels.append(f"FY{year}Q{quarter}")
        quarter += 1
        if quarter == 5:
            year += 1
            quarter = 1
    return labels


def _end_of(period: str, docs: dict[str, dict[str, Any]]) -> str:
    meta = (docs.get(period) or {}).get("meta") or {}
    if meta.get("period_end"):
        return str(meta["period_end"])[:10]
    year = int(period[2:6])
    quarter = int(period[-1])
    month = quarter * 3
    day = 31 if month in {3, 12} else 30
    return f"{year:04d}-{month:02d}-{day:02d}"


_HK_SEGMENTS = (
    ("vas", "增值服务", "vas_revenue"),
    ("marketing", "营销服务", "advertising_revenue"),
    ("fintech", "金融科技及企业服务", "fintech_biz_revenue"),
)


def _hk_from_doc(period: str, doc: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    financials = doc.get("financials") or {}
    revenue = _metric(financials, "revenue")
    gross = _metric(financials, "gross_profit_gaap")
    cost = None
    cost_method = "missing"
    if revenue is not None and gross is not None:
        cost = _whole(float(revenue) - float(gross))
        cost_method = "revenue_minus_gross_profit"
    opex_lines = []
    selling = _kpi(financials, "selling_expense")
    admin = _kpi(financials, "ga_expense")
    if selling is not None:
        opex_lines.append({"key": "selling", "name": "销售及市场推广", "value": abs(int(selling))})
    if admin is not None:
        opex_lines.append({"key": "ga", "name": "一般及行政", "value": abs(int(admin))})
    opex = _whole(sum(float(line["value"]) for line in opex_lines)) if len(opex_lines) == 2 else None
    end = _end_of(period, {period: doc})
    statement = {
        "period": period,
        "end": end,
        "revenue": revenue,
        "revenue_method": "period" if revenue is not None else "missing",
        "cost_of_revenue": cost,
        "cost_method": cost_method,
        "gross_profit": gross,
        "opex": opex,
        "opex_method": "period_components" if opex is not None else "missing",
        "opex_lines": opex_lines,
        "geo": [],
        "geo_method": "undisclosed",
    }
    lines = []
    for key, name, kpi_key in _HK_SEGMENTS:
        value = _kpi(financials, kpi_key)
        if value is None:
            continue
        lines.append(
            {
                "key": key,
                "name": name,
                "value": value,
                "depth": 0,
                "stack": True,
                "share_pct": round(float(value) / float(revenue) * 100, 1) if revenue else None,
            }
        )
    segment = {
        "period": period,
        "end": end,
        "method": "period" if lines else "missing",
        "lines": lines,
    }
    return statement, segment


def _press_rows(ticker: str) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    folder = data_dir(ticker) / "raw"
    statements: dict[str, dict[str, Any]] = {}
    segments: dict[str, dict[str, Any]] = {}
    if not folder.exists():
        return statements, segments
    for path in sorted(folder.glob("FY*/press_release.txt")):
        try:
            statement, segment = parse_tencent_statement(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for row in statement.get("quarters") or []:
            if row.get("revenue") is None:
                continue
            statements[row["period"]] = row
        for row in segment.get("quarters") or []:
            if row.get("lines"):
                segments[row["period"]] = row
    return statements, segments


def build_hk_histories_from_periods(
    ticker: str,
    docs: dict[str, dict[str, Any]] | None = None,
    *,
    start: str = "FY2016Q1",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """季度 JSON 为主。业绩稿简明表能读出的季度覆盖上去，空列不盖住已有数字。"""
    loaded = docs if docs is not None else load_period_docs(ticker, start=start)
    press_statement, press_segment = _press_rows(ticker) if docs is None else ({}, {})
    periods = sorted(set(loaded) | set(press_statement))
    periods = [period for period in periods if period >= start]
    statement_rows = []
    segment_rows = []
    if periods:
        for period in _label_span(periods[0], periods[-1]):
            if period in press_statement:
                statement_rows.append(press_statement[period])
                segment_rows.append(
                    press_segment.get(period)
                    or {"period": period, "end": press_statement[period]["end"], "method": "missing", "lines": []}
                )
                continue
            doc = loaded.get(period)
            if doc is None:
                end = _end_of(period, loaded)
                statement_rows.append(
                    {
                        "period": period,
                        "end": end,
                        "revenue": None,
                        "revenue_method": "missing",
                        "cost_of_revenue": None,
                        "cost_method": "missing",
                        "geo": [],
                        "geo_method": "undisclosed",
                    }
                )
                segment_rows.append({"period": period, "end": end, "method": "missing", "lines": []})
                continue
            statement, segment = _hk_from_doc(period, doc)
            statement_rows.append(statement)
            segment_rows.append(segment)
    source = (
        "人民币金额来自已保存的季度 JSON，不覆盖这些文件。"
        "收入、毛利取季度 JSON。营业成本仅在两者都有时用收入减毛利。"
        "营业费用只在销售及市场推广、一般及行政都有时相加。"
        "业务分部只取季度 JSON 里已经抽出的增值服务、营销服务、金融科技及企业服务；"
        "没有单独披露的「其他」不倒挤。"
        "业绩稿简明综合收益表能读出的季度改用表内原数，包括表里的其他。"
        "没有季度文件、也没有表内数字的季度留空。地区收入未披露。不把港币或美元写进人民币字段。"
    )
    statement = {
        "ticker": ticker.upper(),
        "source": source,
        "currency": "CNY",
        "unit": "人民币",
        "quarters": statement_rows,
    }
    segment = {
        "ticker": ticker.upper(),
        "source": "业务分部来自季度 JSON 的收入分项；简明表里有「其他」时沿用表内原数。没有地区拆分。",
        "quarters": segment_rows,
        "years": [],
    }
    return statement, segment


def _official_usd_quote(quote: Any) -> bool:
    text = str(quote or "")
    if "per ADR" in text:
        return True
    if "US$" in text or "In US dollars" in text or "in US dollars" in text:
        return True
    return "$" in text and "NT$" not in text


def _blank_quarter(period: str, end: str) -> dict[str, Any]:
    return {
        "period": period,
        "end": end,
        "revenue": None,
        "revenue_method": "missing",
        "cost_of_revenue": None,
        "cost_method": "missing",
        "geo": [],
        "geo_method": "undisclosed",
    }


def _adr_row_from_doc(period: str, doc: dict[str, Any]) -> dict[str, Any] | None:
    financials = doc.get("financials") or {}
    revenue_node = financials.get("revenue") if isinstance(financials.get("revenue"), dict) else {}
    if not _official_usd_quote(revenue_node.get("source_quote")):
        return None
    revenue = _metric(financials, "revenue")
    if revenue is None:
        return None
    row = _blank_quarter(period, _end_of(period, {period: doc}))
    row["revenue"] = revenue
    row["revenue_method"] = "press_release"
    margin = financials.get("gross_margin_gaap") if isinstance(financials.get("gross_margin_gaap"), dict) else {}
    if margin.get("value") is not None and _official_usd_quote(margin.get("source_quote")) or (
        margin.get("value") is not None and "Gross margin" in str(margin.get("source_quote") or "")
    ):
        ratio = float(margin["value"])
        gross = _whole(float(revenue) * ratio)
        row["gross_margin"] = ratio
        row["gross_profit"] = gross
        row["cost_of_revenue"] = _whole(float(revenue) - float(gross)) if gross is not None else None
        row["cost_method"] = "revenue_minus_gross_profit"
    operating = financials.get("operating_margin_gaap") if isinstance(financials.get("operating_margin_gaap"), dict) else {}
    if operating.get("value") is not None and "operating margin" in str(operating.get("source_quote") or "").lower():
        row["operating_margin"] = float(operating["value"])
        row["operating_income"] = _whole(float(revenue) * float(operating["value"]))
        row["operating_income_method"] = "press_times_margin"
    net = financials.get("net_income_gaap") if isinstance(financials.get("net_income_gaap"), dict) else {}
    if net.get("value") is not None and _official_usd_quote(net.get("source_quote")):
        row["net_income"] = _metric(financials, "net_income_gaap")
        row["net_income_method"] = "press_release"
    eps = financials.get("eps_gaap") if isinstance(financials.get("eps_gaap"), dict) else {}
    if eps.get("value") is not None and "per ADR" in str(eps.get("source_quote") or ""):
        row["eps_adr_usd"] = float(eps["value"])
        row["eps_adr_method"] = "press_release"
    return row


def extend_adr_history_from_periods(
    statement: dict[str, Any],
    segment: dict[str, Any],
    docs: dict[str, dict[str, Any]],
    *,
    start: str = "FY2016Q1",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """幻灯片已有的金额不改。只补空营收，并补上幻灯片范围外、新闻稿写明美元的季度。"""
    rows = {row["period"]: dict(row) for row in statement.get("quarters") or []}
    seg_rows = {row["period"]: row for row in segment.get("quarters") or []}
    for period, doc in docs.items():
        if period < start:
            continue
        filled = _adr_row_from_doc(period, doc)
        if filled is None:
            continue
        current = rows.get(period)
        if current is None:
            rows[period] = filled
            continue
        if current.get("revenue") is None and filled.get("revenue") is not None:
            current["revenue"] = filled["revenue"]
            current["revenue_method"] = "press_release"
            for key in (
                "gross_margin",
                "gross_profit",
                "cost_of_revenue",
                "cost_method",
                "operating_margin",
                "operating_income",
                "operating_income_method",
                "net_income",
                "net_income_method",
            ):
                if current.get(key) in (None, "", "missing") and filled.get(key) is not None:
                    current[key] = filled[key]
        if current.get("eps_adr_usd") is None and filled.get("eps_adr_usd") is not None:
            current["eps_adr_usd"] = filled["eps_adr_usd"]
            current["eps_adr_method"] = "press_release"
        rows[period] = current
    if not rows:
        return statement, segment
    ordered_periods = _label_span(min(rows), max(rows))
    quarters = []
    segment_quarters = []
    for period in ordered_periods:
        row = rows.get(period) or _blank_quarter(period, _end_of(period, docs))
        quarters.append(row)
        existing = seg_rows.get(period)
        if existing is not None:
            segment_quarters.append(existing)
        else:
            segment_quarters.append(
                {"period": period, "end": row.get("end"), "method": "missing", "lines": []}
            )
    statement = dict(statement)
    statement["quarters"] = quarters
    note = "幻灯片没有覆盖、或当季营收为空时，只用新闻稿写明的美元金额和 US$ per ADR 补上，不按汇率把新台币换算成美元。"
    source = str(statement.get("source") or "")
    if note not in source:
        statement["source"] = source + note
    segment = dict(segment)
    segment["quarters"] = segment_quarters
    return statement, segment


def load_tsmc_slide_histories(ticker: str = "TSM") -> tuple[dict[str, Any], dict[str, Any]] | None:
    folder = data_dir(ticker)
    paths = sorted((folder / "raw").glob("FY*/presentation.html"))
    if not paths:
        return None
    return build_tsmc_histories(paths, cik="0001046179")
