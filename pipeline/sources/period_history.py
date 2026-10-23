"""从已有 FY*.json Stage1 组装 statement_history / segment_history（不联网、不调 LLM）。

缺字段的季度留空，不补数。地区拆分 period JSON 里没有就 geo 留空。
业务柱只在 financials.kpis 里出现可加总的 *_revenue 时写入；否则 segment lines 为空。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

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
