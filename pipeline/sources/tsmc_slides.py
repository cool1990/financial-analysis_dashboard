"""从已保存的台积电财报幻灯片白字生成 statement / segment 历史。

幻灯片正文藏在白色小字里，不看图片。某一季文字为空时，只用后一份幻灯片的
上季或去年同季列补数，不用指引列，也不把图像里读到的占比写进来。
台积电财年在 12 月结束，幻灯片上的 3Q26 就是 FY2026Q3。
"""

from __future__ import annotations

import calendar
import json
import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from html import unescape
from pathlib import Path
from typing import Any


def make_fiscal_period(fiscal_year: int, fiscal_quarter: int) -> str:
    return f"FY{fiscal_year}Q{fiscal_quarter}"


def parse_fiscal_period(label: str) -> tuple[int, int]:
    match = re.fullmatch(r"FY(\d{4})Q([1-4])", label)
    if match is None:
        raise ValueError(f"非法 fiscal_period: {label}")
    return int(match.group(1)), int(match.group(2))


def prior_fiscal_period(label: str) -> str:
    year, quarter = parse_fiscal_period(label)
    if quarter == 1:
        return make_fiscal_period(year - 1, 4)
    return make_fiscal_period(year, quarter - 1)


def next_fiscal_period(label: str) -> str:
    year, quarter = parse_fiscal_period(label)
    if quarter == 4:
        return make_fiscal_period(year + 1, 1)
    return make_fiscal_period(year, quarter + 1)


def yoy_fiscal_period(label: str) -> str:
    year, quarter = parse_fiscal_period(label)
    return make_fiscal_period(year - 1, quarter)


def fiscal_quarter_end(fiscal_year: int, fiscal_quarter: int, fiscal_year_end_month: int) -> date:
    """财季结束月的最后一天。只服务台积电幻灯片，不引用 compute 层。"""
    month = fiscal_year_end_month - (4 - fiscal_quarter) * 3
    year = fiscal_year
    while month <= 0:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    return date(year, month, calendar.monthrange(year, month)[1])

ORDINARY_SHARES_PER_ADR = 5
FY_END_MONTH = 12

# 损益表上带「Guidance」列的行。汇率的指引是单点假设，不是区间。
_GUIDANCE_ROWS = {"usd_revenue", "gross_margin", "operating_margin", "fx"}
_ROW_LABELS = (
    ("usd_revenue", "Net Revenue (US$ billions)"),
    ("ntd_revenue", "Net Revenue"),
    ("gross_margin", "Gross Margin"),
    ("opex_ntd", "Operating Expenses"),
    ("operating_margin", "Operating Margin"),
    ("non_op_ntd", "Non-Operating Items"),
    ("net_income_ntd", "Net Income Attributable to Shareholders of the Parent Company"),
    ("net_margin", "Net Profit Margin"),
    ("eps_ntd", "EPS (NT Dollar)"),
    ("roe", "ROE"),
    ("shipment", "Shipment"),
    ("fx", "Average Exchange Rate"),
)
_MONEY_FIELDS = ("usd_revenue", "ntd_revenue", "opex_ntd", "non_op_ntd", "net_income_ntd", "eps_ntd", "fx")
_RATIO_FIELDS = ("gross_margin", "operating_margin", "net_margin")
_FIELD_TOLERANCE = {
    "usd_revenue": Decimal("0.02"),
    "ntd_revenue": Decimal("0.05"),
    "opex_ntd": Decimal("0.05"),
    "non_op_ntd": Decimal("0.05"),
    "net_income_ntd": Decimal("0.05"),
    "eps_ntd": Decimal("0.02"),
    "fx": Decimal("0.01"),
    "gross_margin": Decimal("0.001"),
    "operating_margin": Decimal("0.001"),
    "net_margin": Decimal("0.001"),
}

_PLATFORMS = (
    ("hpc", "HPC", "高性能计算"),
    ("smartphone", "Smartphone", "智能手机"),
    ("iot", "IoT", "物联网"),
    ("auto", "Automotive", "汽车"),
    ("dce", "DCE", "数字消费电子"),
    ("other", "Others", "其他"),
)
_PLATFORM_BY_LABEL = {label: (key, name) for key, label, name in _PLATFORMS}

