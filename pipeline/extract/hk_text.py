"""港股业绩公告文本：PDF 读取、繁体转简体、把竖排数字拼回同一行。"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

_CN_DIGIT = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_EN_MONTH = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_READABLE = ("收入", "经营盈利", "經營盈利", "Revenues", "综合收益表", "綜合收益表", "简明综合收益表")
_VALUE_LINE = re.compile(
    r"^(?:"
    r"\(\d{1,3}(?:,\d{3})+(?:\.\d+)?\)"
    r"|\d{1,3}(?:,\d{3})+(?:\.\d+)?"
    r"|\(\d{1,6}(?:\.\d+)?\)"
    r"|\d{1,6}"
    r"|\d+\.\d+%?"
    r"|\d+%"
    r"|[—–\-－]"
    r")$"
)
_NOTE_LINE = re.compile(r"^\d{1,2}\([a-z]\)$")
_Q_LINE = re.compile(r"^[1-4]Q\d{4}$")
_HAS_AMOUNT = re.compile(r"\d{1,3}(?:,\d{3})+|\d+\.\d+")
_BARE_REVENUE = {"收入", "Revenues", "Revenue"}
_COST_START = ("收入成本", "Cost of revenue", "cost of revenue")


def cn_int(token: str) -> int:
    if token.isdigit():
        return int(token)
    if "十" not in token:
        return int("".join(str(_CN_DIGIT[ch]) for ch in token if ch in _CN_DIGIT))
    left, _, right = token.partition("十")
    tens = _CN_DIGIT[left] if left else 1
    ones = _CN_DIGIT[right] if right else 0
    return tens * 10 + ones


def parse_cn_date(chunk: str) -> date | None:
    matched = re.search(
        r"([零〇一二三四五六七八九]{4}|\d{4})\s*年\s*"
        r"(十二|十一|十|[一二三四五六七八九]|\d{1,2})\s*月\s*"
        r"([一二三四五六七八九十]{1,3}|\d{1,2})\s*日",
        chunk,
    )
    if not matched:
        return None
    year_tok, month_tok, day_tok = matched.group(1), matched.group(2), matched.group(3)
    year = int(year_tok) if year_tok.isdigit() else int("".join(str(_CN_DIGIT[ch]) for ch in year_tok))
    return date(year, cn_int(month_tok), cn_int(day_tok))


def parse_en_date(chunk: str) -> date | None:
    matched = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", chunk)
    if matched:
        month = _EN_MONTH.get(matched.group(2).lower())
        if month:
            return date(int(matched.group(3)), month, int(matched.group(1)))
    matched = re.search(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", chunk)
    if matched:
        month = _EN_MONTH.get(matched.group(1).lower())
        if month:
            return date(int(matched.group(3)), month, int(matched.group(2)))
    return None


def first_period_end(chunk: str) -> date | None:
    """取片段里第一个报告期末。中文日期优先，其次英文。"""
    cn = parse_cn_date(chunk)
    en = parse_en_date(chunk)
    if cn and en:
        return cn if chunk.find("年") < chunk.lower().find(en.strftime("%Y")) else en
    return cn or en


def quarter_end_from_label(label: str) -> date | None:
    matched = re.fullmatch(r"([1-4])Q(\d{4})", label.strip())
    if not matched:
        return None
    quarter, year = int(matched.group(1)), int(matched.group(2))
    month, day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}[quarter]
    return date(year, month, day)


def to_simplified(text: str) -> str:
    import zhconv

    return zhconv.convert(text, "zh-cn")


def _attach_orphan_revenue(lines: list[str]) -> list[str]:
    """综合收益表里分部在前、合计数字单独成行时，把合计补回「收入」。"""
    out: list[str] = []
    index = 0
    while index < len(lines):
        bare = lines[index].strip()
        if bare not in _BARE_REVENUE:
            out.append(lines[index])
            index += 1
            continue
        orphan = None
        orphan_at = None
        cost_at = None
        for look in range(index + 1, min(len(lines), index + 16)):
            row = lines[look].strip()
            if row.startswith(_COST_START):
                cost_at = look
                break
            if re.fullmatch(r"[\d,.\s()（）%—–\-－]+", row) and _HAS_AMOUNT.search(row):
                orphan = row
                orphan_at = look
        if orphan and cost_at is not None and orphan_at is not None:
            out.append(f"{bare} {orphan}")
            for keep in range(index + 1, cost_at):
                if keep != orphan_at:
                    out.append(lines[keep])
            index = cost_at
            continue
        out.append(lines[index])
        index += 1
    return out


def collapse_vertical_rows(text: str) -> str:
    """pymupdf 经常把同一行的数字拆成多行。已经横排的表原样保留。"""
    raw = [line.strip() for line in text.splitlines() if line.strip()]
    out: list[str] = []
    index = 0
    total = len(raw)
    while index < total:
        line = raw[index]
        if _Q_LINE.match(line):
            labels = []
            while index < total and _Q_LINE.match(raw[index]):
                labels.append(raw[index])
                index += 1
            out.append(" ".join(labels))
            continue
        if _HAS_AMOUNT.search(line) and not _VALUE_LINE.match(line):
            out.append(line)
            index += 1
            continue
        if _VALUE_LINE.match(line):
            values = []
            while index < total and _VALUE_LINE.match(raw[index]):
                values.append(raw[index])
                index += 1
            out.append(" ".join(values))
            continue
        values = []
        cursor = index + 1
        while cursor < total and (_NOTE_LINE.match(raw[cursor]) or _VALUE_LINE.match(raw[cursor])):
            if _VALUE_LINE.match(raw[cursor]):
                values.append(raw[cursor])
            cursor += 1
        if values:
            out.append(f"{line} {' '.join(values)}")
        else:
            out.append(line)
        index = cursor
    return "\n".join(_attach_orphan_revenue(out))


def prepare_statement_text(text: str) -> str:
    simplified = to_simplified(text)
    simplified = (
        simplified.replace("（", "(")
        .replace("）", ")")
        .replace("，", ",")
        .replace("％", "%")
        .replace("\u00a0", " ")
    )
    return collapse_vertical_rows(simplified)


def _pypdf_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _fitz_text(path: Path) -> str:
    import fitz

    doc = fitz.open(str(path))
    try:
        return "\n".join((page.get_text() or "") for page in doc)
    finally:
        doc.close()


def _spacing_breaks(text: str) -> int:
    """pypdf 有时把英文单词从中间切开（Three m onths）。这种文本改用 PyMuPDF。"""
    return len(re.findall(r"[A-Za-z]\s+[a-z]", text))


def read_pdf_text(path: Path) -> str:
    """pypdf 横排且词没被切开时用它。缺中文编码或英文被拆开时改用 PyMuPDF。"""
    text = _pypdf_text(path)
    readable = any(marker in text for marker in _READABLE)
    if readable and _spacing_breaks(text) < 40:
        return text
    fallback = _fitz_text(path)
    if not readable or _spacing_breaks(fallback) < _spacing_breaks(text):
        return fallback or text
    return text or fallback
