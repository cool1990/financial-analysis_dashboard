"""从已保存的腾讯季度 JSON（及可选新闻稿）生成 statement / segment 历史。

优先扫 data/0700.HK/FY*.json：营业收入、毛利取 Stage1；收入成本 = 收入 − 毛利；
营业费用 = 销售及市场推广 + 一般及行政（kpis）。业务柱取 vas / 广告(营销) / 金融科技 kpis，
「其他」为收入减去已披露分项的残差（非负才写入）。2017–2018 未单独披露金融科技时，
残差就是当年报里的「其他」桶，不另造数。

单季新闻稿解析仍保留，便于核对最新一季表内原数；写盘时以全部季度 JSON 为准。
地区收入没有，geo 留空。缺字段的季度留空，不补数。
"""

from __future__ import annotations

import calendar
import json
import re
from datetime import date
from pathlib import Path
from typing import Any


_COLUMNS = re.compile(r"([1-4])Q(20\d{2})")
_AMOUNT = re.compile(r"\(?\d[\d,]*\)?")
_PERIOD_FILE = re.compile(r"^FY(20\d{2})Q([1-4])$")
_LABELS = {
    "收入": "revenue",
    "增值服务": "vas",
    "营销服务": "marketing",
    "其他": "other",
    "收入成本": "cost",
    "毛利": "gross_profit",
    "销售及市场推广开支": "selling",
    "一般及行政开支": "admin",
    # 金融科技行名较长，下面用 startswith 匹配
}
_SEGMENTS = (
    ("vas", "增值服务"),
    ("marketing", "营销服务"),
    ("fintech", "金融科技及企业服务"),
    ("other", "其他"),
)


def _end(year: int, quarter: int) -> str:
    month = quarter * 3
    return date(year, month, calendar.monthrange(year, month)[1]).isoformat()


def _period(quarter: int, year: int) -> str:
    return f"FY{year}Q{quarter}"


def _next(period: str) -> str:
    year = int(period[2:6])
    quarter = int(period[-1]) + 1
    if quarter == 5:
        year += 1
        quarter = 1
    return f"FY{year}Q{quarter}"


def _span(start: str, end: str) -> list[str]:
    labels = []
    cursor = start
    while True:
        labels.append(cursor)
        if cursor == end:
            return labels
        cursor = _next(cursor)


def _amount(token: str) -> int:
    negative = token.startswith("(") and token.endswith(")")
    number = int(token.strip("()").replace(",", ""))
    if negative:
        number = -number
    return number * 1_000_000


def _statement_text(text: str) -> str:
    start = text.find("简明综合收益表")
    if start < 0:
        raise ValueError("新闻稿里没有简明综合收益表")
    body = text[start:]
    end = body.find("下列人士")
    if end > 0:
        body = body[:end]
    return body


def _columns(text: str) -> list[str]:
    header = ""
    for line in _statement_text(text).splitlines():
        found = _COLUMNS.findall(line)
        if len(found) >= 3:
            header = line
            break
    if not header:
        raise ValueError("新闻稿里没有 1Q/4Q 对照列")
    return [_period(int(quarter), int(year)) for quarter, year in _COLUMNS.findall(header)[:3]]


def _row_key(label: str) -> str | None:
    if label in _LABELS:
        return _LABELS[label]
    if label.startswith("金融科技"):
        return "fintech"
    return None


def _rows(text: str) -> dict[str, list[int]]:
    parsed: dict[str, list[int]] = {}
    for raw in _statement_text(text).splitlines():
        line = raw.strip()
        if not line:
            continue
        label = line.split()[0]
        key = _row_key(label)
        if key is None or key in parsed:
            continue
        amounts = _AMOUNT.findall(line[len(label) :])
        if len(amounts) < 3:
            continue
        parsed[key] = [_amount(token) for token in amounts[:3]]
    missing = [name for name in ("revenue", "cost", "gross_profit", "selling", "admin") if name not in parsed]
    if missing:
        raise ValueError("新闻稿收益表缺行: " + "、".join(missing))
    return parsed