_TOKEN = re.compile(
    r"""
    (?P<range>\d[\d,]*\.?\d*%?[-–—]\d[\d,]*\.?\d*%?)
    |(?P<paren>\(\s*\d[\d,]*\.?\d*\s*\))
    |(?P<ppt>[+-]?\d[\d,]*\.?\d*\s*ppts?)
    |(?P<pct>[+-]?\d[\d,]*\.?\d*\s*%)
    |(?P<num>\d[\d,]*\.?\d*)
    """,
    re.VERBOSE,
)
_PLATFORM_PAIR = re.compile(r"\b(HPC|Smartphone|IoT|Automotive|DCE|Others)\s+(\d+(?:\.\d+)?)%")


def slide_text(html: str) -> str:
    """去掉标签，保留幻灯片白字。"""
    text = re.sub(r"(?is)<script\b.*?</script>", " ", html)
    text = re.sub(r"(?is)<style\b.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _decimal(raw: str) -> Decimal:
    cleaned = raw.replace(",", "").replace("%", "").replace("(", "").replace(")", "").strip()
    return Decimal(cleaned)


def _value_tokens(body: str) -> list[Decimal]:
    """一行里的水平值。区间只说明它是指引，变动百分比和 ppt 丢掉。"""
    values: list[Decimal] = []
    for match in _TOKEN.finditer(body):
        if match.group("range") or match.group("ppt"):
            if match.group("range"):
                values.append(Decimal("NaN"))  # 占位：指引区间，后面按位置跳过
            continue
        if match.group("pct"):
            token = match.group("pct").strip()
            if token[0] in "+-":
                continue
            values.append(_decimal(token) / Decimal(100))
            continue
        if match.group("paren"):
            values.append(abs(_decimal(match.group("paren"))))
            continue
        values.append(_decimal(match.group("num")))
    return values


def _split_columns(key: str, values: list[Decimal]) -> dict[str, Decimal] | None:
    numbers = [value for value in values if value.is_finite()]
    # 区间占位说明这一行有指引列：当前、指引、上季、去年同季。
    has_range = any(not value.is_finite() for value in values)
    if key in _GUIDANCE_ROWS and (has_range or len(numbers) >= 4):
        if len(numbers) < 3:
            return None
        # 有区间时，区间已经被丢掉，剩下的是当前、上季、去年同季。
        if has_range and len(numbers) >= 3:
            current, prior, year_ago = numbers[0], numbers[1], numbers[2]
        elif len(numbers) >= 4:
            current, prior, year_ago = numbers[0], numbers[2], numbers[3]
        else:
            current, prior, year_ago = numbers[0], numbers[1], numbers[2]
    elif len(numbers) >= 3:
        current, prior, year_ago = numbers[0], numbers[1], numbers[2]
    else:
        return None
    return {"current": current, "prior": prior, "yoy": year_ago}


def _income_block(text: str) -> str:
    start = text.find("Selected Items from Statements of Comprehensive Income")
    if start < 0:
        start = text.find("Net Revenue (US$ billions)")
    if start < 0:
        return ""
    end = len(text)
    for marker in ("Revenue by Technology", "Revenue by Platform", "Balance Sheets", "Diluted weighted"):
        found = text.find(marker, start + 80)
        if found > start:
            end = min(end, found)
    return text[start:end]


def _row_bodies(block: str) -> dict[str, str]:
    cursor = 0
    found: list[tuple[str, int, int]] = []
    for key, label in _ROW_LABELS:
        index = block.find(label, cursor)
        if index < 0:
            continue
        found.append((key, index, index + len(label)))
        cursor = index + len(label)
    bodies: dict[str, str] = {}
    for position, (key, _start, end) in enumerate(found):
        next_start = found[position + 1][1] if position + 1 < len(found) else len(block)
        bodies[key] = block[end:next_start]
    return bodies


def parse_income_slide(text: str, slide_period: str) -> dict[str, dict[str, Decimal]] | None:
    """一张损益表幻灯片：当前、上季、去年同季三列。读不到美元营收则整张跳过。"""
    block = _income_block(text)
    if "Net Revenue (US$ billions)" not in block:
        return None
    quarter = re.search(r"\b([1-4])Q(\d{2})\b", block)
    if quarter is None:
        return None
    period = make_fiscal_period(2000 + int(quarter.group(2)), int(quarter.group(1)))
    if period != slide_period:
        # 文件名季度和幻灯片标题不一致时，以幻灯片标题为准，避免把错文件写进这一季。
        slide_period = period
    columns: dict[str, dict[str, Decimal]] = {"current": {}, "prior": {}, "yoy": {}}
    for key, body in _row_bodies(block).items():
        if key not in _MONEY_FIELDS and key not in _RATIO_FIELDS:
            continue
        split = _split_columns(key, _value_tokens(body))
        if not split or "usd_revenue" == key and split["current"] <= 0:
            if key == "usd_revenue":
                return None
            continue
        for role, value in split.items():
            columns[role][key] = value
    if "usd_revenue" not in columns["current"]:
        return None
    columns["period"] = slide_period  # type: ignore[assignment]
    return columns


def _platform_chunk(text: str, quarter_label: str) -> str:
    """只取「1Q25 Revenue by Platform」这一页，停在全年页或下一页标题之前。"""
    match = re.search(rf"\b{re.escape(quarter_label)}\s+Revenue by Platform\b", text)
    if match is None:
        return ""
    rest = text[match.end() :]
    stop = re.search(
        r"(?:\b[1-4]Q\d{2}\s+Revenue by\b)|(?:\b20\d{2}\s+Revenue by Platform\b)|Balance Sheets|Revenue by Technology",
        rest,
    )
    return rest[: stop.start()] if stop else rest[:800]


def parse_platform(text: str, period: str) -> dict[str, Decimal] | None:
    """平台占比。合计不在 99–101 之间就整季留空，不用全年页，也不猜缺失项。"""
    year, quarter = parse_fiscal_period(period)
    chunk = _platform_chunk(text, f"{quarter}Q{year % 100:02d}")
    if not chunk:
        return None
    found: dict[str, Decimal] = {}
    for label, raw in _PLATFORM_PAIR.findall(chunk):
        key = _PLATFORM_BY_LABEL[label][0]
        value = Decimal(raw)
        if key in found and found[key] != value:
            return None
        found[key] = value
    if len(found) < 2:
        return None
    total = sum(found.values(), Decimal(0))
    if total < Decimal(99) or total > Decimal(101):
        return None
    return found


def _slide_period_from_name(path: Path) -> str | None:
    match = re.fullmatch(r"FY(\d{4})Q([1-4])", path.parent.name)
    if match is None:
        return None
    return make_fiscal_period(int(match.group(1)), int(match.group(2)))


def _roles_for(period: str) -> dict[str, str]:
    return {
        "current": period,
        "prior": prior_fiscal_period(period),
        "yoy": yoy_fiscal_period(period),
    }


def _prefer(role: str) -> int:
    return {"current": 0, "prior": 1, "yoy": 2}[role]


def _same(field: str, left: Decimal, right: Decimal) -> bool:
    return abs(left - right) <= _FIELD_TOLERANCE[field]


def collect_slides(paths: list[Path]) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
    """按季度合并多张幻灯片。同一格以当季列优先，上季列其次，去年同季列最后。"""
    cells: dict[str, dict[str, Any]] = {}
    used: list[str] = []
    skipped: list[str] = []
    for path in sorted(paths):
        period = _slide_period_from_name(path)
        if period is None:
            continue
        text = slide_text(path.read_text(encoding="utf-8", errors="replace"))
        parsed = parse_income_slide(text, period)
        if parsed is None:
            skipped.append(period)
            continue
        slide_period = str(parsed["period"])
        used.append(slide_period)
        roles = _roles_for(slide_period)
        platform = parse_platform(text, slide_period)
        for role, target in roles.items():
            fields: dict[str, Decimal] = parsed[role]  # type: ignore[assignment]
            if "usd_revenue" not in fields:
                continue
            current = cells.get(target)
            incoming = {
                "fields": fields,
                "role": role,
                "slide": slide_period,
                "platform": platform if role == "current" else None,
            }
            if current is None:
                cells[target] = incoming
                continue
            for field, value in fields.items():
                previous = current["fields"].get(field)
                if previous is not None and not _same(field, previous, value):
                    raise ValueError(
                        f"{target} {field} 在 {current['slide']}:{current['role']} 为 {previous}，"
                        f"在 {slide_period}:{role} 为 {value}"
                    )
            if _prefer(role) < _prefer(str(current["role"])):
                merged = dict(current["fields"])
                merged.update(fields)
                incoming["fields"] = merged
                if incoming["platform"] is None:
                    incoming["platform"] = current.get("platform")
                cells[target] = incoming
            else:
                current["fields"].update({key: value for key, value in fields.items() if key not in current["fields"]})
                if role == "current" and platform is not None:
                    current["platform"] = platform
    return cells, used, skipped


def _usd(billions: Decimal) -> int:
    return int((billions * Decimal(1_000_000_000)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _ntd_to_usd(billions: Decimal, fx: Decimal) -> int:
    return int((billions * Decimal(1_000_000_000) / fx).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _ratio_amount(revenue: int, ratio: Decimal) -> int:
    return int((Decimal(revenue) * ratio).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _json_number(value: Decimal, places: str) -> float:
    quantized = value.quantize(Decimal(places), rounding=ROUND_HALF_UP)
    return float(quantized)


_PRESS_ADR = re.compile(
    r"NT\$\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)\s*\(\s*US\$\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)\s+per ADR unit\s*\)",
    re.IGNORECASE,
)


def parse_press_adr_eps(html: str) -> tuple[float, float] | None:
    """新闻稿里同时写明的普通股新台币和 US$ per ADR。不按汇率换算。"""
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"\s+", " ", unescape(text))
    match = _PRESS_ADR.search(text)
    if match is None:
        return None
    ntd = float(match.group(1).replace(",", ""))
    usd = float(match.group(2).replace(",", ""))
    return ntd, usd


def _press_adr_by_period(paths: list[Path]) -> dict[str, tuple[float, float]]:
    found: dict[str, tuple[float, float]] = {}
    roots = {path.parent.parent for path in paths}
    for root in roots:
        if not root.is_dir():
            continue
        for press in sorted(root.glob("FY*/press_release.html")):
            period = press.parent.name
            if re.fullmatch(r"FY\d{4}Q[1-4]", period) is None:
                continue
            parsed = parse_press_adr_eps(press.read_text(encoding="utf-8", errors="ignore"))
            if parsed is not None:
                found[period] = parsed
    return found


def _quarter_record(period: str, cell: dict[str, Any] | None) -> dict[str, Any]:
    year, quarter = parse_fiscal_period(period)
    end = fiscal_quarter_end(year, quarter, FY_END_MONTH).isoformat()
    record: dict[str, Any] = {
        "period": period,
        "end": end,
        "revenue": None,
        "revenue_method": "missing",
        "cost_of_revenue": None,
        "cost_method": "missing",
        "geo": [],
        "geo_method": "undisclosed",
    }
    if cell is None:
        return record
    fields: dict[str, Decimal] = cell["fields"]
    revenue = _usd(fields["usd_revenue"])
    record.update(
        {
            "revenue": revenue,
            "revenue_method": "slide",
            "observed_on": {"slide": cell["slide"], "column": cell["role"]},
        }
    )
    gross_margin = fields.get("gross_margin")
    if gross_margin is not None:
        gross_profit = _ratio_amount(revenue, gross_margin)
        record["gross_margin"] = _json_number(gross_margin, "0.0001")
        record["gross_profit"] = gross_profit
        record["cost_of_revenue"] = revenue - gross_profit
        record["cost_method"] = "derived"
    else:
        record["cost_method"] = "missing"
    fx = fields.get("fx")
    if fx is not None:
        record["fx_usd_ntd"] = _json_number(fx, "0.01")
    opex = fields.get("opex_ntd")
    if opex is not None and fx is not None and fx > 0:
        record["opex"] = _ntd_to_usd(opex, fx)
        record["opex_method"] = "ntd_over_fx"
    operating_margin = fields.get("operating_margin")
    if operating_margin is not None:
        record["operating_margin"] = _json_number(operating_margin, "0.0001")
        record["operating_income"] = _ratio_amount(revenue, operating_margin)
    net_income = fields.get("net_income_ntd")
    if net_income is not None and fx is not None and fx > 0:
        record["net_income"] = _ntd_to_usd(net_income, fx)
    eps = fields.get("eps_ntd")
    if eps is not None:
        record["eps_ordinary_ntd"] = _json_number(eps, "0.01")
    return record


def _platform_lines(revenue: int | None, shares: dict[str, Decimal] | None) -> list[dict[str, Any]]:
    if not revenue or not shares:
        return []
    lines = []
    for key, _label, name in _PLATFORMS:
        if key not in shares:
            continue
        value = int((Decimal(revenue) * shares[key] / Decimal(100)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        lines.append({"key": key, "name": name, "value": value, "depth": 0, "stack": True, "share_pct": _json_number(shares[key], "0.1")})
    return lines


def _span(start: str, end: str) -> list[str]:
    labels = []
    cursor = start
    while True:
        labels.append(cursor)
        if cursor == end:
            return labels
        cursor = next_fiscal_period(cursor)


def build_tsmc_histories(paths: list[Path], *, cik: str = "0001046179") -> tuple[dict[str, Any], dict[str, Any]]:
    cells, used, skipped = collect_slides(paths)
    if not cells:
        raise ValueError("没有从幻灯片读到任何一季美元营收")
    periods = _span(min(cells), max(cells))
    quarters = [_quarter_record(period, cells.get(period)) for period in periods]
    press_adr = _press_adr_by_period(paths)
    for record in quarters:
        disclosed = press_adr.get(record["period"])
        if disclosed is None:
            continue
        ntd, usd = disclosed
        record["eps_ordinary_ntd"] = ntd
        record["eps_adr_usd"] = usd
        record["eps_adr_method"] = "press_release"
    source = (
        "美元营收取自财报幻灯片「Net Revenue (US$ billions)」当季列；该季白字为空时，用后一份幻灯片的上季或去年同季列，不取指引列。"
        "台币营业费用和净利润除以同一列的 Average Exchange Rate--USD/NTD 换成美元。"
        "毛利 = 美元营收 × 毛利率，营业利润 = 美元营收 × 营业利润率，营业成本 = 美元营收 − 毛利。"
        f"1 ADR = {ORDINARY_SHARES_PER_ADR} 股普通股。普通股 EPS 为新台币。"
        "ADR 美元只取新闻稿写明的 US$ per ADR，不按当季平均汇率把新台币换算成美元。"
        "平台占比只取「nQyy Revenue by Platform」文字且合计约 100% 的季度，金额 = 占比 × 当季美元营收；"
        "全年页，以及只有图、文字里没有百分比的季度，不补数。"
        "地区收入在已保存文字里没有占比，geo 留空。不使用图像识别，不把新台币当成美元。"
    )
    statement = {
        "ticker": "TSM",
        "cik": cik,
        "source": source,
        "adr": {
            "ordinary_shares_per_adr": ORDINARY_SHARES_PER_ADR,
            "eps_method": "press_release_us_per_adr",
            "listing": "NYSE ADR",
        },
        "parsed_slides": used,
        "skipped_slides": skipped,
        "quarters": quarters,
    }
    segment_quarters = []
    for record, period in zip(quarters, periods):
        cell = cells.get(period)
        lines = _platform_lines(record.get("revenue"), None if cell is None else cell.get("platform"))
        segment_quarters.append(
            {
                "period": period,
                "end": record["end"],
                "method": "slide" if lines else "missing",
                "lines": lines,
            }
        )
    segment = {
        "ticker": "TSM",
        "cik": cik,
        "source": (
            "业务平台只取幻灯片白字里的 Revenue by Platform，且各项相加约 100%。"
            "金额 = 披露占比 × 当季美元营收。文字没有百分比的季度 lines 为空，柱上留空。"
            "2024 全年平台页不写入单季。技术节点不是业务单元，也不写入。地区未披露。"
        ),
        "quarters": segment_quarters,
        "years": [],
    }
    return statement, segment


def write_tsmc_histories(folder: Path, *, cik: str = "0001046179") -> tuple[Path, Path]:
    paths = sorted((folder / "raw").glob("FY*/presentation.html"))
    statement, segment = build_tsmc_histories(paths, cik=cik)
    statement_path = folder / "statement_history.json"
    segment_path = folder / "segment_history.json"
    statement_path.write_text(json.dumps(statement, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    segment_path.write_text(json.dumps(segment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return statement_path, segment_path
