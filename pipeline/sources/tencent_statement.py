"""从已保存的腾讯业绩新闻稿生成 statement / segment 历史。

只读简明综合收益表里的人民币百万元。销售及市场推广、一般及行政相加为营业费用。
地区收入这份表没有，geo 留空。不补未出现的金额。
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
_LABELS = {
    "收入": "revenue",
    "增值服务": "vas",
    "营销服务": "marketing",
    "金融科技及企业服务": "fintech",
    "其他": "other",
    "收入成本": "cost",
    "毛利": "gross_profit",
    "销售及市场推广开支": "selling",
    "一般及行政开支": "admin",
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


def _rows(text: str) -> dict[str, list[int]]:
    parsed: dict[str, list[int]] = {}
    for raw in _statement_text(text).splitlines():
        line = raw.strip()
        if not line:
            continue
        label = line.split()[0]
        key = _LABELS.get(label)
        if key is None or key in parsed:
            continue
        amounts = _AMOUNT.findall(line[len(label):])
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


def write_tencent_histories(folder: Path) -> tuple[Path, Path]:
    text = (folder / "raw" / "FY2026Q1" / "press_release.txt").read_text(encoding="utf-8")
    statement, segment = parse_tencent_statement(text)
    statement_path = folder / "statement_history.json"
    segment_path = folder / "segment_history.json"
    statement_path.write_text(json.dumps(statement, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    segment_path.write_text(json.dumps(segment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return statement_path, segment_path
