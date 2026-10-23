"""港股业绩公告（文本）→ ExtractFinancials 形状的字典。

规则见 prompts/extract_financials_hk.md。只抄简明综合收益表里的数字，
不做汇率换算；六个月中期没有三个月列时拒绝写成单季。
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from pipeline.extract.numbers import parse_number

_CN_DIGIT = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_COMMA_NUM = re.compile(r"\(?\d{1,3}(?:,\d{3})+(?:\.\d+)?\)?")
_Q_LABEL = re.compile(r"[1-4]Q\d{4}")
_EPS_LINE = re.compile(r"^-\s*摊薄\s+")
_NON_IFRS_EPS = re.compile(r"每股摊薄盈利为人民币\s*([\d.]+)\s*元")
_FCF = re.compile(r"自由现金流为人民币\s*([\d,]+)\s*亿元")


class HkImportError(ValueError):
    """公告口径不能写入所请求的单季。"""


def pdf_to_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    parts = [(page.extract_text() or "") for page in reader.pages]
    text = "\n".join(parts)
    if len(text.strip()) < 200:
        raise HkImportError(f"PDF 抽不出足够文本: {path}")
    return text


def load_press_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return pdf_to_text(path)
    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8")
    raise HkImportError(f"不认识的业绩公告文件: {path.name}")


def _cn_int(token: str) -> int:
    if token.isdigit():
        return int(token)
    if "十" not in token:
        return int("".join(str(_CN_DIGIT[ch]) for ch in token))
    left, _, right = token.partition("十")
    tens = _CN_DIGIT[left] if left else 1
    ones = _CN_DIGIT[right] if right else 0
    return tens * 10 + ones


def _parse_cn_date(chunk: str) -> date | None:
    m = re.search(
        r"([零〇一二三四五六七八九]{4}|\d{4})\s*年\s*"
        r"(十二|十一|十|[一二三四五六七八九]|\d{1,2})\s*月\s*"
        r"([一二三四五六七八九十]{1,3}|\d{1,2})\s*日",
        chunk,
    )
    if not m:
        return None
    year_tok, month_tok, day_tok = m.group(1), m.group(2), m.group(3)
    year = int(year_tok) if year_tok.isdigit() else int("".join(str(_CN_DIGIT[ch]) for ch in year_tok))
    return date(year, _cn_int(month_tok), _cn_int(day_tok))


def _slice_between(text: str, start: str, ends: tuple[str, ...]) -> str:
    i = text.find(start)
    if i < 0:
        return ""
    j = len(text)
    for marker in ends:
        k = text.find(marker, i + len(start))
        if k > i:
            j = min(j, k)
    return text[i:j]


def _span(text: str) -> str:
    three = bool(re.search(r"止三个月|第一季|第三季|第四季", text))
    six = bool(re.search(r"止六个月|中期业绩", text)) or (
        "六个月" in text and "三个月" not in text
    )
    if six and not three:
        return "6m"
    if three:
        return "3m"
    if re.search(r"全年业绩|止年度", text):
        return "12m"
    return "unknown"


def _period_end(text: str) -> date | None:
    m = re.search(r"截至(.{0,40}?日)止", text)
    if m:
        found = _parse_cn_date(m.group(1))
        if found:
            return found
    return _parse_cn_date(text[:800])


def _release_day(text: str) -> date | None:
    m = re.search(r"香港，(.{0,30}?日)", text)
    if m:
        return _parse_cn_date(m.group(1))
    return None


def _comma_numbers(line: str) -> list[str]:
    return _COMMA_NUM.findall(line)


def _quarter_only(section: str) -> str:
    """同一张表里如果还有「止六个月」，只保留三个月那一段。"""
    if "止六个月" not in section and "六个月" not in section:
        return section
    kept: list[str] = []
    active = False
    for raw in section.splitlines():
        line = raw.strip()
        if "止六个月" in line or ("六个月" in line and "三个月" not in line):
            active = False
            continue
        if "止三个月" in line or len(_Q_LABEL.findall(line)) >= 2:
            active = True
        if active:
            kept.append(raw)
    return "\n".join(kept)


def _column_labels(section: str) -> list[str]:
    for line in section.splitlines():
        labels = _Q_LABEL.findall(line)
        if len(labels) >= 2:
            return labels
    return []


def _roles(labels: list[str]) -> dict[str, int]:
    roles = {"current": 0}
    if not labels:
        return roles
    m = re.match(r"([1-4])Q(\d{4})", labels[0])
    if not m:
        return roles
    quarter, year = int(m.group(1)), int(m.group(2))
    yoy = f"{quarter}Q{year - 1}"
    prior_q, prior_y = (4, year - 1) if quarter == 1 else (quarter - 1, year)
    prior = f"{prior_q}Q{prior_y}"
    for index, label in enumerate(labels):
        if label == yoy:
            roles["yoy"] = index
        elif label == prior:
            roles["qoq"] = index
    return roles


def _pick(nums: list[str], index: int | None) -> str | None:
    if index is None or index >= len(nums):
        return None
    return nums[index]


def _first_row(section: str, pattern: re.Pattern[str], *, comma: bool) -> tuple[str, list[str]] | None:
    for raw in section.splitlines():
        line = raw.strip()
        if not line or not pattern.search(line):
            continue
        if comma:
            nums = _comma_numbers(line)
            if not nums:
                continue
        else:
            nums = re.findall(r"\d+\.\d+", line)
            if not nums:
                continue
        return line, nums
    return None


_MONEY_ROWS: tuple[tuple[str, str], ...] = (
    ("non_ifrs_operating_profit", r"非国际财务报告准则经营盈利"),
    ("operating_income", r"^经营盈利(?!率)"),
    ("revenue", r"^收入(?!成本)"),
    ("cost_of_revenue", r"^收入成本"),
    ("gross_profit", r"^毛利(?!率)"),
    ("vas_revenue", r"^增值服务"),
    ("advertising_revenue", r"^营销服务|^网络广告|^在线广告"),
    ("fintech_biz_revenue", r"^金融科技及企业服务"),
    ("selling_expense", r"^销售及市场推广开支"),
    ("ga_expense", r"^一般及行政开支"),
    ("rd_expense", r"^研发开支|^研究及开发"),
    ("net_income", r"^期内盈利"),
)


def _node(raw: str | None, quote: str, *, unit: str, yoy: str | None, qoq: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    return {
        "raw": raw.strip("()"),
        "unit": unit,
        "currency": "CNY",
        "period_type": "3m",
        "months": 3,
        "source_quote": quote[:200],
        "year_ago_raw": None if yoy is None else yoy.strip("()"),
        "prior_quarter_raw": None if qoq is None else qoq.strip("()"),
    }


def _money_row(section: str, pattern: str, roles: dict[str, int]) -> dict[str, Any] | None:
    found = _first_row(section, re.compile(pattern), comma=True)
    if not found:
        return None
    line, nums = found
    return _node(
        _pick(nums, roles.get("current", 0)),
        line,
        unit="millions",
        yoy=_pick(nums, roles.get("yoy")),
        qoq=_pick(nums, roles.get("qoq")),
    )


def _highlight_eps(text: str, start: str, end: str) -> str | None:
    chunk = _slice_between(text, start, (end,))
    m = _NON_IFRS_EPS.search(chunk)
    return m.group(1) if m else None


def _fiscal_label(end: date, fiscal_year_end_month: int) -> str:
    """日历年末月为 12 时，3/6/9/12 月末就是 Q1–Q4。不引用 compute，避免抽取层向上依赖。"""
    if fiscal_year_end_month != 12:
        raise HkImportError("港股手工导入目前只核对财年末月为 12 的公司")
    quarter = {3: 1, 6: 2, 9: 3, 12: 4}.get(end.month)
    if quarter is None or end.day < 28:
        raise HkImportError(f"报告期末 {end.isoformat()} 不是单季月末，拒绝猜测财季")
    return f"FY{end.year}Q{quarter}"


def extract_hk_results(
    text: str,
    fiscal_period: str,
    *,
    fiscal_year_end_month: int = 12,
) -> dict[str, Any]:
    """从业绩公告全文抽出本期字段。口径不对时抛 HkImportError，不写单季。"""
    warnings: list[str] = []
    span = _span(text)
    if span == "6m":
        raise HkImportError(
            f"{fiscal_period}: 公告是中期六个月，没有三个月单季表，不能写成单季"
        )
    if span == "12m":
        raise HkImportError(f"{fiscal_period}: 全年业绩没有单季列，不能倒挤季度")
    if span != "3m":
        raise HkImportError(f"{fiscal_period}: 无法确认这是三个月单季业绩")

    end = _period_end(text)
    if end is None:
        raise HkImportError("公告里没有「截至…止」的报告期")
    inferred = _fiscal_label(end, fiscal_year_end_month)
    if inferred != fiscal_period:
        raise HkImportError(
            f"公告报告期 {inferred}（截至 {end.isoformat()}）与请求的 {fiscal_period} 不一致"
        )

    statement = _quarter_only(
        _slice_between(text, "简明综合收益表", ("简明综合全面收益表", "简明综合财务状况表"))
    )
    if not statement.strip():
        raise HkImportError("未见简明综合收益表，拒绝从摘要句子猜数字")
    other = _quarter_only(
        _slice_between(text, "其他财务资料", ("附注", "简明综合财务状况表", "非国际财务报告准则财务计量与"))
    )

    labels = _column_labels(statement) or _column_labels(other)
    roles = _roles(labels)
    if "yoy" not in roles:
        warnings.append("收益表没有去年同季列，同比留空")

    def take(section: str, pattern: str) -> dict[str, Any] | None:
        return _money_row(section, pattern, roles)

    rows = {key: take(statement, pat) for key, pat in _MONEY_ROWS}
    capex = take(other or statement, r"^资本开支")
    if rows["revenue"] is None or rows["gross_profit"] is None or rows["net_income"] is None:
        raise HkImportError("收益表缺少收入、毛利或期内盈利")

    eps_row = _first_row(statement, _EPS_LINE, comma=False)
    eps_gaap = None
    if eps_row:
        line, nums = eps_row
        eps_gaap = _node(
            _pick(nums, roles.get("current", 0)),
            line,
            unit="yuan",
            yoy=_pick(nums, roles.get("yoy")),
            qoq=_pick(nums, roles.get("qoq")),
        )
    if eps_gaap is None:
        warnings.append("收益表没有摊薄每股盈利行")

    non_ifrs_eps_raw = _highlight_eps(text, "按非国际财务报告准则", "按国际财务报告准则")
    ifrs_eps_raw = _highlight_eps(text, "按国际财务报告准则", "资本开支")
    if eps_gaap and ifrs_eps_raw and eps_gaap["raw"] != ifrs_eps_raw:
        warnings.append(
            f"收益表摊薄 EPS {eps_gaap['raw']} 与国际财务报告准则摘要 {ifrs_eps_raw} 不一致，采用收益表"
        )
    eps_nongaap = None
    if non_ifrs_eps_raw:
        quote_m = _NON_IFRS_EPS.search(text)
        eps_nongaap = {
            "raw": non_ifrs_eps_raw,
            "unit": "yuan",
            "currency": "CNY",
            "period_type": "3m",
            "months": 3,
            "label": "非国际财务报告准则每股摊薄盈利",
            "source_quote": (quote_m.group(0) if quote_m else non_ifrs_eps_raw)[:200],
        }

    op_ng = rows["non_ifrs_operating_profit"]
    if op_ng and eps_nongaap and op_ng["raw"] == eps_nongaap["raw"]:
        raise HkImportError("非国际财务报告准则经营盈利被写成了每股盈利")

    kpis: dict[str, Any] = {}
    for key in (
        "vas_revenue",
        "advertising_revenue",
        "fintech_biz_revenue",
        "selling_expense",
        "ga_expense",
        "rd_expense",
        "non_ifrs_operating_profit",
    ):
        node = rows.get(key)
        if node:
            kpis[key] = node
    fcf_m = _FCF.search(text)
    if fcf_m:
        kpis["free_cash_flow"] = {
            "raw": fcf_m.group(1),
            "unit": "亿元",
            "currency": "CNY",
            "period_type": "3m",
            "months": 3,
            "source_quote": fcf_m.group(0)[:200],
        }
        warnings.append("自由现金流只记入 kpis.free_cash_flow；公告未单列经营现金流，不写入 financials.fcf")
    else:
        warnings.append("公告未单列经营现金流，operating_cash_flow 与 fcf 留空")

    release = _release_day(text)
    extracted = {
        "period_label": fiscal_period,
        "period_end_date": end.isoformat(),
        "release_date": release.isoformat() if release else None,
        "period_type": "3m",
        "currency": "CNY",
        "column_labels": labels,
        "column_roles": roles,
        "revenue": rows["revenue"],
        "cost_of_revenue": rows["cost_of_revenue"],
        "gross_profit_gaap": rows["gross_profit"],
        "gross_profit_nongaap": None,
        "rd_expense": rows["rd_expense"],
        "operating_income_gaap": rows["operating_income"],
        "operating_income_nongaap": op_ng,
        "net_income_gaap": rows["net_income"],
        "eps_gaap_diluted": eps_gaap,
        "eps_nongaap_diluted": eps_nongaap,
        "has_nongaap_eps": eps_nongaap is not None,
        "capex_gross": capex,
        "operating_cash_flow": None,
        "kpis": kpis,
        "prior_year_comparables": {
            "revenue": {"raw": rows["revenue"].get("year_ago_raw"), "unit": "millions"}
        },
        "warnings": warnings,
    }
    return extracted