def parse_tencent_statement(text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    periods = _columns(text)
    rows = _rows(text)
    by_period: dict[str, dict[str, Any]] = {}
    for index, period in enumerate(periods):
        year = int(period[2:6])
        quarter = int(period[-1])
        revenue = rows["revenue"][index]
        cost = abs(rows["cost"][index])
        selling = abs(rows["selling"][index])
        admin = abs(rows["admin"][index])
        by_period[period] = {
            "period": period,
            "end": _end(year, quarter),
            "revenue": revenue,
            "revenue_method": "press",
            "cost_of_revenue": cost,
            "cost_method": "press",
            "gross_profit": rows["gross_profit"][index],
            "opex": selling + admin,
            "opex_method": "press_components",
            "opex_lines": [
                {"key": "selling", "name": "销售及市场推广", "value": selling},
                {"key": "ga", "name": "一般及行政", "value": admin},
            ],
            "geo": [],
            "geo_method": "undisclosed",
            "segments": {
                key: rows[key][index]
                for key, _name in _SEGMENTS
                if key in rows
            },
        }
    ordered = _span(min(by_period), max(by_period))
    quarters = []
    segment_quarters = []
    for period in ordered:
        current = by_period.get(period)
        if current is None:
            year = int(period[2:6])
            quarter = int(period[-1])
            quarters.append(
                {
                    "period": period,
                    "end": _end(year, quarter),
                    "revenue": None,
                    "revenue_method": "missing",
                    "cost_of_revenue": None,
                    "cost_method": "missing",
                    "geo": [],
                    "geo_method": "undisclosed",
                }
            )
            segment_quarters.append({"period": period, "end": _end(year, quarter), "method": "missing", "lines": []})
            continue
        segments = current.pop("segments")
        revenue = current["revenue"]
        lines = []
        for key, name in _SEGMENTS:
            if key not in segments:
                continue
            value = segments[key]
            lines.append(
                {
                    "key": key,
                    "name": name,
                    "value": value,
                    "depth": 0,
                    "stack": True,
                    "share_pct": round(value / revenue * 100, 1) if revenue else None,
                }
            )
        quarters.append(current)
        segment_quarters.append(
            {
                "period": period,
                "end": current["end"],
                "method": "press" if lines else "missing",
                "lines": lines,
            }
        )
    source = (
        "人民币金额来自业绩新闻稿简明综合收益表，单位为百万元。"
        "营业收入、收入成本、毛利取表内原数。营业费用是销售及市场推广开支与一般及行政开支的绝对值合计，不含其他收益/亏损，不含营业成本。"
        "业务柱是增值服务、营销服务、金融科技及企业服务、其他，金额取自收入下的分项。"
        "表里没有的季度留空。地区收入这份表没有，geo 留空，不补数。"
    )
    statement = {
        "ticker": "0700.HK",
        "source": source,
        "currency": "CNY",
        "unit": "人民币",
        "quarters": quarters,
    }
    segment = {
        "ticker": "0700.HK",
        "source": "业务分部取自同一张收益表的收入分项。没有地区拆分。",
        "quarters": segment_quarters,
        "years": [],
    }
    return statement, segment


def _money(value: Any) -> int | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(round(number))


def _kpi_millions(kpis: dict[str, Any], name: str) -> int | None:
    item = kpis.get(name)
    if not isinstance(item, dict):
        return None
    raw = item.get("raw")
    if raw is None or raw == "":
        return None
    try:
        return int(round(float(str(raw).replace(",", "")) * 1_000_000))
    except (TypeError, ValueError):
        return None


def _block_value(fin: dict[str, Any], key: str) -> int | None:
    block = fin.get(key)
    if not isinstance(block, dict):
        return None
    return _money(block.get("value"))


def _segment_lines(revenue: int | None, kpis: dict[str, Any]) -> list[dict[str, Any]]:
    if revenue is None or revenue <= 0:
        return []
    vas = _kpi_millions(kpis, "vas_revenue")
    marketing = _kpi_millions(kpis, "advertising_revenue")
    fintech = _kpi_millions(kpis, "fintech_biz_revenue")
    parts: list[tuple[str, str, int]] = []
    if vas is not None:
        parts.append(("vas", "增值服务", vas))
    if marketing is not None:
        parts.append(("marketing", "营销服务", marketing))
    if fintech is not None:
        parts.append(("fintech", "金融科技及企业服务", fintech))
    if len(parts) < 2:
        return []
    known = sum(value for _key, _name, value in parts)
    residual = revenue - known
    if residual >= 0:
        parts.append(("other", "其他", residual))
    lines = []
    for key, name, value in parts:
        lines.append(
            {
                "key": key,
                "name": name,
                "value": value,
                "depth": 0,
                "stack": True,
                "share_pct": round(value / revenue * 100, 1),
            }
        )
    return lines


def _quarter_from_period_doc(period: str, doc: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    year = int(period[2:6])
    quarter = int(period[-1])
    end = _end(year, quarter)
    fin = doc.get("financials") if isinstance(doc.get("financials"), dict) else {}
    kpis = fin.get("kpis") if isinstance(fin.get("kpis"), dict) else {}
    revenue = _block_value(fin, "revenue")
    gross = _block_value(fin, "gross_profit_gaap")
    selling = _kpi_millions(kpis, "selling_expense")
    admin = _kpi_millions(kpis, "ga_expense")

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
        if gross is not None:
            statement["gross_profit"] = gross
            cost = revenue - gross
            if cost >= 0:
                statement["cost_of_revenue"] = cost
                statement["cost_method"] = "period"
            else:
                statement["cost_of_revenue"] = None
                statement["cost_method"] = "missing"
        else:
            statement["cost_of_revenue"] = None
            statement["cost_method"] = "missing"

    if selling is not None and admin is not None:
        statement["opex"] = selling + admin
        statement["opex_method"] = "period_components"
        statement["opex_lines"] = [
            {"key": "selling", "name": "销售及市场推广", "value": selling},
            {"key": "ga", "name": "一般及行政", "value": admin},
        ]

    lines = _segment_lines(revenue, kpis)
    segment = {
        "period": period,
        "end": end,
        "method": "period_kpis" if lines else "missing",
        "lines": lines,
    }
    return statement, segment


def build_tencent_histories_from_periods(folder: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """从 folder 下全部 FY*Q*.json 组装连续历史；缺文件的季不写入跨度外，跨度内缺数留空。"""
    by_period: dict[str, dict[str, Any]] = {}
    for path in sorted(folder.glob("FY*.json")):
        match = _PERIOD_FILE.match(path.stem)
        if not match:
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        statement, segment = _quarter_from_period_doc(path.stem, doc)
        by_period[path.stem] = {"statement": statement, "segment": segment}

    if not by_period:
        raise ValueError(f"{folder} 下没有可用的 FY*Q*.json")

    ordered = _span(min(by_period), max(by_period))
    quarters: list[dict[str, Any]] = []
    segment_quarters: list[dict[str, Any]] = []
    for period in ordered:
        packed = by_period.get(period)
        if packed is None:
            year = int(period[2:6])
            quarter = int(period[-1])
            quarters.append(
                {
                    "period": period,
                    "end": _end(year, quarter),
                    "revenue": None,
                    "revenue_method": "missing",
                    "cost_of_revenue": None,
                    "cost_method": "missing",
                    "geo": [],
                    "geo_method": "undisclosed",
                }
            )
            segment_quarters.append(
                {"period": period, "end": _end(year, quarter), "method": "missing", "lines": []}
            )
            continue
        quarters.append(packed["statement"])
        segment_quarters.append(packed["segment"])

    source = (
        "人民币金额来自 data/0700.HK 各季 Stage1 JSON。"
        "营业收入、毛利取 financials 原数；收入成本 = 收入 − 毛利。"
        "营业费用是销售及市场推广与一般及行政（kpis）合计，不含其他收益/亏损，不含营业成本。"
        "业务柱取自 kpis：增值服务、营销服务（含早期网络广告）、金融科技及企业服务；"
        "「其他」为收入减去已披露分项的非负残差。2017–2018 未单列金融科技时残差即当年「其他」桶。"
        "没有季度文件或字段缺失时留空。地区收入未披露，geo 留空，不补数。"
    )
    statement = {
        "ticker": "0700.HK",
        "source": source,
        "currency": "CNY",
        "unit": "人民币",
        "quarters": quarters,
    }
    segment = {
        "ticker": "0700.HK",
        "source": "业务分部取自各季 financials.kpis 收入分项；「其他」为残差。没有地区拆分。",
        "quarters": segment_quarters,
        "years": [],
    }
    return statement, segment


def write_tencent_histories(folder: Path) -> tuple[Path, Path]:
    statement, segment = build_tencent_histories_from_periods(folder)
    statement_path = folder / "statement_history.json"
    segment_path = folder / "segment_history.json"
    statement_path.write_text(json.dumps(statement, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    segment_path.write_text(json.dumps(segment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return statement_path, segment_path
