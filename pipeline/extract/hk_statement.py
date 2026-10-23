"""从腾讯业绩公告里定位三个月单季表。没有单季列时只返回累计，不把半年写成一季。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from pipeline.extract.hk_text import first_period_end, prepare_statement_text, quarter_end_from_label

_COMMA_NUM = re.compile(r"\(?\d{1,3}(?:,\d{3})+(?:\.\d+)?\)?")
_Q_LABEL = re.compile(r"[1-4]Q\d{4}")
_REVENUE_LINE = re.compile(r"^(?:收入(?!成本)|revenues?)\b", re.I)
_STOP = re.compile(
    r"综合全面收益表|其他财务资料|财务状况表|consolidated statement of comprehensive|statement of financial position",
    re.I,
)

_CN_Q = {"一": 1, "二": 2, "三": 3, "四": 4}


class HkImportError(ValueError):
    """公告口径不能写入所请求的单季。"""


class HkCumulativeOnly(HkImportError):
    """公告只有累计，没有三个月单季列。cumulative 供残差使用，调用方仍可选择留空。"""

    def __init__(self, message: str, cumulative: dict | None = None):
        super().__init__(message)
        self.cumulative = cumulative or {}


@dataclass
class _Candidate:
    header: str
    body: str
    mode: str
    period_end: date
    roles: dict[str, int]
    rows: dict[str, dict] = field(default_factory=dict)
    score: int = 0


_ROW_SPECS: tuple[tuple[str, str], ...] = (
    ("non_ifrs_operating_profit", r"^(?:非国际财务报告准则经营盈利|非通用会计准则经营盈利|non-ifrs operating profit|non-gaap operating profit)\b"),
    ("operating_income", r"^(?:经营盈利(?!率)|operating profit(?!\s*margin))\b"),
    ("revenue", r"^(?:收入(?!成本)|revenues?)\b"),
    ("cost_of_revenue", r"^(?:收入成本|cost of revenues?)\b"),
    ("gross_profit", r"^(?:毛利(?!率)|gross profit(?!\s*margin))\b"),
    ("vas_revenue", r"^(?:增值服务|value-added services)\b"),
    ("advertising_revenue", r"^(?:营销服务|网络广告|在线广告|online advertising)\b"),
    ("fintech_biz_revenue", r"^(?:金融科技及企业服务|fintech and business services)\b"),
    ("selling_expense", r"^(?:销售及市场推广开支|selling and marketing expenses)\b"),
    ("ga_expense", r"^(?:一般及行政开支|general and administrative expenses)\b"),
    ("rd_expense", r"^(?:研发开支|研究及开发|research and development)\b"),
    ("net_income", r"^(?:期内盈利|profit for the period)\b"),
    ("capex", r"^(?:资本开支|capital expenditures)\b"),
)


def infer_hk_filing_period(filename: str) -> str | None:
    """腾讯公告文件名 → FY{Y}Q{n}。一季报/三季报对应该季；中报→Q2；年报→Q4。"""
    stem = filename.rsplit("/", 1)[-1]
    stem = stem.rsplit(".", 1)[0]
    fy = re.search(r"FY(20\d{2})Q([1-4])", stem, re.I)
    if fy:
        return f"FY{fy.group(1)}Q{fy.group(2)}"
    labeled = re.search(r"([1-4])Q(20\d{2})", stem, re.I)
    if labeled:
        return f"FY{labeled.group(2)}Q{labeled.group(1)}"
    year_q = re.search(r"(20\d{2})Q([1-4])", stem, re.I)
    if year_q:
        return f"FY{year_q.group(1)}Q{year_q.group(2)}"
    year = re.search(r"(20\d{2})", stem)
    if not year:
        return None
    y = year.group(1)
    quarter = re.search(r"([一二三四])季", stem)
    if quarter:
        return f"FY{y}Q{_CN_Q[quarter.group(1)]}"
    if re.search(r"中报|中期", stem):
        return f"FY{y}Q2"
    if re.search(r"年报|年度报告|全年业绩", stem):
        return f"FY{y}Q4"
    return None


def _comma_numbers(line: str) -> list[str]:
    return _COMMA_NUM.findall(line)


def _decimals(line: str) -> list[str]:
    return re.findall(r"\d+\.\d+", line)


def _roles_from_labels(labels: list[str]) -> dict[str, int]:
    roles = {"current": 0}
    if not labels:
        return roles
    matched = re.match(r"([1-4])Q(\d{4})", labels[0])
    if not matched:
        return roles
    quarter, year = int(matched.group(1)), int(matched.group(2))
    yoy = f"{quarter}Q{year - 1}"
    prior_q, prior_y = (4, year - 1) if quarter == 1 else (quarter - 1, year)
    prior = f"{prior_q}Q{prior_y}"
    for index, label in enumerate(labels):
        if label == yoy:
            roles["yoy"] = index
        elif label == prior:
            roles["qoq"] = index
    return roles


def _quarter_ends(dates: list[date]) -> list[date]:
    return [item for item in dates if item.month in {3, 6, 9, 12} and item.day >= 28]


def _header_mode(header: str, labels: list[str]) -> str:
    if "九个月" in header or "nine month" in header.lower():
        return "quarter_plus_ytd"
    if labels:
        return "qlabels"
    lower = header.lower()
    yoy = "同比" in header or "year-on-year" in lower
    qoq = "环比" in header or "quarter-on-quarter" in lower
    # 刊发日不是列头。列头上的季末日才决定这是同比还是环比。
    ends = _quarter_ends(_dates_in(header))
    if yoy and qoq and len(ends) >= 3:
        return "highlight"
    if len(ends) >= 2:
        first, second = ends[0], ends[1]
        if second.year == first.year - 1 and second.month == first.month:
            return "yoy"
        gap = (first - second).days
        if 80 <= gap <= 100:
            return "qoq"
    if yoy and qoq:
        return "highlight"
    if qoq and not yoy:
        return "qoq"
    return "yoy"


def _cn_year(token: str) -> int:
    if token.isdigit():
        return int(token)
    from pipeline.extract.hk_text import _CN_DIGIT

    return int("".join(str(_CN_DIGIT[ch]) for ch in token))


def _cn_piece(token: str) -> int:
    from pipeline.extract.hk_text import cn_int

    return cn_int(token)


_YEAR_ONLY = re.compile(r"^([零〇一二三四五六七八九]{4}|\d{4})\s*年$")
_MD_ONLY = re.compile(
    r"^(十二|十一|十|[一二三四五六七八九]|\d{1,2})\s*月\s*([一二三四五六七八九十]{1,3}|\d{1,2})\s*日$"
)
_FULL_CN = re.compile(
    r"([零〇一二三四五六七八九]{4}|\d{4})\s*年\s*"
    r"(十二|十一|十|[一二三四五六七八九]|\d{1,2})\s*月\s*"
    r"([一二三四五六七八九十]{1,3}|\d{1,2})\s*日"
)
_EN_DATE = re.compile(r"\d{1,2}\s+[A-Za-z]+\s+\d{4}")
_HEADER_SEPARATOR = re.compile(
    r"^(?:同比变动|环比变动|同比|环比|year-on-year|quarter-on-quarter|change|year-|on-year|quarter-|on-quarter)$",
    re.I,
)
_HEADER_INTRO = re.compile(
    r"^(?:未经审核|经审核|unaudited|audited|截至下列日期止三个月|three months ended.*)$",
    re.I,
)
_NEXT_TABLE = re.compile(
    r"止九个月|止六个月|止年度|止十二个月|nine months ended|six months ended|for the year ended|twelve months ended",
    re.I,
)


def _line_date_atoms(line: str) -> list[tuple] | None:
    """一行列头：单独的年、单独的月日，或一行里的完整日期。"""
    stripped = line.strip()
    year_only = _YEAR_ONLY.match(stripped)
    if year_only:
        return [("Y", _cn_year(year_only.group(1)))]
    month_day = _MD_ONLY.match(stripped)
    if month_day:
        return [("MD", _cn_piece(month_day.group(1)), _cn_piece(month_day.group(2)))]
    fulls: list[tuple] = []
    for matched in _FULL_CN.finditer(stripped):
        try:
            fulls.append((
                "D",
                date(_cn_year(matched.group(1)), _cn_piece(matched.group(2)), _cn_piece(matched.group(3))),
            ))
        except ValueError:
            continue
    if fulls and not _FULL_CN.sub("", stripped).strip(" \t,，、"):
        return fulls
    english: list[tuple] = []
    for matched in _EN_DATE.finditer(stripped):
        parsed = first_period_end(matched.group(0))
        if parsed:
            english.append(("D", parsed))
    if not english:
        return None
    leftover = _EN_DATE.sub("", stripped).strip(" \t,，")
    if leftover == "" or leftover.lower() in {"three months ended", "unaudited"} or len(stripped) < 80:
        return english
    return None


def _pair_header_atoms(flat: list[tuple]) -> list[date]:
    """「年、年、月日、月日」或「年、月日、年、月日」都按从左到右配对。"""
    found: list[date] = []
    index = 0
    while index < len(flat):
        kind = flat[index][0]
        if kind == "D":
            found.append(flat[index][1])
            index += 1
            continue
        if kind == "Y" and index + 1 < len(flat) and flat[index + 1][0] == "MD":
            year, month_day = flat[index], flat[index + 1]
            try:
                found.append(date(year[1], month_day[1], month_day[2]))
            except ValueError:
                pass
            index += 2
            continue
        if kind == "Y":
            years: list[int] = []
            while index < len(flat) and flat[index][0] == "Y":
                years.append(flat[index][1])
                index += 1
            month_days: list[tuple] = []
            while index < len(flat) and flat[index][0] == "MD":
                month_days.append(flat[index])
                index += 1
            if len(years) == len(month_days):
                for year, month_day in zip(years, month_days):
                    try:
                        found.append(date(year, month_day[1], month_day[2]))
                    except ValueError:
                        pass
            continue
        index += 1
    return found


def _column_header_dates(header: str) -> list[date]:
    """只读紧贴表格的列头，不让标题里的「二零一六年第四季」吃掉本年。"""
    stitched = re.sub(
        r"(\d{1,2})\s*\n\s*([A-Za-z]+)\s+(\d{4})",
        r"\1 \2 \3",
        header,
    )
    lines: list[str] = []
    for raw in stitched.splitlines():
        stripped = raw.strip()
        if not stripped or re.fullmatch(r"[（(].+[)）]", stripped):
            continue
        lines.append(stripped)
    groups: list[list[tuple]] = []
    for line in reversed(lines):
        atoms = _line_date_atoms(line)
        if atoms:
            groups.append(atoms)
            continue
        if _HEADER_SEPARATOR.match(line):
            continue
        if _HEADER_INTRO.match(line):
            if groups:
                break
            continue
        if groups:
            break
    flat: list[tuple] = []
    for group in reversed(groups):
        flat.extend(group)
    return _pair_header_atoms(flat)


def _loose_dates(chunk: str) -> list[date]:
    """列头拆不开时，再从正文里的完整日期兜底。"""
    years = [
        _cn_year(token)
        for token in re.findall(r"([零〇一二三四五六七八九]{4}|\d{4})\s*年", chunk)
    ]
    parts = re.findall(
        r"(十二|十一|十|[一二三四五六七八九]|\d{1,2})\s*月\s*([一二三四五六七八九十]{1,3}|\d{1,2})\s*日",
        chunk,
    )
    found: list[date] = []
    if parts and len(years) >= len(parts):
        # 标题在前、列头在后时，用最后一组年，避免「与二零一六年」抢列。
        for year, (month, day) in zip(years[-len(parts):], parts):
            try:
                found.append(date(year, _cn_piece(month), _cn_piece(day)))
            except ValueError:
                continue
    elif parts and len(years) == 1:
        for month, day in parts:
            try:
                found.append(date(years[0], _cn_piece(month), _cn_piece(day)))
            except ValueError:
                continue
    if not found:
        parsed = first_period_end(chunk)
        if parsed:
            found.append(parsed)
    for matched in re.finditer(r"\d{1,2}\s+[A-Za-z]+\s+\d{4}", chunk):
        parsed = first_period_end(matched.group(0))
        if parsed and parsed not in found:
            found.append(parsed)
    return found


def _dates_in(chunk: str) -> list[date]:
    column = _column_header_dates(chunk)
    if column:
        return column
    return _loose_dates(chunk)


def _period_end_of(header: str, labels: list[str]) -> date | None:
    if labels:
        labeled = quarter_end_from_label(labels[0])
        if labeled:
            return labeled
    ends = _quarter_ends(_dates_in(header))
    if ends:
        return ends[0]
    dates = _dates_in(header)
    if dates:
        return dates[0]
    return first_period_end(header)


def _fiscal_from_end(end: date, fiscal_year_end_month: int) -> str:
    if fiscal_year_end_month != 12:
        raise HkImportError("港股手工导入目前只核对财年末月为 12 的公司")
    quarter = {3: 1, 6: 2, 9: 3, 12: 4}.get(end.month)
    if quarter is None or end.day < 28:
        raise HkImportError(f"报告期末 {end.isoformat()} 不是单季月末，拒绝猜测财季")
    return f"FY{end.year}Q{quarter}"


def _is_quarter_header(header: str) -> bool:
    lower = header.lower()
    three = any(token in header for token in ("三个月", "第一季", "第二季", "第三季", "第四季")) or "three month" in lower or "quarter" in lower
    six_only = (("六个月" in header or "six month" in lower or "中期业绩" in header) and not three)
    year_only = (("止年度" in header or "for the year" in lower or "十二个月" in header) and not three)
    if six_only or year_only:
        return False
    if "九个月" in header and "三个月" in header:
        return True
    return three or bool(_Q_LABEL.search(header))


def _node(raw: str | None, quote: str, *, unit: str, yoy: str | None, qoq: str | None) -> dict | None:
    if not raw:
        return None
    cleaned = raw.strip().strip("()")
    if not cleaned or cleaned in {"-", "－", "—"}:
        return None
    return {
        "raw": cleaned,
        "unit": unit,
        "currency": "CNY",
        "period_type": "3m",
        "months": 3,
        "source_quote": quote[:200],
        "year_ago_raw": None if yoy is None else yoy.strip().strip("()"),
        "prior_quarter_raw": None if qoq is None else qoq.strip().strip("()"),
    }


def _pick(values: list[str], index: int | None) -> str | None:
    if index is None or index >= len(values):
        return None
    return values[index]


def _line_values(line: str, *, eps: bool) -> list[str]:
    return _decimals(line) if eps else _comma_numbers(line)


def _parse_rows(body: str, roles: dict[str, int]) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    lines = body.splitlines()
    for key, pattern in _ROW_SPECS:
        compiled = re.compile(pattern, re.I)
        for line in lines:
            stripped = line.strip()
            if not stripped or not compiled.search(stripped):
                continue
            numbers = _line_values(stripped, eps=False)
            if not numbers:
                continue
            node = _node(
                _pick(numbers, roles.get("current", 0)),
                stripped,
                unit="millions",
                yoy=_pick(numbers, roles.get("yoy")),
                qoq=_pick(numbers, roles.get("qoq")),
            )
            if node:
                rows[key] = node
            break
    rows.update(_eps_rows(lines, roles))
    return rows


def _looks_nongaap(lines: list[str], index: int) -> bool:
    """摊薄行是否落在非通用每股盈利标题下。标题上方带数字的非通用净利润行不算。"""
    heading = None
    for back in range(index, max(-1, index - 6), -1):
        stripped = lines[back].strip()
        if re.match(r"^[-－—–]", stripped):
            continue
        if re.search(r"每股盈利|earnings per share|eps\b", stripped, re.I):
            heading = back
            break
    if heading is None:
        window_lines = lines[max(0, index - 2):index + 1]
    else:
        start = heading
        previous = lines[heading - 1] if heading else ""
        if heading > 0 and not _comma_numbers(previous):
            start = heading - 1
        window_lines = lines[start:index + 1]
    window = "\n".join(window_lines)
    return bool(re.search(r"非国际财务报告准则|非通用会计准则|non-ifrs|non-gaap", window, re.I))


def _eps_rows(lines: list[str], roles: dict[str, int]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not re.match(r"^[-－—–]\s*(?:摊薄|diluted)\b", stripped, re.I):
            continue
        numbers = _decimals(stripped)
        if not numbers:
            continue
        node = _node(
            _pick(numbers, roles.get("current", 0)),
            stripped,
            unit="yuan",
            yoy=_pick(numbers, roles.get("yoy")),
            qoq=_pick(numbers, roles.get("qoq")),
        )
        if not node:
            continue
        if _looks_nongaap(lines, index):
            out.setdefault("eps_nongaap", node)
        elif "eps_gaap" not in out:
            out["eps_gaap"] = node
    return out


def _roles_for(mode: str, labels: list[str]) -> dict[str, int]:
    if mode == "qlabels":
        return _roles_from_labels(labels)
    if mode == "highlight":
        return {"current": 0, "yoy": 1, "qoq": 2}
    if mode == "qoq":
        return {"current": 0, "qoq": 1}
    return {"current": 0, "yoy": 1}


def _cross_foot_ok(rows: dict[str, dict]) -> bool:
    revenue = rows.get("revenue")
    cost = rows.get("cost_of_revenue")
    gross = rows.get("gross_profit")
    if not revenue or not cost or not gross:
        return True
    def _num(raw: str) -> float:
        return float(raw.replace(",", ""))
    gap = abs((_num(revenue["raw"]) - _num(cost["raw"])) - _num(gross["raw"]))
    return gap <= 2


def _score(header: str, mode: str, rows: dict[str, dict]) -> int:
    score = 0
    if "cost_of_revenue" in rows:
        score += 5
    if "eps_gaap" in rows:
        score += 2
    score += {"qlabels": 4, "quarter_plus_ytd": 4, "yoy": 3, "highlight": 1, "qoq": 2}.get(mode, 0)
    if "综合收益表" in header or "income statement" in header.lower():
        score += 2
    if "比较" in header or "compared" in header.lower():
        score += 3
    return score


def _candidates(text: str) -> list[_Candidate]:
    lines = text.splitlines()
    found: list[_Candidate] = []
    for index, line in enumerate(lines):
        if not _REVENUE_LINE.match(line.strip()):
            continue
        if not _comma_numbers(line):
            continue
        header_lines = lines[max(0, index - 14):index]
        end = index + 1
        limit = min(len(lines), index + 48)
        for cursor in range(index + 1, limit):
            if _STOP.search(lines[cursor]) or _NEXT_TABLE.search(lines[cursor]):
                break
            end = cursor + 1
        header = "\n".join(header_lines)
        body = "\n".join(lines[index:end])
        if not _is_quarter_header(header + "\n" + body[:200]):
            continue
        labels: list[str] = []
        # 同比、环比列头经常分成两行：1Q2024 1Q2023 / 1Q2024 4Q2023。两行都要留下。
        for probe in header_lines[-6:] + lines[index:index + 2]:
            got = _Q_LABEL.findall(probe)
            if len(got) >= 2:
                labels.extend(got)
        try:
            period_end = _period_end_of(header, labels)
        except HkImportError:
            continue
        if period_end is None:
            continue
        mode = _header_mode(header, labels)
        roles = _roles_for(mode, labels)
        rows = _parse_rows(body, roles)
        if "revenue" not in rows or "gross_profit" not in rows or "net_income" not in rows:
            continue
        if not _cross_foot_ok(rows):
            continue
        found.append(_Candidate(
            header=header,
            body=body,
            mode=mode,
            period_end=period_end,
            roles=roles,
            rows=rows,
            score=_score(header, mode, rows),
        ))
    return found


def _merge_missing(primary: dict[str, dict], extra: dict[str, dict], *, qoq_only: bool) -> None:
    for key, node in extra.items():
        if key not in primary:
            if qoq_only:
                continue
            primary[key] = dict(node)
            continue
        current = primary[key]
        if not qoq_only and node.get("year_ago_raw") and not current.get("year_ago_raw"):
            current["year_ago_raw"] = node["year_ago_raw"]
        prior = node.get("prior_quarter_raw")
        if prior and not current.get("prior_quarter_raw") and prior != current.get("raw"):
            current["prior_quarter_raw"] = prior


def _bridge_blocks(text: str) -> list[tuple[date, str]]:
    """调节表日期行。季报常写成「截至 2017 年 9 月 30 日三个月」，调整项列名在日期行上方。"""
    pattern = re.compile(
        r"截至\s*.{0,48}?三个月|unaudited three months ended.{0,80}|three months ended\s+\d{1,2}\s+[A-Za-z]+\s+\d{4}",
        re.I,
    )
    matches = list(pattern.finditer(text))
    blocks: list[tuple[date, str]] = []
    marker = re.compile(r"股份酬金|share-based|摊销|amortisation|amortization|减值拨备|impairment", re.I)
    for index, matched in enumerate(matches):
        next_at = matches[index + 1].start() if index + 1 < len(matches) else matched.end() + 1800
        forward = text[matched.start():min(next_at, matched.end() + 2200)]
        preceding = text[max(0, matched.start() - 800):matched.start()]
        if not marker.search(preceding + forward):
            continue
        end = first_period_end(matched.group(0)) or first_period_end(forward[:240])
        if end:
            blocks.append((end, forward))
    return blocks


def _bridge_amount(window: str, label: str) -> str | None:
    compiled = re.compile(label, re.I)
    for line in window.splitlines():
        stripped = line.strip()
        if not compiled.search(stripped):
            continue
        numbers = _comma_numbers(stripped)
        if len(numbers) >= 3:
            return numbers[-1].strip("()")
    return None


def _gaap_raw(rows: dict[str, dict], key: str) -> str | None:
    node = rows.get(key)
    return None if not node else node.get("raw")


def _bridge_operating(window: str, gaap_op: str | None) -> str | None:
    """调节表经营盈利行：第一列是已报告经营盈利，中间是调整项，最后一列才是非通用口径。

    比较表里并列的几个季度经营盈利没有小额调整项，不能把最后一列当成非通用。
    """
    for line in window.splitlines():
        stripped = line.strip()
        if not re.match(r"^(?:经营盈利|operating profit)\b", stripped, re.I):
            continue
        numbers = [item.strip("()") for item in _comma_numbers(stripped)]
        if len(numbers) < 4:
            continue
        if gaap_op and numbers[0] != gaap_op:
            continue
        if numbers[-1] == numbers[0]:
            continue
        try:
            first = abs(float(numbers[0].replace(",", "")))
            middles = [abs(float(item.replace(",", ""))) for item in numbers[1:-1]]
            last = abs(float(numbers[-1].replace(",", "")))
        except ValueError:
            continue
        if not first or not middles or min(middles) > first * 0.4:
            continue
        if last > first * 2.5 or last < first * 0.25:
            continue
        return numbers[-1]
    return None


def _bridge_eps_pair(window: str, gaap_eps: str | None) -> tuple[str | None, str | None]:
    """调节表摊薄行正好两列：第一列 GAAP，第二列非通用/非国际。"""
    for line in window.splitlines():
        stripped = line.strip()
        if not re.match(r"^[-－—–]\s*(?:摊薄|diluted)\b", stripped, re.I):
            continue
        decimals = _decimals(stripped)
        if len(decimals) != 2:
            continue
        if gaap_eps and decimals[0] != gaap_eps:
            continue
        if decimals[-1] == decimals[0]:
            continue
        return decimals[0], decimals[-1]
    return None, None


def _apply_bridge(text: str, period_end: date, rows: dict[str, dict]) -> None:
    blocks = _bridge_blocks(text)
    current = [window for end, window in blocks if end == period_end]
    gaap_op = _gaap_raw(rows, "operating_income")
    gaap_eps = _gaap_raw(rows, "eps_gaap")
    if "non_ifrs_operating_profit" not in rows:
        raw = next((got for window in current if (got := _bridge_operating(window, gaap_op))), None)
        if raw:
            yoy_end = date(period_end.year - 1, period_end.month, period_end.day)
            yoy_windows = [window for end, window in blocks if end == yoy_end]
            yoy_gaap = (rows.get("operating_income") or {}).get("year_ago_raw")
            yoy = next((got for window in yoy_windows if (got := _bridge_operating(window, yoy_gaap))), None)
            node = _node(raw, f"非通用/非国际财务报告准则调节表 经营盈利 {raw}", unit="millions", yoy=yoy, qoq=None)
            if node:
                rows["non_ifrs_operating_profit"] = node
    for window in current:
        gaap_raw, raw_eps = _bridge_eps_pair(window, gaap_eps)
        if not raw_eps:
            continue
        if "eps_gaap" not in rows and gaap_raw:
            gaap_node = _node(gaap_raw, f"调节表摊薄 {gaap_raw}", unit="yuan", yoy=None, qoq=None)
            if gaap_node:
                rows["eps_gaap"] = gaap_node
        eps = _node(raw_eps, f"调节表摊薄 {raw_eps}", unit="yuan", yoy=None, qoq=None)
        if eps:
            rows["eps_nongaap"] = eps
        break
    _drop_unusable_nongaap_eps(rows)


def _drop_unusable_nongaap_eps(rows: dict[str, dict]) -> None:
    """九个月摊薄或与 GAAP 相同的数不能当作单季非通用每股盈利。"""
    nongaap = rows.get("eps_nongaap")
    gaap = (rows.get("eps_gaap") or {}).get("raw")
    if not nongaap or not gaap:
        return
    if nongaap.get("raw") == gaap:
        rows.pop("eps_nongaap", None)
        return
    try:
        ratio = float(nongaap["raw"]) / float(gaap)
    except ValueError:
        return
    # 上限丢掉九个月摊薄。下限要留住大额处置收益把 GAAP EPS 抬高的季度（2021Q4 约 0.26）。
    if ratio > 2.5 or ratio < 0.15:
        rows.pop("eps_nongaap", None)


def _highlight_nongaap_eps(text: str, gaap_eps: str | None = None) -> dict | None:
    starts = (
        "按非国际财务报告准则",
        "按非通用会计准则",
        "非国际财务报告准则每股",
        "非通用会计准则每股",
    )
    pattern = re.compile(r"每股摊薄盈利为人民币\s*([\d.]+)\s*元")
    for matched_start in re.finditer("|".join(starts), text):
        chunk = text[matched_start.end():matched_start.end() + 900]
        matched = pattern.search(chunk)
        if not matched:
            continue
        raw = matched.group(1)
        if gaap_eps and raw == gaap_eps:
            continue
        node = _node(raw, matched.group(0), unit="yuan", yoy=None, qoq=None)
        if node:
            return node
    return None


_SEGMENT_SPECS = (
    ("vas_revenue", re.compile(r"^(?:增值服务|value-added services|vas)\b", re.I)),
    ("advertising_revenue", re.compile(r"^(?:营销服务|网络广告|在线广告|online advertising)\b", re.I)),
    ("fintech_biz_revenue", re.compile(r"^(?:金融科技及企业服务|fintech and business services)\b", re.I)),
)


def _attach_segments(text: str, rows: dict[str, dict]) -> None:
    """分部收入有时不在综合收益表里，而在「占收入总额百分比」的三个月表。"""
    for key, pattern in _SEGMENT_SPECS:
        if key in rows:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if "%" not in stripped or not pattern.search(stripped):
                continue
            numbers = _comma_numbers(stripped)
            if len(numbers) < 1:
                continue
            node = _node(
                numbers[0],
                stripped,
                unit="millions",
                yoy=numbers[1] if len(numbers) > 1 else None,
                qoq=None,
            )
            if node:
                rows[key] = node
            break


def _capex_near_labels(text: str, rows: dict[str, dict]) -> None:
    if "capex" in rows:
        return
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not re.match(r"^(?:资本开支|capital expenditures)\b", line.strip(), re.I):
            continue
        labels: list[str] = []
        for probe in lines[max(0, index - 8):index + 1]:
            got = _Q_LABEL.findall(probe)
            if len(got) >= 2:
                labels = got
        numbers = _comma_numbers(line)
        if not numbers:
            continue
        roles = _roles_from_labels(labels) if labels else {"current": 0}
        node = _node(
            _pick(numbers, roles.get("current", 0)),
            line.strip(),
            unit="millions",
            yoy=_pick(numbers, roles.get("yoy")),
            qoq=_pick(numbers, roles.get("qoq")),
        )
        if node:
            rows["capex"] = node
            return


def _cumulative(text: str, fiscal_period: str) -> dict | None:
    """六个月 / 九个月 / 全年收入表的本期列。不把它写成单季。"""
    lines = text.splitlines()
    best = None
    for index, line in enumerate(lines):
        if not _REVENUE_LINE.match(line.strip()) or not _comma_numbers(line):
            continue
        header = "\n".join(lines[max(0, index - 12):index + 1])
        lower = header.lower()
        months = None
        if "九个月" in header or "nine month" in lower:
            months = 9
        elif "六个月" in header or "six month" in lower or "中期" in header:
            months = 6
        elif "止年度" in header or "for the year" in lower or "十二个月" in header:
            months = 12
        if months is None:
            continue
        if "三个月" in header and months != 9:
            continue
        end = _dates_in(header)
        period_end = end[0] if end else first_period_end(header)
        if period_end is None:
            continue
        body_end = min(len(lines), index + 40)
        body = "\n".join(lines[index:body_end])
        roles = {"current": 0, "yoy": 1} if months != 9 else {"current": 2}
        # 三个月+九个月并列表：九个月本期通常是第 3 列（下标 2）。纯累计表用第 1 列。
        if months == 9 and "三个月" in header:
            roles = {"current": 2}
        elif months == 9:
            roles = {"current": 0}
        rows = _parse_rows(body, roles)
        if "revenue" not in rows:
            continue
        flows = {}
        for key in (
            "revenue", "cost_of_revenue", "gross_profit", "operating_income",
            "net_income", "vas_revenue", "advertising_revenue", "fintech_biz_revenue",
            "selling_expense", "ga_expense", "rd_expense",
        ):
            node = rows.get(key)
            if node:
                flows[key] = float(node["raw"].replace(",", ""))
        start = date(period_end.year, 1, 1)
        payload = {
            "period_start": start.isoformat(),
            "period_end": period_end.isoformat(),
            "months": months,
            "flows": flows,
            "source_quote": rows["revenue"]["source_quote"],
        }
        if best is None or months > best["months"]:
            best = payload
    if best is None:
        return None
    best["requested_period"] = fiscal_period
    return best


def parse_hk_quarter(text: str, fiscal_period: str, *, fiscal_year_end_month: int = 12) -> dict:
    """返回单季行。只有累计时抛 HkCumulativeOnly。口径对不上抛 HkImportError。"""
    prepared = prepare_statement_text(text)
    def _label(item: _Candidate) -> str | None:
        try:
            return _fiscal_from_end(item.period_end, fiscal_year_end_month)
        except HkImportError:
            return None

    found = _candidates(prepared)
    matches = [item for item in found if _label(item) == fiscal_period]
    others = [item for item in found if _label(item) not in {None, fiscal_period}]
    if not matches:
        cumulative = _cumulative(prepared, fiscal_period)
        if others and cumulative is None:
            sample = _fiscal_from_end(others[0].period_end, fiscal_year_end_month)
            raise HkImportError(
                f"公告报告期 {sample}（截至 {others[0].period_end.isoformat()}）与请求的 {fiscal_period} 不一致"
            )
        if cumulative and cumulative["months"] == 6:
            raise HkCumulativeOnly(
                f"{fiscal_period}: 公告是中期六个月，没有三个月单季表，不能写成单季",
                cumulative,
            )
        if cumulative and cumulative["months"] == 12:
            raise HkCumulativeOnly(
                f"{fiscal_period}: 全年业绩没有单季列，不能倒挤季度",
                cumulative,
            )
        if cumulative and cumulative["months"] == 9:
            raise HkCumulativeOnly(
                f"{fiscal_period}: 公告是九个月累计，没有三个月单季表，不能写成单季",
                cumulative,
            )
        raise HkImportError(f"{fiscal_period}: 无法确认这是三个月单季业绩")

    matches.sort(key=lambda item: item.score, reverse=True)
    primary = matches[0]
    rows = {key: dict(node) for key, node in primary.rows.items()}
    for item in matches[1:]:
        if item.rows.get("revenue", {}).get("raw") != rows["revenue"]["raw"]:
            continue
        _merge_missing(rows, item.rows, qoq_only=item.mode == "qoq")
    _apply_bridge(prepared, primary.period_end, rows)
    if "eps_nongaap" not in rows:
        highlighted = _highlight_nongaap_eps(prepared, (rows.get("eps_gaap") or {}).get("raw"))
        if highlighted:
            rows["eps_nongaap"] = highlighted
            _drop_unusable_nongaap_eps(rows)
    _capex_near_labels(prepared, rows)
    _attach_segments(prepared, rows)
    op_ng = (rows.get("non_ifrs_operating_profit") or {}).get("raw")
    eps_ng = (rows.get("eps_nongaap") or {}).get("raw")
    if op_ng and eps_ng and op_ng == eps_ng:
        raise HkImportError("非国际财务报告准则经营盈利被写成了每股盈利")
    return {
        "rows": rows,
        "period_end": primary.period_end,
        "mode": primary.mode,
        "roles": primary.roles,
        "labels": _Q_LABEL.findall(primary.header),
        "prepared_text": prepared,
        "extract_mode": "disclosed",
    }
