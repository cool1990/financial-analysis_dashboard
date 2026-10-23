from __future__ import annotations

import contextvars
import json
import re
import shutil
from datetime import date, datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from pipeline import ROOT
from pipeline.compute.periods import (
    make_fiscal_period,
    next_fiscal_period,
    parse_fiscal_period,
    prior_fiscal_period,
    yoy_fiscal_period,
)
from pipeline.compute.valuation import eps_currency, pe_multiple, price_currency
from pipeline.compute.derived import derived_metrics, evaluate, merge_checks
from pipeline.config import _plain, data_dir, list_tickers, load_position, load_settings, load_ticker_config, site_dir

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")
BJ = ZoneInfo("Asia/Shanghai")
_REPORTING_CCY: contextvars.ContextVar[str] = contextvars.ContextVar("reporting_ccy", default="USD")


def _reporting_currency(ticker: str) -> str:
    try:
        cfg = load_ticker_config(ticker)
    except (FileNotFoundError, OSError):
        return "USD"
    return str(cfg.get("reporting_currency") or "USD").upper()


def _statement_basis(ticker: str) -> str:
    try:
        basis = str(load_ticker_config(ticker).get("accounting_basis") or "").lower()
    except (FileNotFoundError, OSError):
        return "GAAP"
    return "IFRS" if basis in {"ifrs", "hkfrs"} else "GAAP"


def _ccy_prefix(currency: str | None = None) -> str:
    ccy = (currency or _REPORTING_CCY.get() or "USD").upper()
    return {"CNY": "¥", "RMB": "¥", "HKD": "HK$", "USD": "$"}.get(ccy, "$")


def _chart_units(currency: str | None = None) -> tuple[str, str]:
    ccy = (currency or _REPORTING_CCY.get() or "USD").upper()
    if ccy in {"CNY", "RMB"}:
        return "十亿人民币", "人民币"
    if ccy == "HKD":
        return "十亿港元", "港元"
    return "十亿美元", "美元"


# ---------------------------------------------------------------- 数字格式


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_pct(value: float | None, pp: bool = False) -> str:
    v = _num(value)
    if v is None:
        return "—"
    if pp:
        return f"{v*100:+.2f} 百分点" if abs(v) < 2 else f"{v:+.2f} 百分点"
    return f"{v*100:+.2f}%"


def _fmt_money(value: float | None, currency: str | None = None) -> str:
    v = _num(value)
    if v is None:
        return "—"
    av = abs(v)
    sign = "-" if v < 0 else ""
    prefix = _ccy_prefix(currency)
    if av >= 1_000_000_000:
        return f"{sign}{prefix}{av/1_000_000_000:.2f}B"
    if av >= 1_000_000:
        return f"{sign}{prefix}{av/1_000_000:.2f}M"
    return f"{sign}{prefix}{av:,.2f}"


def _is_ratio_key(key: str | None) -> bool:
    k = key or ""
    return "margin" in k or k.endswith("_rate") or k.startswith("wafer_revenue_mix.")


def _fmt_value(value: float | None, key: str | None) -> str:
    """按指标类型格式化本季数值：利润率 → 87.0%，EPS → $33.42，其余金额。"""
    v = _num(value)
    if v is None:
        return "—"
    if _is_ratio_key(key):
        return f"{v*100:.1f}%" if abs(v) <= 1.5 else f"{v:.1f}%"
    if "eps" in (key or ""):
        return f"{_ccy_prefix()}{v:.2f}"
    return _fmt_money(v)


def _change(value: float | None, *, pp: bool = False) -> dict[str, str]:
    """同比 / 环比 / 差异：返回 {"text", "cls"}；变化超过 10 倍时改写成倍数，避免 +45543%。"""
    v = _num(value)
    if v is None:
        return {"text": "—", "cls": "na"}
    cls = "up" if v > 0 else "down" if v < 0 else "flat"
    if pp:
        return {"text": f"{v*100:+.1f}pp", "cls": cls}
    if abs(v) >= 10:
        return {"text": f"{v + 1:.0f} 倍", "cls": cls}
    return {"text": f"{v*100:+.1f}%", "cls": cls}


def _fmt_guide(value: float | None, metric_key: str | None) -> str:
    """指引数值：利润率/税率类按百分比，EPS 按美元，股本按股数，其余按金额。"""
    v = _num(value)
    if v is None:
        return "—"
    key = metric_key or ""
    if "margin" in key or "rate" in key:
        return f"{v*100:.2f}%" if abs(v) <= 1 else f"{v:.2f}%"
    if "eps" in key:
        return f"{_ccy_prefix()}{v:.2f}"
    if "share" in key:
        return f"{v/1e9:.2f}B 股" if abs(v) >= 1e9 else f"{v/1e6:.1f}M 股"
    return _fmt_money(v)


def _fmt_release(raw: str | None) -> str:
    """发布时间同时给美东与北京时间。"""
    if not raw:
        return "—"
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return str(raw)
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d %H:%M")
    et, bj = dt.astimezone(ET), dt.astimezone(BJ)
    return f"{et:%Y-%m-%d %H:%M} 美东（北京 {bj:%m-%d %H:%M}）"


# ---------------------------------------------------------------- 中文标签

METRIC_LABELS = {
    "revenue": "营收",
    "gross_margin": "毛利率",
    "gross_margin_gaap": "毛利率（GAAP）",
    "gross_margin_nongaap": "毛利率（Non-GAAP）",
    "operating_margin": "营业利润率",
    "operating_margin_gaap": "营业利润率（GAAP）",
    "operating_margin_nongaap": "营业利润率（Non-GAAP）",
    "eps": "每股收益",
    "eps_gaap": "稀释每股收益（GAAP）",
    "eps_nongaap": "稀释每股收益（Non-GAAP）",
    "operating_cash_flow": "经营现金流",
    "capex": "资本开支",
    "fcf": "自由现金流",
    "net_income_gaap": "净利润（GAAP）",
    "net_income_nongaap": "净利润（Non-GAAP）",
    "opex_gaap": "运营费用（GAAP）",
    "opex_nongaap": "运营费用（Non-GAAP）",
    "share_count": "稀释股本",
    "next_q_revenue_guidance": "下季营收指引",
    "next_q_eps_guidance": "下季每股收益指引",
}

STAGE_LABELS = {
    "stage1_done": "阶段1 · 新闻稿",
    "stage2_done": "阶段2 · 含电话会",
    "stage3_done": "阶段2 · 含电话会",  # Stage3 已停用；历史 stage3_done 按 Stage2 展示
    "transcript_found": "已找到文字稿",
    "backfill_gaap": "历史回补（GAAP）",
    "scheduled": "待财报",
}

SOURCE_LABELS = {
    "press_release": "新闻稿",
    "prepared_remarks": "管理层发言",
    "qa": "问答环节",
    "motley_fool": "Motley Fool 文字稿",
    "ir_page": "投资者关系",
    "manual": "手动上传",
    "consensus": "一致预期",
    "none": "无",
    "xbrl": "SEC XBRL",
    "gaap": "GAAP",
    "non_gaap": "Non-GAAP",
    "unspecified": "未注明",
}

DIRECTION_LABELS = {"up": "看高", "down": "看低", "flat": "持平"}
DIRECTNESS_LABELS = {"direct": "直接回答", "partial": "部分回答", "evasive": "回避"}
TONE_LABELS = {"positive": "偏积极", "neutral": "中性", "cautious": "偏谨慎"}
VERDICT_LABELS = {"beat": "超预期", "miss": "不及预期", "inline": "符合预期", "unknown": "待核对"}

METRIC_SECTIONS = [
    ("revenue", "营业收入", False),
    ("gross_margin_nongaap", "毛利率（Non-GAAP）", True),
    ("gross_margin_gaap", "毛利率（GAAP）", True),
    ("operating_margin_nongaap", "营业利润率（Non-GAAP）", True),
    ("operating_margin_gaap", "营业利润率（GAAP）", True),
    ("eps_nongaap", "稀释每股收益（Non-GAAP）", False),
    ("eps_gaap", "稀释每股收益（GAAP）", False),
    ("operating_cash_flow", "经营现金流", False),
    ("capex", "资本开支", False),
    ("fcf", "自由现金流", False),
]


def _metric_label(key: str | None) -> str:
    return METRIC_LABELS.get(key or "", key or "—")


def _stage_label(stage: str | None) -> str:
    return STAGE_LABELS.get(stage or "", stage or "—")


def _source_label(src: str | None) -> str:
    if not src:
        return "—"
    return SOURCE_LABELS.get(src, src)


def _direction_label(d: str | None) -> str:
    return DIRECTION_LABELS.get(d or "", d or "")


def _directness_label(d: str | None) -> str:
    return DIRECTNESS_LABELS.get(d or "", d or "—")


def _tone_label(t: str | None) -> str:
    return TONE_LABELS.get(t or "", t or "—")


def _verdict_label(v: str | None) -> str:
    return VERDICT_LABELS.get(v or "", v or "—")


def _dedupe(items: list | None) -> list:
    seen: set[str] = set()
    out = []
    for w in items or []:
        if str(w) not in seen:
            seen.add(str(w))
            out.append(w)
    return out


def _humanize_warning(text: str) -> str:
    """把残留的英文/元组告警转成更可读的中文。"""
    s = str(text)
    s = s.replace("Expecting value: line 1 column 1 (char 0)", "模型返回空内容，结构化失败")
    m = re.search(r"电话会指引与新闻稿不一致:\s*\('([^']*)',\s*'([^']*)'\)", s)
    if m:
        return f"电话会与新闻稿指引条目冲突（已拆分展示）：{_metric_label(m.group(1))} · {m.group(2)}"
    return s


def _is_stage3_warning(text: str) -> bool:
    """Stage3 已停用：次日股价反应相关告警不再展示。"""
    s = str(text or "")
    markers = (
        "次日涨跌",
        "股价反应",
        "等待阶段3",
        "交易日收盘价不足，暂无法计算次日涨跌",
        "交易日数据不足",
    )
    return any(m in s for m in markers)


def _period_zh(period: str | None) -> str:
    if not period:
        return "—"
    out = period.strip()
    repl = [
        (r"(?i)\bFQ([1-4])-(\d{2})\b", r"20\2财年Q\1"),
        (r"(?i)q([1-4])\s*fiscal\s*(\d{4})", r"\2财年Q\1"),
        (r"(?i)(first|1st) quarter (of )?fiscal\s*(\d{4})", r"\3财年Q1"),
        (r"(?i)second half of calendar\s*(\d{4})", r"\1日历年下半年"),
        (r"(?i)calendar\s*(\d{4}) and (\d{4})", r"\1–\2日历年"),
        (r"(?i)fiscal\s*(\d{4}) balance", r"\1财年余下季度"),
        (r"(?i)fiscal\s*(\d{4})", r"\1财年"),
        (r"(?i)calendar\s*(\d{4})", r"\1日历年"),
        (r"(?i)late\s*(\d{4})", r"\1年末"),
        (r"(?i)through\s*(\d{4})", r"至\1年"),
        (r"(?i)beyond\s*(\d{4})", r"\1年以后"),
        (r"(?i)not specified|unspecified", "未指明"),
        (r"(?i)second half of next year", "明年下半年"),
    ]
    for pat, rep in repl:
        out = re.sub(pat, rep, out)
    return out


# ---------------------------------------------------------------- 视图模型


def _empty_benchmark() -> dict[str, Any]:
    return {"value": None, "source": "none", "diff": None, "diff_pct": None, "diff_pp": None}


def _wafer_mix_sections(fin: dict[str, Any], drivers: dict[str, Any]) -> list[dict[str, Any]]:
    """制程节点占晶圆收入的百分比。没有美元金额，不占金额行。"""
    mix = ((fin.get("kpis") or {}).get("wafer_revenue_mix") or {})
    lines = mix.get("lines") if isinstance(mix, dict) else None
    if not lines:
        return []
    advanced_driver = drivers.get("wafer_revenue_mix.advanced") or drivers.get("kpis.advanced_node_revenue")
    sections: list[dict[str, Any]] = []
    for line in lines:
        if not isinstance(line, dict) or line.get("value") is None:
            continue
        key = f"wafer_revenue_mix.{line.get('key')}"
        driver = drivers.get(key)
        if driver is None and line.get("key") == "advanced":
            driver = advanced_driver
        sections.append(
            {
                "key": key,
                "label": line.get("name") or key,
                "is_ratio": True,
                "is_eps": False,
                "metric": {
                    "value": line.get("value"),
                    "yoy_pct": None,
                    "qoq_pct": None,
                    "yoy_pp": line.get("yoy_pp"),
                    "qoq_pp": line.get("qoq_pp"),
                    "benchmark": _empty_benchmark(),
                    "source_quote": line.get("source_quote") or mix.get("source_quote"),
                },
                "driver": driver,
            }
        )
    return sections


def _metric_sections(doc: dict[str, Any]) -> list[dict[str, Any]]:
    fin = doc.get("financials") or {}
    drivers = {(m.get("metric") or ""): m for m in ((doc.get("drivers") or {}).get("metrics") or [])}
    used: set[str] = set()
    sections: list[dict[str, Any]] = []
    for key, label, is_ratio in METRIC_SECTIONS:
        metric = fin.get(key)
        if not metric:
            continue
        used.add(key)
        sections.append(
            {
                "key": key,
                "label": (metric or {}).get("display_label") or label,
                "is_ratio": is_ratio,
                "is_eps": "eps" in key,
                "metric": metric,
                "driver": drivers.get(key),
            }
        )
        if key == "revenue":
            for section in _wafer_mix_sections(fin, drivers):
                used.add(section["key"])
                sections.append(section)
            used.add("kpis.advanced_node_revenue")
    for key, drv in drivers.items():
        if key in used:
            continue
        # 没有金额的 KPI 壳（例如只有先进制程占比）不渲染成空金额行。
        if key.startswith("kpis.") or key.startswith("wafer_revenue_mix."):
            metric = fin.get(key)
            if not (isinstance(metric, dict) and metric.get("value") is not None):
                continue
        sections.append(
            {
                "key": key,
                "label": _metric_label(key),
                "is_ratio": _is_ratio_key(key),
                "is_eps": "eps" in (key or ""),
                "metric": fin.get(key),
                "driver": drv,
            }
        )
    return sections


def _block_num(doc: dict[str, Any] | None, key: str, field: str = "value") -> float | None:
    if not doc:
        return None
    return _num(((doc.get("financials") or {}).get(key) or {}).get(field))


def _quote_money(ticker: str, period: str) -> dict[str, dict[str, float | None]]:
    """新闻稿表格上的利润额：本季、上季、去年同季。没有抽取结果时返回空。"""
    extracted = _read_json(data_dir(ticker) / "raw" / period / "extracted_financials.json") or {}
    if not extracted:
        return {}
    from pipeline.compute.comparatives import _cells_from_anchor, detect_layout
    from pipeline.extract.numbers import ParsedAmount

    layout = detect_layout(extracted) or {}
    out: dict[str, dict[str, float | None]] = {}
    for field in _QUOTE_MONEY_FIELDS:
        item = extracted.get(field)
        if not isinstance(item, dict) or not item.get("raw"):
            continue
        slots: dict[str, float | None] = {
            "value": ParsedAmount.from_dict(item).value,
            "year_ago": None,
            "prior_q": None,
        }
        cells = _cells_from_anchor(item)
        if cells and layout:
            for name, key in (("year_ago", "yoy"), ("prior_q", "qoq")):
                idx = layout.get(key)
                if idx is None or idx >= len(cells):
                    continue
                slots[name] = ParsedAmount.from_dict(
                    {"raw": repr(cells[idx]), "unit": item.get("unit")}
                ).value
        out[field] = slots
    return out


def _absolute_at(
    docs: dict[str, dict[str, Any]],
    anchor: dict[str, Any],
    anchor_period: str,
    ticker: str,
    target: str,
    money_key: str,
    margin_key: str | None,
) -> tuple[float | None, bool]:
    """某一季的利润额。相邻季度页优先；否则用本季块上的对比列或新闻稿表格。"""
    doc = docs.get(target)
    if doc is not None:
        if margin_key:
            rev = _block_num(doc, "revenue")
            margin = _block_num(doc, margin_key)
            if rev is not None and margin is not None:
                return rev * margin, False
        stored = _block_num(doc, money_key)
        if stored is not None:
            return stored, False
    which = "value" if target == anchor_period else None
    if target == yoy_fiscal_period(anchor_period):
        which = "year_ago"
    elif target == prior_fiscal_period(anchor_period):
        which = "prior_q"
    if which is None:
        return None, False
    if which != "value":
        if margin_key:
            rev = _block_num(anchor, "revenue", which)
            margin = _block_num(anchor, margin_key, which)
            if rev is not None and margin is not None:
                return rev * margin, True
        stored = _block_num(anchor, money_key, which)
        if stored is not None:
            return stored, True
    quoted = (_quote_money(ticker, anchor_period).get(money_key) or {}).get(which)
    if quoted is not None:
        return quoted, which != "value"
    return None, False


def _companion_row(
    *,
    key: str,
    label: str,
    value: float | None,
    yoy: float | None,
    qoq: float | None,
    nested: bool,
    from_press: bool,
) -> dict[str, Any] | None:
    if value is None:
        return None
    ratio = _is_ratio_key(key)
    return {
        "key": key,
        "label": label,
        "value": _fmt_value(value, key),
        "yoy": _change(yoy, pp=ratio),
        "qoq": _change(qoq, pp=ratio),
        "vs": {"text": "—", "cls": "na"},
        "vs_source": "",
        "summary": "",
        "drivers": [],
        "conflicts": [],
        "from_press": from_press,
        "nested": nested,
    }


def _levels(
    docs: dict[str, dict[str, Any]],
    anchor: dict[str, Any],
    period: str,
    ticker: str,
    money_key: str,
    margin_key: str | None,
) -> tuple[float | None, float | None, float | None, bool]:
    cur, _ = _absolute_at(docs, anchor, period, ticker, period, money_key, margin_key)
    yoy_v, yoy_press = _absolute_at(
        docs, anchor, period, ticker, yoy_fiscal_period(period), money_key, margin_key
    )
    qoq_v, qoq_press = _absolute_at(
        docs, anchor, period, ticker, prior_fiscal_period(period), money_key, margin_key
    )
    return cur, yoy_v, qoq_v, yoy_press or qoq_press


def _with_profit_companions(
    rows: list[dict[str, Any]],
    doc: dict[str, Any],
    docs: dict[str, dict[str, Any]],
    ticker: str,
    period: str,
) -> list[dict[str, Any]]:
    """营业利润率下面补利润额；营业利润块之后补净利润率与净利润。"""
    seen = {row["key"] for row in rows}
    basis = _statement_basis(ticker)
    adjusted = "Non-IFRS" if basis == "IFRS" else "Non-GAAP"
    companions = (
        ("operating_margin_nongaap", "operating_income_nongaap", f"营业利润（{adjusted}）"),
        ("operating_margin_gaap", "operating_income_gaap", f"营业利润（{basis}）"),
        ("operating_margin", "operating_income", "营业利润"),
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(row)
        spec = next((item for item in companions if item[0] == row["key"]), None)
        if not spec or spec[1] in seen:
            continue
        margin_key, money_key, label = spec
        if _block_num(doc, margin_key) is None:
            continue
        cur, yoy_v, qoq_v, from_press = _levels(docs, doc, period, ticker, money_key, margin_key)
        made = _companion_row(
            key=money_key,
            label=label,
            value=cur,
            yoy=_rel(cur, yoy_v, False),
            qoq=_rel(cur, qoq_v, False),
            nested=False,
            from_press=from_press,
        )
        if made:
            seen.add(money_key)
            out.append(made)

    if any(row["key"].startswith("net_income") or row["key"].startswith("net_margin") for row in out):
        return out
    net_rows: list[dict[str, Any]] = []
    for money_key, margin_label, money_label in (
        ("net_income_nongaap", f"净利润率（{adjusted}）", f"净利润（{adjusted}）"),
        ("net_income_gaap", f"净利润率（{basis}）", f"净利润（{basis}）"),
    ):
        margin_key = money_key.replace("net_income", "net_margin")
        ni, ni_y, ni_q, ni_press = _levels(docs, doc, period, ticker, money_key, None)
        rev, rev_y, rev_q, rev_press = _levels(docs, doc, period, ticker, "revenue", None)
        if ni is None or not rev:
            continue
        margin = ni / rev
        margin_y = ni_y / rev_y if ni_y is not None and rev_y else None
        margin_q = ni_q / rev_q if ni_q is not None and rev_q else None
        net_rows.append(
            _companion_row(
                key=margin_key,
                label=margin_label,
                value=margin,
                yoy=_rel(margin, margin_y, True),
                qoq=_rel(margin, margin_q, True),
                nested=False,
                from_press=ni_press or rev_press,
            )
        )
        net_rows.append(
            _companion_row(
                key=money_key,
                label=money_label,
                value=ni,
                yoy=_rel(ni, ni_y, False),
                qoq=_rel(ni, ni_q, False),
                nested=False,
                from_press=ni_press,
            )
        )
    net_rows = [row for row in net_rows if row]
    if not net_rows:
        return out
    insert_at = None
    for i, row in enumerate(out):
        if row["key"].startswith("operating_margin") or row["key"].startswith("operating_income"):
            insert_at = i + 1
    if insert_at is None:
        for i, row in enumerate(out):
            if row["key"].startswith("eps"):
                insert_at = i
                break
    if insert_at is None:
        insert_at = len(out)
    return out[:insert_at] + net_rows + out[insert_at:]


def _fill_profit_levels(points: dict[str, dict[str, Any]], docs: dict[str, dict[str, Any]]) -> None:
    """趋势图补上利润额和净利润率。已有季度页用营收 × 利润率，缺页用新闻稿对比列。"""
    ticker = ""
    for doc in docs.values():
        ticker = (doc.get("meta") or {}).get("ticker") or ""
        if ticker:
            break

    def product(point: dict[str, Any]) -> None:
        rev = point.get("revenue")
        for margin_key, money_key in (
            ("gross_margin_gaap", "gross_profit_gaap"),
            ("gross_margin_nongaap", "gross_profit_nongaap"),
            ("operating_margin_gaap", "operating_income_gaap"),
            ("operating_margin_nongaap", "operating_income_nongaap"),
        ):
            margin = point.get(margin_key)
            if point.get(money_key) is None and rev is not None and margin is not None:
                point[money_key] = rev * margin
        for money_key, margin_key in (
            ("net_income_gaap", "net_margin_gaap"),
            ("net_income_nongaap", "net_margin_nongaap"),
        ):
            ni = point.get(money_key)
            if point.get(margin_key) is None and ni is not None and rev:
                point[margin_key] = ni / rev

    for point in points.values():
        product(point)
    if not ticker:
        return
    for period in docs:
        quoted = _quote_money(ticker, period)
        if not quoted:
            continue
        slots = {
            period: "value",
            yoy_fiscal_period(period): "year_ago",
            prior_fiscal_period(period): "prior_q",
        }
        for target, slot in slots.items():
            if slot != "value" and target in docs:
                continue
            point = points.get(target)
            if point is None:
                continue
            for key, levels in quoted.items():
                if point.get(key) is None and levels.get(slot) is not None:
                    point[key] = levels[slot]
            product(point)


_MONTHS = {
    name: index
    for index, name in enumerate(
        ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"),
        1,
    )
}
_PRESS_CACHE: dict[tuple[str, str], list[str] | None] = {}
_NA = {"text": "—", "cls": "na"}


def _press_lines(ticker: str, period: str) -> list[str] | None:
    key = (ticker, period)
    if key in _PRESS_CACHE:
        return _PRESS_CACHE[key]
    path = data_dir(ticker) / "raw" / period / "press_release.html"
    if not path.exists():
        _PRESS_CACHE[key] = None
        return None
    text = unescape(path.read_text(encoding="utf-8", errors="replace"))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(?:p|div|tr|h\d|li|table|td|th)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\xa0", " ")
    lines = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    _PRESS_CACHE[key] = lines
    return lines


def _parse_cell(text: str) -> float | None:
    token = text.strip().replace(",", "").replace("$", "")
    if token in {"", "—", "-", "–", "%"}:
        return None
    negative = token.startswith("(") and token.endswith(")")
    if negative:
        token = token[1:-1]
    if token.endswith("%"):
        token = token[:-1]
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", token):
        return None
    value = float(token)
    return -value if negative else value


def _take_numbers(lines: list[str], start: int, count: int) -> list[float | None]:
    nums: list[float | None] = []
    index = start
    while index < len(lines) and len(nums) < count:
        line = lines[index]
        if line in {"$", "%"}:
            index += 1
            continue
        if line in {"—", "-", "–"}:
            nums.append(None)
            index += 1
            continue
        if not re.fullmatch(r"\(?\$?-?[\d,]+(?:\.\d+)?\)?%?", line):
            break
        nums.append(_parse_cell(line))
        index += 1
    return nums


def _header_dates(lines: list[str]) -> list[date]:
    months: list[tuple[int, int]] = []
    years: list[int] = []
    for line in lines:
        if len(line) > 48:
            continue
        matched = re.search(
            r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?(?:\s+(\d{4}))?",
            line,
        )
        if matched:
            months.append((_MONTHS[matched.group(1)], int(matched.group(2))))
            if matched.group(3):
                years.append(int(matched.group(3)))
            continue
        if re.fullmatch(r"\d{4}", line):
            years.append(int(line))
    if not months or not years:
        return []
    if len(months) == len(years):
        pairs = list(zip(months, years))
    elif len(years) % len(months) == 0:
        group = len(years) // len(months)
        pairs = []
        for index, month_day in enumerate(months):
            for year in years[index * group : (index + 1) * group]:
                pairs.append((month_day, year))
    else:
        return []
    found: list[date] = []
    for (month, day), year in pairs:
        try:
            found.append(date(year, month, day))
        except ValueError:
            continue
    return found


def _near(left: date | None, right: date | None, tolerance: int) -> bool:
    return left is not None and right is not None and abs((left - right).days) <= tolerance


def _column_roles(dates: list[date], period: str, doc: dict[str, Any], docs: dict[str, dict[str, Any]]) -> dict[int, str]:
    end = _as_date((doc.get("meta") or {}).get("period_end"))
    roles: dict[int, str] = {}
    if end is None:
        if dates:
            roles[0] = "current"
        return roles
    prior_doc = docs.get(prior_fiscal_period(period)) or {}
    yoy_doc = docs.get(yoy_fiscal_period(period)) or {}
    prior_end = _as_date((prior_doc.get("meta") or {}).get("period_end")) or (end - timedelta(days=91))
    yoy_end = _as_date((yoy_doc.get("meta") or {}).get("period_end")) or (end - timedelta(days=365))
    for index, day in enumerate(dates):
        if _near(day, end, 12):
            kind = "current"
        elif _near(day, prior_end, 28):
            kind = "qoq"
        elif _near(day, yoy_end, 28):
            kind = "yoy"
        else:
            continue
        if kind not in roles.values():
            roles[index] = kind
    if dates and "current" not in roles.values():
        roles[0] = "current"
    return roles


def _pick_role(nums: list[float | None] | None, roles: dict[int, str], kind: str) -> float | None:
    if not nums:
        return None
    for index, role in roles.items():
        if role == kind and index < len(nums) and nums[index] is not None:
            return nums[index]
    if kind == "current" and nums and nums[0] is not None:
        return nums[0]
    return None


def _section(lines: list[str], start_pred, end_pred) -> tuple[int, int] | None:
    start = next((index for index, line in enumerate(lines) if start_pred(line)), None)
    if start is None:
        return None
    end = next((index for index in range(start + 1, len(lines)) if end_pred(lines[index])), len(lines))
    return start, end


def _flow_levels(doc: dict[str, Any], key: str, *, ratio: bool = False) -> tuple[float | None, float | None, float | None]:
    block = (doc.get("financials") or {}).get(key) or {}
    if not isinstance(block, dict):
        return None, None, None
    current = _finite(block.get("value"))
    yoy = _finite(block.get("year_ago"))
    qoq = _finite(block.get("prior_q"))
    if ratio and current is not None:
        if yoy is None and block.get("yoy_pp") is not None:
            yoy = current - _finite(block.get("yoy_pp"))
        if qoq is None and block.get("qoq_pp") is not None:
            qoq = current - _finite(block.get("qoq_pp"))
    if not ratio and current is not None:
        if yoy is None and block.get("yoy_pct") not in (None, -1):
            pace = _finite(block.get("yoy_pct"))
            if pace is not None:
                yoy = current / (1 + pace)
        if qoq is None and block.get("qoq_pct") not in (None, -1):
            pace = _finite(block.get("qoq_pct"))
            if pace is not None:
                qoq = current / (1 + pace)
    return current, yoy, qoq


def _period_day_count(doc: dict[str, Any]) -> int | None:
    start = _as_date((doc.get("meta") or {}).get("period_start"))
    end = _as_date((doc.get("meta") or {}).get("period_end"))
    if start is None or end is None or end < start:
        return None
    # 有的回补把上一季结束日写成了本季开始日（例如 3 月 31 日）。季末那天起算会多一天。
    if start.day >= 28 and start.month in {3, 6, 9, 12}:
        nxt = start + timedelta(days=1)
        span = (end - nxt).days + 1
        if 80 <= span <= 100:
            return span
    return (end - start).days + 1


def _turnover_days(stock: float | None, flow: float | None, days: int | None) -> float | None:
    if stock is None or flow in (None, 0) or not days:
        return None
    return stock / flow * days


def _fmt_shares(value: float | None) -> str:
    if value is None:
        return "—"
    millions = value / 1_000_000
    if abs(millions - round(millions)) < 0.05:
        return f"{round(millions):,}M 股"
    return f"{millions:,.1f}M 股"


def _fmt_days(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}天"


def _share_change(current: float | None, base: float | None) -> dict[str, str]:
    if current is None or not base:
        return dict(_NA)
    pct = (current - base) / abs(base)
    # 股本变动通常不到 1%，百分比会写成 0.0%。改用百万股差额，同比和环比同一单位。
    if abs(pct) < 0.01:
        delta = (current - base) / 1_000_000
        if abs(delta) < 0.05:
            return {"text": "0", "cls": "flat"}
        cls = "up" if delta > 0 else "down"
        shown = round(delta)
        if abs(delta - shown) < 0.05:
            return {"text": f"{shown:+.0f}M 股", "cls": cls}
        return {"text": f"{delta:+.1f}M 股", "cls": cls}
    return _change(pct)


def _day_change(current: float | None, base: float | None) -> dict[str, str]:
    if current is None or base is None:
        return dict(_NA)
    delta = current - base
    cls = "up" if delta > 0 else "down" if delta < 0 else "flat"
    return {"text": f"{delta:+.1f}天", "cls": cls}


def _structure_driver(description: str, reasoning: str, direction: str = "") -> dict[str, Any]:
    return {
        "type": "结构",
        "direction": direction,
        "is_inference": True,
        "source": "press_release",
        "description": description,
        "evidence_quote": "",
        "reasoning": reasoning,
    }


def _sum_columns(hits: list[list[float | None]], count: int) -> list[float | None] | None:
    if not hits:
        return None
    totals = [0.0] * count
    seen = [False] * count
    for nums in hits:
        for index, value in enumerate(nums[:count]):
            if value is not None:
                totals[index] += value
                seen[index] = True
    return [totals[index] if seen[index] else None for index in range(count)]


_XBRL_ACTIVITY: dict[str, dict[str, Any]] = {}


def _xbrl_activity(ticker: str) -> dict[str, Any]:
    key = (ticker or "").upper()
    if key in _XBRL_ACTIVITY:
        return _XBRL_ACTIVITY[key]
    path = data_dir(key) / "xbrl_activity.json"
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if isinstance(loaded, dict):
            data = loaded
    _XBRL_ACTIVITY[key] = data
    return data


def _load_period_doc(ticker: str, period: str, docs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    found = docs.get(period)
    if isinstance(found, dict) and found:
        return found
    path = data_dir(ticker) / f"{period}.json"
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _xbrl_level(rows: Any, end: date, *, quarterly: bool) -> float | None:
    if not isinstance(rows, list):
        return None
    hits: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("value") is None:
            continue
        stop = _as_date(row.get("end"))
        if stop != end:
            continue
        start = _as_date(row.get("start")) if row.get("start") else None
        if quarterly:
            if start is None or not (80 <= (stop - start).days <= 100):
                continue
        elif start is not None:
            continue
        hits.append(row)
    if not hits:
        return None
    best = max(hits, key=lambda row: str(row.get("filed") or ""))
    return _finite(best.get("value"))


def _fill_activity_gaps(
    ticker: str,
    period: str,
    docs: dict[str, dict[str, Any]],
    facts: dict[str, Any],
) -> None:
    """新闻稿对比列没有上季或去年同季时，用该季新闻稿或已保存的 10-Q XBRL。"""
    if not ticker or not period:
        return
    activity = _xbrl_activity(ticker)
    share_rows = activity.get("diluted_shares")
    ar_rows = activity.get("accounts_receivable")
    for kind, other_period in (
        ("qoq", prior_fiscal_period(period)),
        ("yoy", yoy_fiscal_period(period)),
    ):
        other = _load_period_doc(ticker, other_period, docs)
        end = _as_date((other.get("meta") or {}).get("period_end"))
        other_facts = (
            _statement_facts(ticker, other_period, other, docs, fill_gaps=False)
            if other and _press_lines(ticker, other_period)
            else {}
        )
        if facts.get(f"shares_{kind}") is None:
            value = other_facts.get("shares")
            source = "press" if value is not None else ""
            if value is None and end is not None:
                value = _xbrl_level(share_rows, end, quarterly=True)
                source = "xbrl" if value is not None else ""
            if value is not None:
                facts[f"shares_{kind}"] = value
                facts[f"shares_{kind}_external"] = source
        if facts.get(f"ar_{kind}") is None:
            value = other_facts.get("ar")
            source = "press" if value is not None else ""
            if value is None and end is not None:
                value = _xbrl_level(ar_rows, end, quarterly=False)
                source = "xbrl" if value is not None else ""
            if value is not None:
                facts[f"ar_{kind}"] = value
                facts[f"ar_{kind}_external"] = source
                facts[f"ar_{kind}_doc"] = other


def _external_dso(facts: dict[str, Any], kind: str, flow_fallback: float | None, days: int | None) -> float | None:
    """对比列来自另一季时，用那一季自己的营收和天数，不用本季天数重算。"""
    other = facts.get(f"ar_{kind}_doc") if facts.get(f"ar_{kind}_external") else None
    if isinstance(other, dict) and other:
        flow = _finite(((other.get("financials") or {}).get("revenue") or {}).get("value"))
        other_days = _period_day_count(other)
        got = _turnover_days(facts.get(f"ar_{kind}"), flow, other_days)
        if got is not None:
            return got
    return _turnover_days(facts.get(f"ar_{kind}"), flow_fallback, days)


def _statement_facts(
    ticker: str,
    period: str,
    doc: dict[str, Any],
    docs: dict[str, dict[str, Any]],
    *,
    fill_gaps: bool = True,
) -> dict[str, Any]:
    """从已保存的新闻稿读股本、应收、存货、预收或合同负债。不重跑抽取。"""
    empty: dict[str, Any] = {"has_release": False}
    lines = _press_lines(ticker, period)
    if not lines:
        return empty
    income = _section(
        lines,
        lambda line: "STATEMENTS OF INCOME" in line or "STATEMENTS OF OPERATIONS" in line,
        lambda line: "BALANCE SHEET" in line,
    )
    balance = _section(
        lines,
        lambda line: "BALANCE SHEET" in line,
        lambda line: "CASH FLOW" in line,
    )
    facts: dict[str, Any] = {"has_release": True, "saw_balance_sheet": balance is not None}
    if income:
        start, end = income
        revenue_at = next((index for index in range(start, end) if lines[index] == "Revenue"), end)
        dates = _header_dates(lines[start:revenue_at])
        roles = _column_roles(dates, period, doc, docs)
        width = max(len(dates), 1)
        share_at = next(
            (
                index
                for index in range(start, end)
                if "weighted-average shares" in lines[index].lower()
                or "weighted average shares" in lines[index].lower()
                or "shares used in per share" in lines[index].lower()
            ),
            None,
        )
        if share_at is not None:
            diluted = []
            basic = []
            for index in range(share_at, min(end, share_at + 24)):
                label = lines[index].lower()
                if label not in {"diluted", "basic"}:
                    continue
                nums = _take_numbers(lines, index + 1, width)
                if not nums or nums[0] is None or nums[0] < 50:
                    continue
                if label == "diluted" and not diluted:
                    diluted = nums
                elif label == "basic" and not basic:
                    basic = nums
            if diluted:
                facts["shares"] = _pick_role(diluted, roles, "current") 
                facts["shares_qoq"] = _pick_role(diluted, roles, "qoq")
                facts["shares_yoy"] = _pick_role(diluted, roles, "yoy")
                if facts["shares"] is not None:
                    facts["shares"] *= 1_000_000
                if facts["shares_qoq"] is not None:
                    facts["shares_qoq"] *= 1_000_000
                if facts["shares_yoy"] is not None:
                    facts["shares_yoy"] *= 1_000_000
            if basic and basic[0] is not None and basic[0] >= 50:
                facts["basic_shares"] = basic[0] * 1_000_000
    if balance:
        start, end = balance
        asset_at = next((index for index in range(start, end) if lines[index] == "Assets"), start + 8)
        dates = _header_dates(lines[start:asset_at])
        roles = _column_roles(dates, period, doc, docs)
        width = max(len(dates), 1)

        def rows_matching(pred) -> list[float | None] | None:
            hits = []
            for index in range(start, end):
                if not pred(lines[index]):
                    continue
                nums = _take_numbers(lines, index + 1, width)
                if nums:
                    hits.append(nums)
            return _sum_columns(hits, width)

        def is_receivable(label: str) -> bool:
            low = label.lower()
            return "receivable" in low and "allowance" not in low and len(label) < 80

        def is_inventory(label: str) -> bool:
            low = label.lower().rstrip(":")
            return low in {"inventories", "inventory"} or low.startswith("inventories")

        def is_deferred(label: str) -> bool:
            low = label.lower()
            return "deferred revenue" in low or "unearned revenue" in low

        def is_contract(label: str) -> bool:
            return "contract liabilit" in label.lower()

        receivable = rows_matching(is_receivable)
        inventory = rows_matching(is_inventory)
        deferred = rows_matching(is_deferred)
        contract = rows_matching(is_contract)
        for key, nums in (
            ("ar", receivable),
            ("inventory", inventory),
            ("deferred", deferred),
            ("contract", contract),
        ):
            if nums is None:
                continue
            current = _pick_role(nums, roles, "current")
            qoq = _pick_role(nums, roles, "qoq")
            yoy = _pick_role(nums, roles, "yoy")
            facts[key] = None if current is None else current * 1_000_000
            facts[f"{key}_qoq"] = None if qoq is None else qoq * 1_000_000
            facts[f"{key}_yoy"] = None if yoy is None else yoy * 1_000_000
        facts["has_inventory_line"] = inventory is not None
        facts["has_deferred_line"] = deferred is not None
        facts["has_contract_line"] = contract is not None
        facts["has_receivable_line"] = receivable is not None

    if fill_gaps:
        _fill_activity_gaps(ticker, period, docs, facts)

    days = _period_day_count(doc)
    revenue, revenue_yoy, revenue_qoq = _flow_levels(doc, "revenue")
    margin, margin_yoy, margin_qoq = _flow_levels(doc, "gross_margin_gaap", ratio=True)
    if margin is None:
        margin, margin_yoy, margin_qoq = _flow_levels(doc, "gross_margin_nongaap", ratio=True)
    if margin is None:
        margin, margin_yoy, margin_qoq = _flow_levels(doc, "gross_margin", ratio=True)

    def cogs(flow: float | None, rate: float | None) -> float | None:
        if flow is None or rate is None:
            return None
        return flow * (1 - rate)

    facts["days"] = days
    facts["revenue"] = revenue
    facts["revenue_yoy"] = revenue_yoy
    facts["revenue_qoq"] = revenue_qoq
    for kind, stock_key, flow_now, flow_qoq, flow_yoy in (
        ("dio", "inventory", cogs(revenue, margin), cogs(revenue_qoq, margin_qoq), cogs(revenue_yoy, margin_yoy)),
    ):
        facts[kind] = _turnover_days(facts.get(stock_key), flow_now, days)
        facts[f"{kind}_qoq"] = _turnover_days(facts.get(f"{stock_key}_qoq"), flow_qoq, days)
        facts[f"{kind}_yoy"] = _turnover_days(facts.get(f"{stock_key}_yoy"), flow_yoy, days)
    facts["dso"] = _turnover_days(facts.get("ar"), revenue, days)
    facts["dso_qoq"] = _external_dso(facts, "qoq", revenue_qoq, days)
    facts["dso_yoy"] = _external_dso(facts, "yoy", revenue_yoy, days)
    prepay_key = "deferred" if facts.get("has_deferred_line") else "contract" if facts.get("has_contract_line") else None
    facts["prepay_key"] = prepay_key
    if prepay_key:
        facts["prepay"] = _turnover_days(facts.get(prepay_key), revenue, days)
        facts["prepay_qoq"] = _turnover_days(facts.get(f"{prepay_key}_qoq"), revenue_qoq, days)
        facts["prepay_yoy"] = _turnover_days(facts.get(f"{prepay_key}_yoy"), revenue_yoy, days)
    return facts


def _pace_sentence(stock_now: float | None, stock_then: float | None, flow_now: float | None, flow_then: float | None, stock_name: str, flow_name: str) -> str:
    if not stock_now or not stock_then or not flow_now or not flow_then:
        return ""
    stock_pace = stock_now / abs(stock_then) - 1
    flow_pace = flow_now / abs(flow_then) - 1
    stock_text = _change(stock_pace)["text"]
    flow_text = _change(flow_pace)["text"]
    if stock_pace > flow_pace + 0.05:
        return f"{stock_name}同比 {stock_text}，快于{flow_name}同比 {flow_text}，周转天数变长。"
    if flow_pace > stock_pace + 0.05:
        return f"{flow_name}同比 {flow_text}，快于{stock_name}同比 {stock_text}，周转天数变短。"
    return f"{stock_name}和{flow_name}同比接近，周转天数变化不大。"


def _activity_row(
    key: str,
    label: str,
    value: str,
    yoy: dict[str, str],
    qoq: dict[str, str],
    summary: str,
    drivers: list[dict[str, Any]],
    *,
    from_press: bool | None = None,
) -> dict[str, Any]:
    if from_press is None:
        from_press = yoy.get("text") not in {None, "—"} or qoq.get("text") not in {None, "—"}
    return {
        "key": key,
        "label": label,
        "value": value,
        "yoy": yoy,
        "qoq": qoq,
        "vs": dict(_NA),
        "vs_source": "",
        "summary": summary,
        "drivers": drivers,
        "conflicts": [],
        "from_press": from_press,
        "nested": False,
    }


def _activity_rows(
    ticker: str,
    period: str,
    doc: dict[str, Any],
    docs: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    facts = _statement_facts(ticker, period, doc, docs)
    if not facts.get("has_release"):
        return []
    rows = []
    shares = facts.get("shares")
    basic = facts.get("basic_shares")
    if shares:
        gap = None if basic is None else shares - basic
        gap_text = "" if gap is None else f"比基本股多 {gap / 1_000_000:.0f}M，差额是潜在稀释。"
        pieces = []
        if facts.get("shares_qoq_external") == "xbrl":
            pieces.append("环比")
        if facts.get("shares_yoy_external") == "xbrl":
            pieces.append("同比")
        compare = f"{'、'.join(pieces)}用对应季度 10-Q 的稀释加权平均股。" if pieces else ""
        summary = f"稀释加权平均股 {_fmt_shares(shares)}。{gap_text}{compare}".strip()
        description = gap_text or "新闻稿只列了稀释股，没有基本股，潜在稀释看不出来。"
        rows.append(
            _activity_row(
                "share_count",
                "股份数量",
                _fmt_shares(shares),
                _share_change(shares, facts.get("shares_yoy")),
                _share_change(shares, facts.get("shares_qoq")),
                summary,
                [_structure_driver(description, "用利润表稀释加权平均股。股数按百万股换回实际股数，不用每股收益那一行。")],
                from_press=not pieces and (
                    facts.get("shares_yoy") is not None or facts.get("shares_qoq") is not None
                ),
            )
        )
    else:
        rows.append(
            _activity_row(
                "share_count",
                "股份数量",
                "未披露",
                dict(_NA),
                dict(_NA),
                "新闻稿里没有稀释加权平均股。",
                [_structure_driver("没有读到 Weighted-average shares 或 shares used in per share calculations。", "不拿每股收益冒充股数。")],
            )
        )

    if facts.get("has_inventory_line") and facts.get("dio") is not None:
        cogs_now = facts["inventory"] / facts["dio"] * facts["days"] if facts.get("days") else None
        pace = _pace_sentence(
            facts.get("inventory"),
            facts.get("inventory_yoy"),
            cogs_now,
            (facts["inventory_yoy"] / facts["dio_yoy"] * facts["days"]) if facts.get("inventory_yoy") and facts.get("dio_yoy") and facts.get("days") else None,
            "存货",
            "销售成本",
        )
        rows.append(
            _activity_row(
                "dio_days",
                "存货周转天数",
                _fmt_days(facts["dio"]),
                _day_change(facts.get("dio"), facts.get("dio_yoy")),
                _day_change(facts.get("dio"), facts.get("dio_qoq")),
                f"期末存货 {_fmt_money(facts.get('inventory'))}，相当于本期销售成本 {facts['dio']:.1f} 天。{pace}",
                [_structure_driver(pace or "用期末存货除以本期销售成本，再乘本季天数。", "期末余额，不是期初期末平均。销售成本用营收 ×（1 − 毛利率）。", "+" if (facts.get("dio_yoy") is not None and facts["dio"] > facts["dio_yoy"]) else "-" if facts.get("dio_yoy") is not None and facts["dio"] < facts["dio_yoy"] else "")],
            )
        )
    else:
        rows.append(
            _activity_row(
                "dio_days",
                "存货周转天数",
                "未披露",
                dict(_NA),
                dict(_NA),
                "资产负债表没有存货科目。",
                [_structure_driver("本季资产负债表没有 Inventories 行，周转天数不算。", "不把其他流动资产当成存货。")],
            )
        )

    if facts.get("has_receivable_line") and facts.get("dso") is not None:
        pace = _pace_sentence(facts.get("ar"), facts.get("ar_yoy"), facts.get("revenue"), facts.get("revenue_yoy"), "应收账款", "营收")
        rows.append(
            _activity_row(
                "dso_days",
                "应收账款周转天数",
                _fmt_days(facts["dso"]),
                _day_change(facts.get("dso"), facts.get("dso_yoy")),
                _day_change(facts.get("dso"), facts.get("dso_qoq")),
                f"期末应收账款 {_fmt_money(facts.get('ar'))}，相当于本季营收 {facts['dso']:.1f} 天。{pace}"
                + (
                    "上季和去年同季的应收取自对应季度 10-Q 的期末余额。"
                    if facts.get("ar_yoy_external") == "xbrl" or facts.get("ar_qoq_external") == "xbrl"
                    else ""
                    if facts.get("ar_yoy") or facts.get("ar_qoq")
                    else "资产负债表没有上季或去年同季的应收列。"
                ),
                [_structure_driver(pace or "只有期末应收，天数变动不算。", "期末应收账款 ÷ 本季营收 × 本季天数，不是平均余额。")],
                from_press=not (
                    facts.get("ar_yoy_external") == "xbrl" or facts.get("ar_qoq_external") == "xbrl"
                )
                and (facts.get("ar_yoy") is not None or facts.get("ar_qoq") is not None),
            )
        )
    else:
        rows.append(
            _activity_row(
                "dso_days",
                "应收账款周转天数",
                "未披露",
                dict(_NA),
                dict(_NA),
                "资产负债表没有应收账款科目。",
                [_structure_driver("没有读到应收账款行。", "不把其他应收或预付当成应收账款。")],
            )
        )

    if facts.get("prepay_key") == "deferred" and facts.get("prepay") is not None:
        label = "预收账款周转天数"
        stock_name = "预收账款"
        stock = facts.get("deferred")
        stock_yoy = facts.get("deferred_yoy")
    elif facts.get("prepay_key") == "contract" and facts.get("prepay") is not None:
        label = "合同负债周转天数"
        stock_name = "合同负债"
        stock = facts.get("contract")
        stock_yoy = facts.get("contract_yoy")
    else:
        label = ""
    if label:
        pace = _pace_sentence(stock, stock_yoy, facts.get("revenue"), facts.get("revenue_yoy"), stock_name, "营收")
        rows.append(
            _activity_row(
                "deferred_days",
                label,
                _fmt_days(facts["prepay"]),
                _day_change(facts.get("prepay"), facts.get("prepay_yoy")),
                _day_change(facts.get("prepay"), facts.get("prepay_qoq")),
                f"期末{stock_name} {_fmt_money(stock)}，相当于本季营收 {facts['prepay']:.1f} 天。{pace}",
                [_structure_driver(pace or f"用期末{stock_name}除以本季营收。", "没有预收账款时改用合同负债。期末余额，不是平均余额。")],
            )
        )
    else:
        rows.append(
            _activity_row(
                "deferred_days",
                "预收账款周转天数",
                "未披露",
                dict(_NA),
                dict(_NA),
                "资产负债表没有预收账款，也没有合同负债。",
                [_structure_driver("Deferred revenue 和 contract liabilities 都没有。", "不把递延所得税或租赁负债当成预收。")],
            )
        )
    return rows


def _fill_statement_levels(points: dict[str, dict[str, Any]], docs: dict[str, dict[str, Any]]) -> None:
    ticker = ""
    for doc in docs.values():
        ticker = (doc.get("meta") or {}).get("ticker") or ""
        if ticker:
            break
    if not ticker:
        return
    for period, doc in docs.items():
        point = points.get(period)
        if point is None:
            continue
        facts = _statement_facts(ticker, period, doc, docs)
        if not facts.get("has_release"):
            continue
        if facts.get("shares") is not None:
            point["share_count"] = facts["shares"]
        if facts.get("dso") is not None:
            point["dso_days"] = facts["dso"]
        if facts.get("dio") is not None:
            point["dio_days"] = facts["dio"]
        if facts.get("prepay") is not None:
            point["deferred_days"] = facts["prepay"]


_SEGMENT_NAMES = {
    "advertising": "广告",
    "other revenue": "其他收入",
    "family of apps": "应用家族",
    "reality labs": "Reality Labs",
    "cloud memory": "云存储",
    "core data center": "核心数据中心",
    "mobile and client": "移动与客户端",
    "automotive and embedded": "汽车与嵌入式",
}


def _segment_name(raw: str) -> str:
    key = re.sub(r"\s+business unit$", "", raw.strip(), flags=re.I).lower()
    return _SEGMENT_NAMES.get(key, raw.strip())


def _segment_roles_from_fq(lines: list[str], period: str) -> dict[int, str]:
    periods = []
    for line in lines:
        matched = re.fullmatch(r"FQ([1-4])-(\d{2})", line)
        if not matched:
            continue
        year = 2000 + int(matched.group(2))
        periods.append(f"FY{year}Q{matched.group(1)}")
    roles: dict[int, str] = {}
    for index, key in enumerate(periods):
        if key == period:
            roles[index] = "current"
        elif key == prior_fiscal_period(period):
            roles[index] = "qoq"
        elif key == yoy_fiscal_period(period):
            roles[index] = "yoy"
    if periods and "current" not in roles.values():
        roles[0] = "current"
    return roles


def _business_split(ticker: str, period: str, doc: dict[str, Any], docs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    geo_note = "本季新闻稿没有按地区列示收入。"
    blank = {"business": [], "geo": [], "note": "", "business_note": "本季没有保存新闻稿，分部拆不开。", "geo_note": geo_note}
    lines = _press_lines(ticker, period)
    if not lines:
        return blank
    unit_at = next((index for index, line in enumerate(lines) if "Business Unit Financial Results" in line), None)
    segment_at = next((index for index, line in enumerate(lines) if line == "Segment Information"), None)
    rows: list[dict[str, Any]] = []
    company_total = None
    if unit_at is not None:
        end = next((index for index in range(unit_at + 1, len(lines)) if lines[index] == "Business Outlook"), len(lines))
        roles = _segment_roles_from_fq(lines[unit_at:unit_at + 12], period)
        width = max(roles.keys(), default=0) + 1
        index = unit_at
        while index < end:
            if lines[index].lower().endswith("business unit"):
                name = lines[index]
                revenue_at = next((cursor for cursor in range(index + 1, min(end, index + 8)) if lines[cursor] == "Revenue"), None)
                margin_at = next((cursor for cursor in range(index + 1, min(end, index + 20)) if lines[cursor] == "Operating margin"), None)
                revenue_nums = _take_numbers(lines, revenue_at + 1, width) if revenue_at is not None else []
                margin_nums = _take_numbers(lines, margin_at + 1, width) if margin_at is not None else []
                current = _pick_role(revenue_nums, roles, "current")
                if current is not None:
                    rows.append(
                        {
                            "name": _segment_name(name),
                            "current": current * 1_000_000,
                            "qoq": None if _pick_role(revenue_nums, roles, "qoq") is None else _pick_role(revenue_nums, roles, "qoq") * 1_000_000,
                            "yoy": None if _pick_role(revenue_nums, roles, "yoy") is None else _pick_role(revenue_nums, roles, "yoy") * 1_000_000,
                            "opinc": None,
                            "margin": _pick_role(margin_nums, roles, "current"),
                            "children": [],
                        }
                    )
            index += 1
    elif segment_at is not None:
        end = next(
            (
                index
                for index in range(segment_at + 1, len(lines))
                if lines[index].startswith("Reconciliation") or lines[index] == "Business Outlook" or "GAAP advertising" in lines[index]
            ),
            len(lines),
        )
        header_end = next((index for index in range(segment_at, end) if lines[index] in {"Revenue:", "Revenue"}), segment_at + 12)
        roles = _column_roles(_header_dates(lines[segment_at:header_end]), period, doc, docs)
        width = max(len(_header_dates(lines[segment_at:header_end])), 1)

        def read_block(begin_label: str, stop_prefixes: tuple[str, ...]) -> list[dict[str, Any]]:
            begin = next((index for index in range(segment_at, end) if lines[index] == begin_label), None)
            if begin is None:
                return []
            found = []
            index = begin + 1
            while index < end:
                line = lines[index]
                low = line.lower()
                if any(low.startswith(prefix) for prefix in stop_prefixes):
                    break
                if re.search(r"[A-Za-z]", line) and not re.fullmatch(r"\d{4}", line) and len(line) < 60 and line not in {"$", "%"}:
                    nums = _take_numbers(lines, index + 1, width)
                    current = _pick_role(nums, roles, "current")
                    if current is not None and not low.startswith("total"):
                        found.append(
                            {
                                "name": _segment_name(line),
                                "raw": line,
                                "current": current * 1_000_000,
                                "qoq": None if _pick_role(nums, roles, "qoq") is None else _pick_role(nums, roles, "qoq") * 1_000_000,
                                "yoy": None if _pick_role(nums, roles, "yoy") is None else _pick_role(nums, roles, "yoy") * 1_000_000,
                            }
                        )
                index += 1
            return found

        revenue_rows = read_block("Revenue:", ("income", "total revenue"))
        # Total revenue is a stop, so read it separately for the company total.
        total = None
        total_at = next((index for index in range(segment_at, end) if lines[index] == "Total revenue"), None)
        if total_at is not None:
            total_nums = _take_numbers(lines, total_at + 1, width)
            total_current = _pick_role(total_nums, roles, "current")
            if total_current is not None:
                total = total_current * 1_000_000
        opinc_rows = read_block("Income (loss) from operations:", ("total", "reconciliation"))
        opinc_by_name = {row["raw"].lower(): row["current"] for row in opinc_rows}
        pending: list[dict[str, Any]] = []
        for row in revenue_rows:
            if pending and abs(row["current"] - sum(item["current"] for item in pending)) <= 2_000_000:
                row["children"] = pending
                row["opinc"] = opinc_by_name.get(row["raw"].lower())
                pending = []
                rows.append(row)
            else:
                row["children"] = []
                row["opinc"] = opinc_by_name.get(row["raw"].lower())
                pending.append(row)
        for row in pending:
            rows.append(row)
        company_total = total
    else:
        company_total = None
    if not rows:
        return {
            "business": [],
            "geo": [],
            "note": "",
            "business_note": "本季新闻稿没有按业务列示收入。",
            "geo_note": geo_note,
        }
    total_revenue = company_total or sum(row["current"] for row in rows)
    note_bits = []
    leaves = []
    for row in rows:
        if row.get("children"):
            leaves.extend(row["children"])
        else:
            leaves.append(row)
    top = max(leaves or rows, key=lambda item: item["current"])
    if total_revenue:
        note_bits.append(f"{top['name']}占营收 {top['current'] / total_revenue * 100:.1f}%。")
    profits = [row for row in rows if row.get("opinc") is not None and row["opinc"] > 0]
    losses = [row for row in rows if row.get("opinc") is not None and row["opinc"] < 0]
    if profits and losses:
        note_bits.append(
            f"{profits[0]['name']}营业利润 {_fmt_money(profits[0]['opinc'])}，{losses[0]['name']} 营业亏损 {_fmt_money(abs(losses[0]['opinc']))}。"
        )
    elif any(row.get("margin") is not None for row in rows):
        richest = max(rows, key=lambda item: item.get("margin") or 0)
        company_rev = _finite(((doc.get("financials") or {}).get("revenue") or {}).get("value"))
        pieces = sum(row["current"] for row in rows)
        tied = company_rev and abs(pieces - company_rev) / abs(company_rev) < 0.01
        note_bits.append(
            f"{richest['name']}营业利润率 {richest['margin']:.0f}%。"
            + ("各业务收入相加等于公司营收。" if tied else "")
        )

    def pack(row: dict[str, Any]) -> dict[str, Any]:
        extra = ""
        if row.get("opinc") is not None:
            extra = f"营业利润 {_fmt_money(row['opinc'])}"
        elif row.get("margin") is not None:
            extra = f"营业利润率 {row['margin']:.0f}%"
        return {
            "name": row["name"],
            "revenue": _fmt_money(row["current"]),
            "share": f"{row['current'] / total_revenue * 100:.1f}%" if total_revenue else "",
            "yoy": _change((row["current"] - row["yoy"]) / abs(row["yoy"]) if row.get("yoy") else None),
            "qoq": _change((row["current"] - row["qoq"]) / abs(row["qoq"]) if row.get("qoq") else None),
            "extra": extra,
            "children": [pack(child) for child in row.get("children") or []],
        }

    return {
        "business": [pack(row) for row in rows],
        "geo": [],
        "note": "".join(note_bits),
        "business_note": "",
        "geo_note": geo_note,
    }


def _mix_period_key(label: str, grain: str) -> tuple[int, int]:
    if grain == "year":
        return int(label[2:6]), 0
    year, quarter = parse_fiscal_period(label)
    return year, quarter


def _mix_span(start: str, end: str, grain: str) -> list[str]:
    if grain == "year":
        return [f"FY{year}" for year in range(int(start[2:6]), int(end[2:6]) + 1)]
    year, quarter = parse_fiscal_period(start)
    labels: list[str] = []
    while True:
        label = make_fiscal_period(year, quarter)
        labels.append(label)
        if label == end:
            return labels
        quarter += 1
        if quarter == 5:
            year += 1
            quarter = 1


def _mix_prior(label: str, grain: str, kind: str) -> str | None:
    if grain == "year":
        if kind == "qoq":
            return None
        return f"FY{int(label[2:6]) - 1}"
    return yoy_fiscal_period(label) if kind == "yoy" else prior_fiscal_period(label)


def _raw_stack_keys(raw: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for grain in ("quarters", "years"):
        for row in raw.get(grain) or []:
            for line in row.get("lines") or []:
                if not line.get("stack"):
                    continue
                key = str(line.get("key") or "")
                if key and key not in keys:
                    keys.append(key)
    return keys


def _meta_family_stack(keys: list[str]) -> bool:
    """广告 / 其他 / Reality Labs 这一套堆叠。用来保留 META 原来的注释，不按股票代码写死。"""
    return bool(keys) and set(keys) <= {"ads", "other", "rl"} and "ads" in keys


# 构建期只读缓存。键带文件戳，数据文件变了会重算；同一轮建站里按股票命中。
_MEMO: dict[tuple, Any] = {}


def _file_stamp(path: Path) -> tuple[str, int | None, int | None]:
    try:
        stat = path.stat()
    except OSError:
        return (str(path), None, None)
    return (str(path), stat.st_mtime_ns, stat.st_size)


def _memo(kind: str, ident: str, stamp: tuple, factory):
    key = (kind, ident, stamp)
    if key not in _MEMO:
        _MEMO[key] = factory()
    return _MEMO[key]


def _cached_json(path: Path) -> dict[str, Any] | None:
    """同一路径、同一 mtime 只解析一次。文件不存在时记空，不反复 stat 失败。"""
    stamp = _file_stamp(path)

    def load() -> dict[str, Any] | None:
        if stamp[1] is None:
            return None
        return _read_json(path)

    return _memo("json", str(path), stamp, load)


def _series_inputs(ticker: str) -> tuple:
    """图表和营业成本会读到的文件。戳不变就复用已经算好的序列。"""
    folder = data_dir(ticker)
    paths = [folder / "statement_history.json", folder / "segment_history.json"]
    paths.extend(sorted(folder.glob("FY*.json")))
    raw = folder / "raw"
    if raw.is_dir():
        paths.extend(sorted(raw.glob("*/extracted_financials.json")))
    return tuple(_file_stamp(path) for path in paths)


def _segment_note(raw: dict[str, Any], quarters: list[dict[str, Any]]) -> str:
    """分部注释与财季无关。META 那套堆叠保留原句，其他公司用文件里的来源。"""
    gaps = [row["period"] for row in quarters if row["gap"]]
    methods = {row.get("method") for row in (raw.get("quarters") or [])}
    if _meta_family_stack(_raw_stack_keys(raw)):
        note_bits = ["占比按披露的分部收入。柱是广告、其他、Reality Labs；有母项时母项不重复计入。"]
        if quarters:
            note_bits.append(f"最早一期是 {quarters[0]['period']}。")
        if "table" in methods:
            note_bits.append("部分季度来自 10-Q 收入构成表。")
        if "residual" in methods:
            note_bits.append("没有单季事实的第四季，用全年减去已披露季度；差额不完整则留空。")
        if "xbrl" in methods:
            note_bits.append("单季数来自 10-Q/10-K XBRL。")
        if gaps:
            note_bits.append(f"{'、'.join(gaps)} 没有完整分部，留空。")
        note_bits.append("来源 SEC EDGAR。")
    else:
        note_bits = ["占比按披露的分部收入。只计入文字里能加总的叶子项。"]
        if quarters:
            note_bits.append(f"最早一期是 {quarters[0]['period']}。")
        if gaps:
            note_bits.append(f"{'、'.join(gaps)} 没有完整分部，留空。")
        source = str(raw.get("source") or "").strip()
        if source:
            note_bits.append(source)
    return "".join(note_bits)


def _segment_base(ticker: str) -> dict[str, Any]:
    """分部占比序列与财季无关。本季选中的柱在 _segment_mix 里填。"""
    empty = {"quarters": [], "years": [], "note": ""}
    path = data_dir(ticker) / "segment_history.json"

    def build() -> dict[str, Any]:
        raw = _cached_json(path) or {}
        if not raw:
            return dict(empty)
        quarters = _mix_points(raw.get("quarters") or [], "quarter")
        years = _mix_points(raw.get("years") or [], "year")
        if not quarters and not years:
            return dict(empty)
        return {"quarters": quarters, "years": years, "note": _segment_note(raw, quarters)}

    return _memo("segment", str(path), _file_stamp(path), build)


def _segment_mix(ticker: str, period: str) -> dict[str, Any]:
    """历史分部占比。没有 segment_history.json 的公司返回空，页面只留本季五列表。"""
    base = _segment_base(ticker)
    quarters = base["quarters"]
    years = base["years"]
    if not quarters and not years:
        return {
            "quarters": [],
            "years": [],
            "note": "",
            "default_period": "",
            "selected": None,
        }
    present = [row["period"] for row in quarters if not row["gap"]]
    default = period if period in present else (present[-1] if present else "")
    selected = next((row for row in quarters if row["period"] == default), None)
    return {
        "quarters": quarters,
        "years": years,
        "note": base["note"],
        "default_period": default,
        "selected": selected,
    }


def _mix_points(rows: list[dict[str, Any]], grain: str) -> list[dict[str, Any]]:
    labeled = [row for row in rows if row.get("period")]
    if not labeled:
        return []
    labeled.sort(key=lambda row: _mix_period_key(str(row["period"]), grain))
    usable = [row for row in labeled if row.get("lines")]
    by_period = {str(row["period"]): row for row in labeled}
    amounts: dict[tuple[str, str], float] = {}
    stack_keys: list[str] = []
    for row in usable:
        for line in row["lines"]:
            amounts[(str(row["period"]), str(line["key"]))] = float(line["value"])
            if line.get("stack"):
                key = str(line["key"])
                if key not in stack_keys:
                    stack_keys.append(key)

    def pace(label: str, key: str, kind: str) -> dict[str, str]:
        prior = _mix_prior(label, grain, kind)
        if prior is None or (prior, key) not in amounts:
            return {"text": "—", "cls": "na"}
        base = amounts[(prior, key)]
        if base == 0:
            return {"text": "—", "cls": "na"}
        current = amounts[(label, key)]
        return _change((current - base) / abs(base))

    points: list[dict[str, Any]] = []
    for label in _mix_span(str(labeled[0]["period"]), str(labeled[-1]["period"]), grain):
        row = by_period.get(label)
        if row is None or not row.get("lines"):
            points.append(
                {
                    "period": label,
                    "gap": True,
                    "method": "",
                    "rows": [],
                    "series": {key: None for key in stack_keys},
                }
            )
            continue
        lines = row["lines"]
        company = sum(float(line["value"]) for line in lines if int(line.get("depth") or 0) == 0)
        stacked = sum(float(line["value"]) for line in lines if line.get("stack"))
        if company <= 0:
            company = stacked
        packed = []
        series = {key: None for key in stack_keys}
        for line in lines:
            value = float(line["value"])
            key = str(line["key"])
            share = value / company * 100 if company else None
            if line.get("stack") and key in series and stacked:
                series[key] = round(value / stacked * 100, 1)
            packed.append(
                {
                    "key": key,
                    "name": line["name"],
                    "depth": int(line.get("depth") or 0),
                    "stack": bool(line.get("stack")),
                    "revenue": _fmt_money(value),
                    "value": value,
                    "share": f"{share:.1f}%" if share is not None else "—",
                    "yoy": pace(label, key, "yoy"),
                    "qoq": pace(label, key, "qoq"),
                }
            )
        points.append(
            {
                "period": label,
                "gap": False,
                "method": row.get("method") or "",
                "rows": packed,
                "series": series,
            }
        )
    return points


def _fin_rows(doc: dict[str, Any], docs: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """财务解读表：每行一个指标，展开后看变动原因与原文引用。"""
    meta = doc.get("meta") or {}
    ticker = meta.get("ticker") or ""
    period = meta.get("fiscal_period") or ""
    if docs is None and ticker:
        docs = _load_docs(ticker)
    docs = docs or {}
    rows = []
    for s in _metric_sections(doc):
        m = s["metric"] or {}
        bench = m.get("benchmark") or {}
        has_bench = bench.get("value") is not None
        drv = s["driver"] or {}
        value = m.get("value")
        yoy_raw = m.get("yoy_pp") if s["is_ratio"] else m.get("yoy_pct")
        summary = drv.get("summary") or m.get("basis_note") or ""
        if s["is_eps"]:
            own_adr = _ADR_USD_RE.search(str(m.get("source_quote") or ""))
            disclosed = float(own_adr.group(1).replace(",", "")) if own_adr else None
            if disclosed is None and s["key"] in {"eps", "eps_gaap"} and (_ntd_only_eps_quote(doc) or m.get("value") is None):
                disclosed = _disclosed_adr_usd(doc)
            if disclosed is not None:
                value = disclosed
                if yoy_raw is None:
                    prior = docs.get(yoy_fiscal_period(period)) if period else None
                    base = _disclosed_adr_usd(prior) if prior else None
                    if base:
                        yoy_raw = disclosed / abs(base) - 1
                if m.get("value") is None or abs(float(m.get("value")) - disclosed) > 0.02:
                    note = "本行按新闻稿 US$ per ADR 显示，不按汇率把新台币换成美元。"
                    summary = f"{summary} {note}".strip()
            elif s["key"] in {"eps", "eps_gaap"} and _ntd_only_eps_quote(doc):
                ntd = _statement_ordinary_ntd(doc)
                if ntd is not None:
                    rows.append(
                        {
                            "key": s["key"],
                            "label": s["label"],
                            "value": f"NT${ntd:.2f}",
                            "yoy": _change(yoy_raw, pp=s["is_ratio"]),
                            "qoq": _change(m.get("qoq_pp") if s["is_ratio"] else m.get("qoq_pct"), pp=s["is_ratio"]),
                            "vs": _change(bench.get("diff_pp") if s["is_ratio"] else bench.get("diff_pct"), pp=s["is_ratio"])
                            if has_bench
                            else {"text": "—", "cls": "na"},
                            "vs_source": _source_label(bench.get("source")) if has_bench else "",
                            "summary": (summary + " 只有普通股新台币，新闻稿没有 US$ per ADR，不换算成美元。").strip(),
                            "drivers": drv.get("drivers") or [],
                            "conflicts": drv.get("conflicts") or [],
                            "from_press": m.get("comparatives_source") == "press_release",
                            "nested": False,
                        }
                    )
                    continue
        rows.append(
            {
                "key": s["key"],
                "label": s["label"],
                "value": _fmt_value(value, s["key"]),
                "yoy": _change(yoy_raw, pp=s["is_ratio"]),
                "qoq": _change(m.get("qoq_pp") if s["is_ratio"] else m.get("qoq_pct"), pp=s["is_ratio"]),
                "vs": _change(bench.get("diff_pp") if s["is_ratio"] else bench.get("diff_pct"), pp=s["is_ratio"])
                if has_bench
                else {"text": "—", "cls": "na"},
                "vs_source": _source_label(bench.get("source")) if has_bench else "",
                "summary": summary,
                "drivers": drv.get("drivers") or [],
                "conflicts": drv.get("conflicts") or [],
                "from_press": m.get("comparatives_source") == "press_release",
                "nested": False,
            }
        )
    rows = _with_cogs_row(rows, ticker, period)
    rows = _with_profit_companions(rows, doc, docs, ticker, period)
    rows = _with_opex_row(rows, ticker, period)
    rows.extend(_activity_rows(ticker, period, doc, docs))
    return rows


def _load_statement(ticker: str) -> dict[str, Any]:
    return _cached_json(data_dir(ticker) / "statement_history.json") or {}


def _with_opex_row(rows: list[dict[str, Any]], ticker: str, period: str) -> list[dict[str, Any]]:
    """营业费用放在营业利润前面。没有费用历史的公司不加这一行。"""
    if any(row["key"] == "opex" for row in rows):
        return rows
    quarters = {row["period"]: row for row in (_load_statement(ticker).get("quarters") or []) if row.get("opex") is not None}
    current = quarters.get(period)
    if not current:
        return rows
    yoy_row = quarters.get(yoy_fiscal_period(period))
    qoq_row = quarters.get(prior_fiscal_period(period))
    names = "、".join(line["name"] for line in current.get("opex_lines") or [])
    made = {
        "key": "opex",
        "label": "营业费用",
        "value": _fmt_money(current["opex"]),
        "yoy": _change(_rel(current["opex"], None if yoy_row is None else yoy_row.get("opex"), False)),
        "qoq": _change(_rel(current["opex"], None if qoq_row is None else qoq_row.get("opex"), False)),
        "vs": {"text": "—", "cls": "na"},
        "vs_source": "",
        "summary": f"{names}合计，不含营业成本。" if names else "不含营业成本。",
        "drivers": [],
        "conflicts": [],
        "from_press": False,
        "nested": False,
    }
    for index, row in enumerate(rows):
        if str(row["key"]).startswith("operating_margin") or str(row["key"]).startswith("operating_income"):
            return rows[:index] + [made] + rows[index:]
    return rows + [made]


def _component_lines(row: dict[str, Any], field: str) -> list[dict[str, Any]]:
    lines = []
    for line in row.get(field) or []:
        if not isinstance(line, dict) or line.get("value") is None or not line.get("name"):
            continue
        lines.append(line)
    return lines


def _has_component_breakdown(quarters: list[dict[str, Any]], field: str) -> bool:
    return any(len(_component_lines(row, field)) >= 2 for row in quarters)


def _cost_total(row: dict[str, Any]) -> float | None:
    if row.get("cost_of_revenue") is not None:
        return float(row["cost_of_revenue"])
    lines = _component_lines(row, "cogs_lines")
    if len(lines) >= 2:
        return sum(float(line["value"]) for line in lines)
    return None


def _cost_geo_lines(row: dict[str, Any]) -> list[dict[str, Any]]:
    return _component_lines(row, "cost_geo") or _component_lines(row, "cogs_geo")


def _quote_slots(ticker: str, period: str, field: str) -> dict[str, float | None]:
    """新闻稿表格上的一个金额：本季、上季、去年同季。列位置跟营收行的版式。"""
    extracted = _read_json(data_dir(ticker) / "raw" / period / "extracted_financials.json") or {}
    item = extracted.get(field)
    if not isinstance(item, dict) or not item.get("raw"):
        return {}
    from pipeline.compute.comparatives import _cells_from_anchor, detect_layout
    from pipeline.extract.numbers import ParsedAmount

    slots: dict[str, float | None] = {
        "value": ParsedAmount.from_dict(item).value,
        "year_ago": None,
        "prior_q": None,
    }
    layout = detect_layout(extracted) or {}
    cells = _cells_from_anchor(item)
    if cells and layout:
        for name, key in (("year_ago", "yoy"), ("prior_q", "qoq")):
            idx = layout.get(key)
            if idx is None or idx >= len(cells):
                continue
            slots[name] = ParsedAmount.from_dict(
                {"raw": repr(cells[idx]), "unit": item.get("unit")}
            ).value
    if slots.get("value") is None:
        return {}
    return slots


def _statement_cost_quarters(ticker: str) -> list[dict[str, Any]]:
    quarters = [row for row in (_load_statement(ticker).get("quarters") or []) if row.get("period")]
    if not any(_cost_total(row) is not None for row in quarters):
        return []
    out = []
    for row in quarters:
        out.append({**row, "cost_of_revenue": _cost_total(row)})
    return out


def _extracted_cost_quarters(ticker: str) -> list[dict[str, Any]]:
    values: dict[str, float] = {}
    for period in _load_docs(ticker):
        slots = _quote_slots(ticker, period, "cost_of_revenue")
        if not slots:
            continue
        if slots.get("value") is not None:
            values[period] = float(slots["value"])
        if slots.get("year_ago") is not None:
            values.setdefault(yoy_fiscal_period(period), float(slots["year_ago"]))
        if slots.get("prior_q") is not None:
            values.setdefault(prior_fiscal_period(period), float(slots["prior_q"]))
    return [{"period": period, "cost_of_revenue": values[period]} for period in sorted(values)]


def _derived_cost_quarters(ticker: str) -> list[dict[str, Any]]:
    """没有申报成本时，用已有的营业收入和 GAAP 毛利率倒算，不另造拆分。"""
    values: dict[str, float] = {}

    def put(period: str, revenue: float | None, margin: float | None, *, overwrite: bool) -> None:
        if revenue is None or margin is None:
            return
        amount = float(revenue) * (1 - float(margin))
        if overwrite:
            values[period] = amount
        else:
            values.setdefault(period, amount)

    for period, doc in _load_docs(ticker).items():
        put(period, _block_num(doc, "revenue"), _block_num(doc, "gross_margin_gaap"), overwrite=True)
        put(
            yoy_fiscal_period(period),
            _block_num(doc, "revenue", "year_ago"),
            _block_num(doc, "gross_margin_gaap", "year_ago"),
            overwrite=False,
        )
        put(
            prior_fiscal_period(period),
            _block_num(doc, "revenue", "prior_q"),
            _block_num(doc, "gross_margin_gaap", "prior_q"),
            overwrite=False,
        )
    return [{"period": period, "cost_of_revenue": values[period]} for period in sorted(values)]


def _cost_series(ticker: str) -> tuple[list[dict[str, Any]], str]:
    """营业成本序列。优先 XBRL / statement_history，其次新闻稿，最后才用毛利倒算。"""
    ident = str(data_dir(ticker))
    return _memo("cost", ident, _series_inputs(ticker), lambda: _cost_series_compute(ticker))


_DERIVED_COST_METHODS = {"derived", "slide", "revenue_minus_gross_profit"}
_PRESS_COST_METHODS = {"press", "press_release"}


def _cost_series_compute(ticker: str) -> tuple[list[dict[str, Any]], str]:
    statement = _statement_cost_quarters(ticker)
    if statement:
        methods = {
            str(row.get("cost_method") or "xbrl")
            for row in statement
            if row.get("cost_of_revenue") is not None
        }
        if methods and methods <= _DERIVED_COST_METHODS:
            return statement, "derived"
        if methods and methods <= _PRESS_COST_METHODS:
            return statement, "press"
        # 新闻稿倒算和幻灯片毛利倒算混在一起时，仍然不是 SEC companyfacts。
        if methods and methods <= _DERIVED_COST_METHODS | _PRESS_COST_METHODS:
            return statement, "derived"
        return statement, "xbrl"
    extracted = _extracted_cost_quarters(ticker)
    if extracted:
        return extracted, "press"
    derived = _derived_cost_quarters(ticker)
    if derived:
        return derived, "derived"
    return [], ""


def _breakdown_quarters(quarters: list[dict[str, Any]], field: str, picker) -> list[dict[str, Any]]:
    prepared = []
    for row in quarters:
        lines = picker(row)
        prepared.append({**row, field: lines if len(lines) >= 2 else []})
    return prepared


def _cost_charts(quarters: list[dict[str, Any]], period: str, subtitle: str = "十亿美元") -> list[dict[str, Any]]:
    """合计 + 同比。有两项以上的成本拆分才加占比；没有地区成本就不画第三张。"""
    charts = [_amount_yoy_chart(quarters, "cost_of_revenue", "营业成本", subtitle)]
    if _has_component_breakdown(quarters, "cogs_lines"):
        charts.append(
            _share_chart(
                _line_points(
                    _breakdown_quarters(quarters, "cogs_lines", lambda row: _component_lines(row, "cogs_lines")),
                    "cogs_lines",
                    denominator="lines",
                ),
                "成本构成",
                "占比",
                period,
            )
        )
    if any(len(_cost_geo_lines(row)) >= 2 for row in quarters):
        charts.append(
            _share_chart(
                _line_points(
                    _breakdown_quarters(quarters, "cost_geo", _cost_geo_lines),
                    "cost_geo",
                    denominator="lines",
                ),
                "按地区",
                "占比",
                period,
            )
        )
    return charts


def _cost_note(quarters: list[dict[str, Any]], source: str) -> str:
    if source == "xbrl":
        bits = ["营业成本来自 SEC companyfacts 的单季 us-gaap。"]
        if any(row.get("cost_method") == "residual" for row in quarters):
            bits.append("第四季没有单季事实时用全年减前九个月。")
    elif source == "press":
        bits = ["营业成本来自新闻稿表格。"]
    else:
        bits = ["营业成本由营业收入减去毛利得到。"]
    if _has_component_breakdown(quarters, "cogs_lines"):
        bits.append("占比以这些成本项合计为分母。")
    else:
        bits.append("没有把营业成本再拆开。")
    if any(len(_cost_geo_lines(row)) >= 2 for row in quarters):
        bits.append("地区成本来自申报的地区轴。")
    return "".join(bits)


def _cogs_summary(current: dict[str, Any], source: str) -> str:
    lines = _component_lines(current, "cogs_lines")
    if len(lines) >= 2:
        return "、".join(str(line["name"]) for line in lines) + "合计。"
    if source == "xbrl":
        return "申报营业成本，没有再拆开。"
    if source == "press":
        return "新闻稿中的营业成本，没有再拆开。"
    return "营业收入减去毛利，没有单独的营业成本科目。"


def _with_cogs_row(rows: list[dict[str, Any]], ticker: str, period: str) -> list[dict[str, Any]]:
    """营业成本放在毛利率上面，替换原来的毛利润行。没有成本数就不加。"""
    if any(row["key"] == "cost_of_revenue" for row in rows):
        return rows
    quarters, source = _cost_series(ticker)
    by_period = {str(row["period"]): row for row in quarters}
    current = by_period.get(period)
    value = None if current is None else current.get("cost_of_revenue")
    if value is None:
        return rows
    yoy_row = by_period.get(yoy_fiscal_period(period)) or {}
    qoq_row = by_period.get(prior_fiscal_period(period)) or {}
    made = {
        "key": "cost_of_revenue",
        "label": "营业成本",
        "value": _fmt_money(value),
        "yoy": _change(_rel(value, yoy_row.get("cost_of_revenue"), False)),
        "qoq": _change(_rel(value, qoq_row.get("cost_of_revenue"), False)),
        "vs": {"text": "—", "cls": "na"},
        "vs_source": "",
        "summary": _cogs_summary(current, source),
        "drivers": [],
        "conflicts": [],
        "from_press": source != "xbrl",
        "nested": False,
    }
    for index, row in enumerate(rows):
        if str(row["key"]).startswith("gross_margin"):
            return rows[:index] + [made] + rows[index:]
    for index, row in enumerate(rows):
        if row["key"] == "revenue":
            return rows[: index + 1] + [made] + rows[index + 1 :]
    return [made] + rows


def _fiscal_parts(period: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"FY(\d{4})Q([1-4])", str(period or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _annual_amount_series(quarters: list[dict[str, Any]], field: str) -> dict[str, Any] | None:
    """四个季度都有金额才算一个财年。同比只对上一个完整财年，缺年不跨年比较。"""
    buckets: dict[int, dict[int, float | None]] = {}
    for row in quarters:
        parts = _fiscal_parts(str(row.get("period") or ""))
        if parts is None:
            continue
        year, quarter = parts
        buckets.setdefault(year, {})[quarter] = row.get(field)
    totals: dict[int, float] = {}
    for year, slots in buckets.items():
        if any(quarter not in slots or slots[quarter] is None for quarter in (1, 2, 3, 4)):
            continue
        totals[year] = sum(float(slots[quarter]) for quarter in (1, 2, 3, 4))
    if not totals:
        return None
    labels: list[str] = []
    amount: list[float] = []
    yoy: list[float | None] = []
    text: list[str] = []
    for year in sorted(totals):
        total = totals[year]
        labels.append(f"FY{year}")
        amount.append(round(total / 1_000_000_000, 2))
        text.append(_fmt_money(total))
        prior = totals.get(year - 1)
        if prior in (None, 0):
            yoy.append(None)
        else:
            yoy.append(round((total - float(prior)) / abs(float(prior)) * 100, 1))
    return {"labels": labels, "amount": amount, "yoy": yoy, "amount_text": text}


def _annual_share_series(points: list[dict[str, Any]]) -> dict[str, Any] | None:
    """先把四个季度的分项金额加总，再算占比。某季缺失、留空或分项没有金额，则整年省略。"""
    buckets: dict[int, dict[int, dict[str, Any]]] = {}
    for point in points:
        parts = _fiscal_parts(str(point.get("period") or ""))
        if parts is None:
            continue
        year, quarter = parts
        buckets.setdefault(year, {})[quarter] = point
    annual_points: list[dict[str, Any]] = []
    for year in sorted(buckets):
        slots = buckets[year]
        if any(quarter not in slots for quarter in (1, 2, 3, 4)):
            continue
        quarters = [slots[quarter] for quarter in (1, 2, 3, 4)]
        if any(point.get("gap") or not point.get("items") for point in quarters):
            continue
        sums: dict[str, float] = {}
        meta: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        incomplete = False
        for point in quarters:
            for item in point.get("items") or []:
                if not item.get("stack"):
                    continue
                raw = item.get("value")
                if raw is None:
                    incomplete = True
                    break
                key = str(item["key"])
                if key not in sums:
                    sums[key] = 0.0
                    order.append(key)
                    meta[key] = item
                sums[key] += float(raw)
            if incomplete:
                break
        if incomplete or not order:
            continue
        total = sum(sums.values())
        if total <= 0:
            continue
        items = []
        for key in order:
            value = sums[key]
            items.append(
                {
                    "key": key,
                    "name": meta[key]["name"],
                    "legend": meta[key].get("legend") or meta[key]["name"],
                    "share": round(value / total * 100, 1),
                    "amount": _fmt_money(value),
                    "value": value,
                    "stack": True,
                }
            )
        annual_points.append({"period": f"FY{year}", "gap": False, "items": items})
    if not annual_points:
        return None
    return {
        "labels": [point["period"] for point in annual_points],
        "points": annual_points,
        "focus": annual_points[-1]["period"],
    }


def _amount_yoy_chart(quarters: list[dict[str, Any]], field: str, title: str, subtitle: str) -> dict[str, Any]:
    labels: list[str] = []
    amount: list[float | None] = []
    yoy: list[float | None] = []
    text: list[str | None] = []
    values = {str(row["period"]): row.get(field) for row in quarters}
    for row in quarters:
        label = str(row["period"])
        value = row.get(field)
        labels.append(label)
        amount.append(None if value is None else round(float(value) / 1_000_000_000, 2))
        text.append(None if value is None else _fmt_money(value))
        prior = values.get(yoy_fiscal_period(label))
        if value is None or prior in (None, 0):
            yoy.append(None)
        else:
            yoy.append(round((float(value) - float(prior)) / abs(float(prior)) * 100, 1))
    return {
        "title": title,
        "kind": "amount_yoy",
        "subtitle": subtitle,
        "labels": labels,
        "amount": amount,
        "yoy": yoy,
        "amount_text": text,
        "annual": _annual_amount_series(quarters, field),
    }


def _share_chart(points: list[dict[str, Any]], title: str, subtitle: str, focus: str) -> dict[str, Any]:
    labels = [point["period"] for point in points]
    if focus not in labels:
        present = [point["period"] for point in points if not point.get("gap")]
        focus = present[-1] if present else (labels[-1] if labels else "")
    return {
        "title": title,
        "kind": "share",
        "subtitle": subtitle,
        "labels": labels,
        "points": points,
        "focus": focus,
        "annual": _annual_share_series(points),
    }


def _line_points(quarters: list[dict[str, Any]], field: str, *, denominator: str) -> list[dict[str, Any]]:
    points = []
    for row in quarters:
        lines = list(row.get(field) or [])
        if not lines:
            points.append({"period": row["period"], "gap": True, "items": []})
            continue
        total = sum(float(line["value"]) for line in lines)
        if denominator == "revenue" and row.get("revenue"):
            total = float(row["revenue"])
        items = []
        for line in lines:
            value = float(line["value"])
            share = value / total * 100 if total else None
            items.append(
                {
                    "key": line["key"],
                    "name": line["name"],
                    "legend": "其他" if line["key"] == "other" else line["name"],
                    "share": None if share is None else round(share, 1),
                    "amount": _fmt_money(value),
                    "value": value,
                    "stack": True,
                }
            )
        points.append({"period": row["period"], "gap": False, "items": items})
    return points


def _business_points(mix: dict[str, Any]) -> list[dict[str, Any]]:
    points = []
    for row in mix.get("quarters") or []:
        if row.get("gap"):
            points.append({"period": row["period"], "gap": True, "items": []})
            continue
        items = []
        for line in row.get("rows") or []:
            share_text = line.get("share") or ""
            share = None
            if share_text.endswith("%"):
                try:
                    share = float(share_text[:-1])
                except ValueError:
                    share = None
            items.append(
                {
                    "key": line["key"],
                    "name": line["name"],
                    "legend": "其他" if line["key"] == "other" else line["name"],
                    "share": share,
                    "amount": line["revenue"],
                    "value": line.get("value"),
                    "stack": bool(line.get("stack")),
                }
            )
        points.append({"period": row["period"], "gap": not items, "items": items})
    return points


def _generic_charts(key: str) -> list[dict[str, Any]]:
    return [
        {"title": "水平值", "kind": "generic", "subtitle": "本季水平", "metric": key, "series": "level"},
        {"title": "环比", "kind": "generic", "subtitle": "较上一季", "metric": key, "series": "qoq"},
        {"title": "同比", "kind": "generic", "subtitle": "较去年同季", "metric": key, "series": "yoy"},
    ]


def _geo_names(quarters: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for row in quarters:
        for line in row.get("geo") or []:
            name = str(line.get("name") or "")
            if name:
                names.add(name)
    return names


def _meta_geo_history_note(quarters: list[dict[str, Any]]) -> str:
    """2016 两区改成 2017 四区时，保留原来那句地区注释。别的公司不套这句。"""
    names = _geo_names(quarters)
    if "美国以外" in names and {"美国和加拿大", "欧洲", "亚太", "世界其他地区"} <= names:
        return " 2016 年地区是美国与美国以外，2017 起改为美国和加拿大、欧洲、亚太、世界其他地区。"
    return ""


def _stacked_point_keys(points: list[dict[str, Any]]) -> list[str]:
    keys: list[str] = []
    for point in points:
        for item in point.get("items") or []:
            if item.get("stack") and item.get("key") not in keys:
                keys.append(str(item["key"]))
    return keys


def _share_focus(chart: dict[str, Any], period: str) -> str:
    """占比图只有 focus 跟本季走，柱的序列不跟。"""
    labels = [str(label) for label in (chart.get("labels") or [])]
    if period in labels:
        return period
    present = [str(point["period"]) for point in (chart.get("points") or []) if not point.get("gap")]
    if present:
        return present[-1]
    return labels[-1] if labels else ""


def _fin_series(ticker: str) -> dict[str, Any]:
    """金额、同比、占比的序列按股票算一次。币种变了金额文字会变，所以戳里带上。"""
    ident = str(data_dir(ticker)) + "\0" + _REPORTING_CCY.get()
    stamp = (_REPORTING_CCY.get(), _series_inputs(ticker))
    return _memo("fin", ident, stamp, lambda: _build_fin_series(ticker))


def _fin_view(ticker: str, period: str) -> dict[str, Any]:
    """点指标后要画的图。序列来自缓存，这里只改占比图指向的季度。"""
    base = _fin_series(ticker)
    by_key: dict[str, Any] = {}
    changed = False
    for key, spec in (base.get("by_key") or {}).items():
        charts = []
        spec_changed = False
        for chart in spec.get("charts") or []:
            if chart.get("kind") == "share":
                focus = _share_focus(chart, period)
                if chart.get("focus") != focus:
                    chart = {**chart, "focus": focus}
                    spec_changed = True
            charts.append(chart)
        if spec_changed:
            by_key[key] = {**spec, "charts": charts}
            changed = True
        else:
            by_key[key] = spec
    if not changed:
        return base
    return {"by_key": by_key}


def _build_fin_series(ticker: str) -> dict[str, Any]:
    """点指标后要画的图。没有历史文件的科目走原来的水平值 / 环比 / 同比。"""
    period = ""
    by_key: dict[str, dict[str, Any]] = {}
    money_sub, _eps_unit = _chart_units(_reporting_currency(ticker))
    statement = _load_statement(ticker)
    quarters = [row for row in (statement.get("quarters") or []) if row.get("period")]
    if quarters:
        by_key["revenue"] = {
            "note": statement.get("source") or "",
            "charts": [_amount_yoy_chart(quarters, "revenue", "营业收入", money_sub)],
        }
        if any(row.get("gross_profit") is not None for row in quarters):
            gross_chart = {
                "note": "毛利金额来自已保存的报表历史，和营业收入同一套季度。没有再拆开。",
                "charts": [_amount_yoy_chart(quarters, "gross_profit", "毛利", money_sub)],
            }
            by_key["gross_margin_gaap"] = gross_chart
            by_key["gross_margin_nongaap"] = gross_chart
        if any(row.get("opex") is not None for row in quarters):
            opex_charts = [_amount_yoy_chart(quarters, "opex", "营业费用", money_sub)]
            if any(row.get("opex_lines") for row in quarters):
                opex_charts.append(
                    _share_chart(
                        _line_points(quarters, "opex_lines", denominator="lines"),
                        "费用构成",
                        "占比",
                        period,
                    )
                )
                opex_note = "营业费用按申报列出的费用项加总，不含营业成本。占比以这些费用项合计为分母。"
            else:
                opex_note = "营业费用是已披露的合计，没有再拆成研发、销售和管理。不含营业成本。"
            by_key["opex"] = {"note": opex_note, "charts": opex_charts}
        if any("geo" in row for row in quarters):
            by_key["revenue"]["charts"].append(
                _share_chart(
                    _line_points(quarters, "geo", denominator="lines"),
                    "按地区",
                    "占比",
                    period,
                )
            )
            geo_note = _meta_geo_history_note(quarters)
            if geo_note:
                by_key["revenue"]["note"] = (by_key["revenue"]["note"] + geo_note).strip()
            elif not any(row.get("geo") for row in quarters):
                by_key["revenue"]["note"] = (
                    by_key["revenue"]["note"] + " 已保存的披露文字里没有地区收入，地区柱留空。"
                ).strip()
    mix = _segment_mix(ticker, period)
    business = _business_points(mix)
    if business and "revenue" in by_key:
        gaps = [point["period"] for point in business if point.get("gap")]
        note = by_key["revenue"]["note"]
        if gaps:
            note = f"{note} {'、'.join(gaps)} 没有完整的业务分部，柱上留空。"
        if _meta_family_stack(_stacked_point_keys(business)):
            note = f"{note} 业务柱只计广告、其他和 Reality Labs；应用家族是母项，不重复计入。"
        else:
            note = f"{note} 业务柱只计已披露且能加总的分部，没有占比的季度不补数。"
        by_key["revenue"]["note"] = note.strip()
        charts = by_key["revenue"]["charts"]
        charts.insert(1, _share_chart(business, "按业务", "占比", period))
    elif business:
        by_key["revenue"] = {
            "note": mix.get("note") or "",
            "charts": [_share_chart(business, "按业务", "占比", period)],
        }
    cost_quarters, cost_source = _cost_series(ticker)
    if cost_quarters and any(row.get("cost_of_revenue") is not None for row in cost_quarters):
        by_key["cost_of_revenue"] = {
            "note": _cost_note(cost_quarters, cost_source),
            "charts": _cost_charts(cost_quarters, period, money_sub),
        }
    return {"by_key": by_key}


def _fin_panel(rows: list[dict[str, Any]], view: dict[str, Any]) -> dict[str, Any]:
    default = "revenue" if any(row["key"] == "revenue" for row in rows) else (rows[0]["key"] if rows else "")
    spec = (view.get("by_key") or {}).get(default) or {}
    charts = spec.get("charts") or (_generic_charts(default) if default else [])
    grain = any(chart.get("kind") in {"amount_yoy", "share"} for chart in charts)
    return {**view, "default": default, "charts": charts, "note": spec.get("note") or "", "grain": grain}


def _price_view(doc: dict[str, Any]) -> dict[str, Any]:
    """Stage3 已停用：次日涨跌不再展示。"""
    del doc
    return {"text": "—", "cls": "na", "detail": ""}


_RANGE = " – "
_CALL_SOURCES = {"prepared_remarks", "qa"}
_PERIOD_ALIASES = {
    "next year": "明年",
    "long-term": "长期",
    "long term": "长期",
    "next few years": "未来几年",
    "coming months": "未来数月",
    "late january 2027": "2027年1月下旬",
}


def _period_label(period: str | None) -> str:
    """归一后的期间写成短中文。认不出的原文仍走原来的替换。"""
    text = (period or "").strip()
    if not text:
        return ""
    alias = _PERIOD_ALIASES.get(text.lower())
    if alias:
        return alias
    m = re.fullmatch(r"FY(\d{4})Q([1-4])", text)
    if m:
        return f"{m.group(1)}财年Q{m.group(2)}"
    m = re.fullmatch(r"FY(\d{4})", text)
    if m:
        return f"{m.group(1)}财年"
    shown = _period_zh(text)
    return "" if shown == "—" else shown


_PERFORMANCE_GUIDE = (
    ("revenue", "营收", ("revenue",), False),
    ("gross_margin", "毛利率", ("gross_margin_nongaap", "gross_margin_gaap", "gross_margin"), True),
    ("operating_margin", "营业利润率", ("operating_margin_nongaap", "operating_margin_gaap", "operating_margin"), True),
    ("net_margin", "净利润率", ("net_margin_nongaap", "net_margin_gaap", "net_margin"), True),
    ("eps", "调整后摊薄 EPS", ("eps_nongaap",), False),
    ("capex", "资本开支", ("capex",), False),
)

_OUTLOOK_TOPICS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("外部环境", ("监管", "法律审查", "诉讼环境", "宏观", "地缘", "政策", "外部环境", "scrutiny")),
    ("需求变化", ("需求", "广告业务", "广告主", "订单", "出货", "收入增长", "收入将保持", "客户需求")),
    ("供给变化", ("供给", "供应", "产能", "算力", "紧缺", "供需", "紧张")),
    ("竞争变化", ("竞争", "对手", "锁定优势", "份额", "并不锁定")),
    ("商业模式", ("订阅", "变现", "商业模式", "按用量", "收费", "代理式", "商业化")),
    ("公司战略", ("战略", "北极星", "超级智能", "个人智能", "走向盈利", "长期投入")),
)


def _guide_mid(item: dict[str, Any]) -> float | None:
    mid = _finite(item.get("mid"))
    if mid is not None:
        return mid
    low, high = _finite(item.get("low")), _finite(item.get("high"))
    if low is not None and high is not None:
        return (low + high) / 2
    return low if low is not None else high


def _as_ratio(value: float | None, ratio: bool) -> float | None:
    if value is None:
        return None
    if ratio and abs(value) > 1.5:
        return value / 100
    return value


def _blank_change() -> dict[str, str]:
    return {"text": "", "cls": "na"}


def _pp_change(current: float | None, base: float | None) -> dict[str, str]:
    if current is None or base is None:
        return _blank_change()
    delta = current - base
    # 小于 0.05 个百分点按持平，避免 -0.0pp 被标成下跌。
    if abs(delta) < 5e-4:
        return {"text": "0.0pp", "cls": "flat"}
    cls = "up" if delta > 0 else "down" if delta < 0 else "flat"
    return {"text": f"{delta * 100:+.1f}pp", "cls": cls}


def _safe_level_change(current: float | None, base: float | None) -> float | None:
    """金额变化。两边数量级差到 20 倍以上时留空，避免亿元和元直接相除。"""
    if current is None or base in (None, 0):
        return None
    if abs(current) > 0 and (abs(current) / abs(base) >= 20 or abs(base) / abs(current) >= 20):
        return None
    return (current - base) / abs(base)


def _cn_amount_scale(item: dict[str, Any]) -> float:
    """电话会把 1,960 亿元存成 1960。财务实际数是元，比较前乘回去。"""
    raws = " ".join(str(item.get(key) or "") for key in ("point_raw", "low_raw", "high_raw", "plus_minus_raw"))
    blob = raws if raws.strip() else str(item.get("source_quote") or "")
    if "万亿" in blob:
        return 1e12
    if "亿" in blob:
        return 1e8
    if "万" in blob and "%" not in blob and "百分" not in blob:
        return 1e4
    return 1.0


def _scaled_guide_item(item: dict[str, Any], ratio: bool) -> dict[str, Any]:
    if ratio:
        return item
    scale = _cn_amount_scale(item)
    if scale == 1:
        return item
    out = dict(item)
    for key in ("low", "mid", "high"):
        value = _finite(out.get(key))
        if value is None or abs(value) >= 1_000_000:
            continue
        out[key] = value * scale
    return out


def _guide_display(item: dict[str, Any], key: str) -> str:
    low, high = _finite(item.get("low")), _finite(item.get("high"))
    mid = _guide_mid(item)
    if item.get("plus_minus_raw") and mid is not None:
        return _fmt_guide(mid, key)
    if low is not None and high is not None and low != high:
        return f"{_fmt_guide(low, key)} – {_fmt_guide(high, key)}"
    return _fmt_guide(mid, key)


def _actual_level(doc: dict[str, Any] | None, keys: tuple[str, ...], ratio: bool) -> float | None:
    if not doc:
        return None
    fin = doc.get("financials") or {}
    for key in keys:
        block = fin.get(key) or {}
        if isinstance(block, dict) and _finite(block.get("value")) is not None:
            return _as_ratio(_finite(block.get("value")), ratio)
    return None


def _fiscal_year_level(docs: dict[str, dict[str, Any]], year: int, keys: tuple[str, ...]) -> float | None:
    """四个季度都有已披露实际才相加。缺一季就不算全年同比。"""
    total = 0.0
    for quarter in range(1, 5):
        value = _actual_level(docs.get(f"FY{year}Q{quarter}"), keys, False)
        if value is None:
            return None
        total += value
    return total


def _performance_rows(doc: dict[str, Any], docs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """管理层数字指引。没给的指标不占行；环比、同比、较共识算不出就空着。"""
    from pipeline.extract.guidance import normalize_period

    page = str((doc.get("meta") or {}).get("fiscal_period") or "")
    nxt = _shift_period(page, 1)
    items = (doc.get("guidance") or {}).get("items") or []
    score = {row.get("metric"): row for row in doc.get("scorecard") or []}
    rows = []
    for family, label, keys, ratio in _PERFORMANCE_GUIDE:
        chosen = []
        for item in items:
            if (item.get("metric_key") or "") not in keys or item.get("type") not in {None, "numeric"}:
                continue
            if _guide_mid(item) is None:
                continue
            chosen.append(item)
        if not chosen:
            continue

        def rank(item: dict[str, Any]) -> tuple:
            period = normalize_period(item.get("period"))
            return (
                0 if period == nxt else 1 if re.fullmatch(r"FY\d{4}Q[1-4]", period) else 2,
                0 if (item.get("metric_key") or "").endswith("_nongaap") else 1,
                0 if item.get("source") == "press_release" else 1,
            )

        item = _scaled_guide_item(sorted(chosen, key=rank)[0], ratio)
        period = normalize_period(item.get("period"))
        key = item.get("metric_key") or keys[0]
        mid = _as_ratio(_guide_mid(item), ratio)
        basis = "Non-GAAP" if key.endswith("_nongaap") else "GAAP" if key.endswith("_gaap") else ""
        shown = label if not basis or family in {"revenue", "eps", "capex"} else f"{label}（{basis}）"
        if family == "eps":
            shown = "调整后摊薄 EPS"
        if period == nxt:
            when = "下季"
        elif re.fullmatch(r"FY\d{4}", period or ""):
            when = f"{period}全年"
        else:
            when = _period_label(period) or period
        # 下季指引的环比基数是本季实际。已经披露的那一季，环比基数是上一季实际。
        qoq_base = None
        if re.fullmatch(r"FY\d{4}Q[1-4]", period or ""):
            if period == nxt:
                qoq_base = _actual_level(doc, keys, ratio)
            else:
                qoq_base = _actual_level(docs.get(_shift_period(period, -1) or ""), keys, ratio)
        yoy_doc = docs.get(yoy_fiscal_period(period)) if re.fullmatch(r"FY\d{4}Q[1-4]", period or "") else None
        yoy_base = _actual_level(yoy_doc, keys, ratio)
        if yoy_base is None and re.fullmatch(r"FY\d{4}", period or "") and not ratio:
            yoy_base = _fiscal_year_level(docs, int(period[2:6]) - 1, keys)
        if ratio:
            qoq = _pp_change(mid, qoq_base)
            yoy = _pp_change(mid, yoy_base)
        else:
            qoq_rel = _safe_level_change(mid, qoq_base)
            yoy_rel = _safe_level_change(mid, yoy_base)
            qoq = _change(qoq_rel) if qoq_rel is not None else _blank_change()
            yoy = _change(yoy_rel) if yoy_rel is not None else _blank_change()
        versus = _blank_change()
        bench_row = None
        forward = period == nxt and family in {"revenue", "eps"}
        if period == nxt and family == "revenue":
            bench_row = score.get("next_q_revenue_guidance")
        elif period == nxt and family == "eps":
            bench_row = score.get("next_q_eps_guidance")
        elif period == page and family in {"revenue", "eps", "gross_margin", "operating_margin", "net_margin", "capex"}:
            bench_row = score.get(family)
        bench = (bench_row or {}).get("benchmark") or {}
        if forward and bench.get("value") is not None and bench.get("diff_pct") is not None:
            versus = _change(bench.get("diff_pct"))
        elif bench.get("value") is not None and mid is not None:
            base = _as_ratio(_finite(bench.get("value")), ratio)
            if ratio:
                versus = _pp_change(mid, base)
            else:
                rel = _safe_level_change(mid, base)
                versus = _change(rel) if rel is not None else _blank_change()
        if qoq.get("text") in {None, "", "—"}:
            qoq = _blank_change()
        if yoy.get("text") in {None, "", "—"}:
            yoy = _blank_change()
        if versus.get("text") in {None, "", "—"}:
            versus = _blank_change()
        rows.append(
            {
                "label": shown,
                "period": when,
                "value": _guide_display(item, key),
                "qoq": qoq,
                "yoy": yoy,
                "vs": versus,
            }
        )
    return rows


def _outlook_pieces(text: str) -> list[str]:
    raw = re.sub(r"\s+", " ", (text or "").replace("\n", " ")).strip()
    parts = [part.strip() for part in re.findall(r"[^。！？]+[。！？]", raw)]
    merged: list[str] = []
    index = 0
    while index < len(parts):
        piece = parts[index]
        if index + 1 < len(parts) and piece.endswith(("原因有二。", "如下。", "两点。")):
            piece += parts[index + 1]
            index += 1
        merged.append(piece)
        index += 1
    kept = []
    for piece in merged:
        if len(piece) < 16 or len(piece) > 180:
            continue
        if re.search(r"\$\d|亿美元", piece) and not re.search(r"需求|供给|竞争|战略|监管|算力", piece):
            continue
        kept.append(piece)
    return kept


def _outlook_similar(left: str, right: str) -> bool:
    if left in right or right in left:
        return True
    chars_re = re.compile(r"[\u4e00-\u9fff]")

    def grams(text: str) -> set[str]:
        chars = "".join(chars_re.findall(text))
        return {chars[index : index + 4] for index in range(max(len(chars) - 3, 0))}

    a, b = grams(left), grams(right)
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) >= 0.34


def _keyword_outlook(doc: dict[str, Any]) -> list[dict[str, str]]:
    """旧数据还没有 Stage2 行业展望时的兜底。新跑的季度以 guidance.industry_outlook 为准。"""
    snippets: list[tuple[str, int]] = []
    for item in (doc.get("guidance") or {}).get("items") or []:
        if item.get("type") == "numeric" and (item.get("metric_key") or "") not in {"other"}:
            continue
        for sentence in _outlook_pieces(item.get("statement") or ""):
            snippets.append((sentence, 3))
    for qa in (doc.get("qa") or {}).get("items") or []:
        for sentence in _outlook_pieces(qa.get("answer_summary") or ""):
            snippets.append((sentence, 0))
    ranked: list[tuple[int, str, str]] = []
    for sentence, bonus in snippets:
        scores = [(sum(sentence.count(word) for word in words), title) for title, words in _OUTLOOK_TOPICS]
        score, title = max(scores)
        if score > 0:
            ranked.append((score + bonus, title, sentence))
    ranked.sort(key=lambda item: item[0], reverse=True)
    buckets: dict[str, list[str]] = {title: [] for title, _words in _OUTLOOK_TOPICS}
    for _score, title, sentence in ranked:
        if len(buckets[title]) >= 2:
            continue
        if any(_outlook_similar(sentence, kept) for kept in buckets[title]):
            continue
        buckets[title].append(sentence)
    return [{"title": title, "text": "".join(buckets[title])} for title, _words in _OUTLOOK_TOPICS if buckets[title]]


def _industry_outlook(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """优先用 Stage2 写入的行业展望。字段还不存在时，才用旧的问答句子兜底。"""
    from pipeline.analyze.outlook import normalize_industry_outlook

    guidance = doc.get("guidance") or {}
    if "industry_outlook" in guidance:
        return normalize_industry_outlook(guidance.get("industry_outlook"))
    return normalize_industry_outlook(_keyword_outlook(doc))


def _guide_section(doc: dict[str, Any], docs: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """指引展望只留两块：有数字的业绩指引，以及 Stage2 写入的行业展望。"""
    docs = docs or {}
    return {"rows": _performance_rows(doc, docs), "outlook": _industry_outlook(doc)}


def _revision_rows(doc: dict[str, Any]) -> dict[str, Any]:
    """Stage3 已停用：T+1/3/7 分析师修正不再展示。"""
    del doc
    return {"has": False, "rows": [], "current_q": None, "notes": ["Stage3 分析师修正已停用"]}


_QA_BULLET_RE = re.compile(r"^(?:[-*•・]|—)\s+")


def _qa_answer_lines(text: str | None) -> list[str]:
    """按发言人分行。不截断、不改写句子；单段原文仍是一行。"""
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return []
    lines: list[str] = []
    for line in raw.split("\n"):
        line = _QA_BULLET_RE.sub("", line.strip())
        if line:
            lines.append(line)
    return lines


def _qa_view(doc: dict[str, Any], has_prior_qa: bool) -> dict[str, Any]:
    qa = doc.get("qa") or {}
    items = []
    for q in qa.get("items") or []:
        # 旧提示要求信息不足时写「文本不足」。那不是回答，页面上不占一轮。
        if (q.get("question_summary") or "").strip() == "文本不足" or (q.get("answer_summary") or "").strip() == "文本不足":
            continue
        failed = bool(q.get("parse_failed")) or any(
            k in (q.get("question_summary") or "") for k in ("结构化失败", "解析失败", "未能自动结构化")
        )
        note = q.get("evasion_note") or ""
        items.append(
            {
                **q,
                "failed": failed,
                "evasive": not failed and (q.get("directness") or "") in {"partial", "evasive"},
                "note": "" if failed or "Expecting value" in note else note,
            }
        )
    hot = [t for t in qa.get("hot_topics") or [] if t != "其他"]
    return {
        "items": items,
        "hot": hot,
        # 没有上季问答时，「新话题」就是全部话题，没有信息量
        "new": [t for t in qa.get("new_topics") or [] if t != "其他"] if has_prior_qa else [],
        "evasive_count": sum(1 for i in items if i["evasive"]),
        "failed_count": sum(1 for i in items if i["failed"]),
    }


def _watchlist(doc: dict[str, Any]) -> list[dict[str, str]]:
    out = []
    for w in (doc.get("summary") or {}).get("next_watchlist") or []:
        if isinstance(w, str):
            out.append({"text": w, "threshold": "", "metric": ""})
        elif isinstance(w, dict):
            out.append(
                {
                    "text": w.get("condition") or w.get("text") or "",
                    "threshold": w.get("threshold") or "",
                    "metric": _metric_label(w.get("metric_key")) if w.get("metric_key") else "",
                }
            )
    return out


def _findings(doc: dict[str, Any]) -> list[str]:
    out = []
    for f in (doc.get("summary") or {}).get("key_findings") or []:
        text = f if isinstance(f, str) else str(f.get("text") or f)
        # 去掉模型附带的「证据：metric_key…」尾巴，页面上下文已经能对上
        text = re.sub(r"[；;]\s*证据[:：].*$", "", text).strip()
        out.append(text)
    return out


# 行内图用的序列。ratio=True 时同比/环比是百分点差，否则是变化率。
_HISTORY_SPECS: tuple[tuple[str, str, bool], ...] = (
    ("revenue", "revenue", False),
    ("gross_margin", "gross_margin_nongaap", True),
    ("gross_margin_gaap", "gross_margin_gaap", True),
    ("gross_margin_nongaap", "gross_margin_nongaap", True),
    ("gross_profit_gaap", "gross_profit_gaap", False),
    ("gross_profit_nongaap", "gross_profit_nongaap", False),
    ("operating_margin", "operating_margin_nongaap", True),
    ("operating_margin_gaap", "operating_margin_gaap", True),
    ("operating_margin_nongaap", "operating_margin_nongaap", True),
    ("operating_income_gaap", "operating_income_gaap", False),
    ("operating_income_nongaap", "operating_income_nongaap", False),
    ("net_margin_gaap", "net_margin_gaap", True),
    ("net_margin_nongaap", "net_margin_nongaap", True),
    ("net_income_gaap", "net_income_gaap", False),
    ("net_income_nongaap", "net_income_nongaap", False),
    ("share_count", "share_count", False),
    ("dso_days", "dso_days", True),
    ("dio_days", "dio_days", True),
    ("deferred_days", "deferred_days", True),
    ("eps", "eps_gaap", False),
    ("eps_nongaap", "eps_nongaap", False),
    ("ocf", "operating_cash_flow", False),
    ("capex", "capex", False),
    ("fcf", "fcf", False),
)

# 利润率行下面跟的金额。毛利润改由营业成本行代替，插在毛利率上面。
# 净利润率不是原表里的行，排在营业利润块之后。
_MARGIN_COMPANIONS: tuple[tuple[str, str, str], ...] = (
    ("operating_margin_nongaap", "operating_income_nongaap", "营业利润（Non-GAAP）"),
    ("operating_margin_gaap", "operating_income_gaap", "营业利润（GAAP）"),
    ("operating_margin", "operating_income", "营业利润"),
)
_QUOTE_MONEY_FIELDS = (
    "gross_profit_gaap",
    "gross_profit_nongaap",
    "operating_income_gaap",
    "operating_income_nongaap",
    "net_income_gaap",
    "net_income_nongaap",
)


def _rel(cur: float | None, base: float | None, ratio: bool) -> float | None:
    if cur is None or base is None:
        return None
    if ratio:
        return cur - base
    if base == 0:
        return None
    return (cur - base) / abs(base)


def _history(docs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """趋势图序列：季度 JSON 为准；缺失季度用新闻稿对比列（上季 / 去年同季）补点。

    同比 / 环比优先用季度 JSON 里已经算好的字段；缺了再用相邻季度的水平值补。
    """

    def pick(fin: dict[str, Any], *keys: str, field: str = "value") -> float | None:
        for k in keys:
            v = (fin.get(k) or {}).get(field)
            if v is not None:
                return v
        return None

    points: dict[str, dict[str, Any]] = {}
    for period, doc in docs.items():
        fin = doc.get("financials") or {}
        for which, target in (("year_ago", yoy_fiscal_period(period)), ("prior_q", prior_fiscal_period(period))):
            if target in docs or target in points:
                continue
            rev = pick(fin, "revenue", field=which)
            if rev is None:
                continue
            points[target] = {
                "period": target,
                "revenue": rev,
                "gross_margin": pick(fin, "gross_margin_nongaap", "gross_margin_gaap", field=which),
                "gross_margin_gaap": pick(fin, "gross_margin_gaap", field=which),
                "gross_margin_nongaap": pick(fin, "gross_margin_nongaap", field=which),
                "operating_margin": pick(fin, "operating_margin_nongaap", "operating_margin_gaap", field=which),
                "operating_margin_gaap": pick(fin, "operating_margin_gaap", field=which),
                "operating_margin_nongaap": pick(fin, "operating_margin_nongaap", field=which),
                "net_income_gaap": pick(fin, "net_income_gaap", field=which),
                "net_income_nongaap": pick(fin, "net_income_nongaap", field=which),
                "fcf": pick(fin, "fcf", field=which),
                "derived": True,
            }
    for period, doc in docs.items():
        fin = doc.get("financials") or {}
        point: dict[str, Any] = {"period": period, "derived": False}
        for series_key, fin_key, ratio in _HISTORY_SPECS:
            change_field = "yoy_pp" if ratio else "yoy_pct"
            qoq_field = "qoq_pp" if ratio else "qoq_pct"
            # 底部利润率图沿用「非 GAAP 优先」；gaap / nongaap 行则各自取自己的键
            if series_key in {"gross_margin", "operating_margin"}:
                alt = fin_key.replace("_nongaap", "_gaap")
                point[series_key] = pick(fin, fin_key, alt)
                point[f"{series_key}_yoy"] = pick(fin, fin_key, alt, field=change_field)
                point[f"{series_key}_qoq"] = pick(fin, fin_key, alt, field=qoq_field)
            else:
                point[series_key] = pick(fin, fin_key)
                point[f"{series_key}_yoy"] = pick(fin, fin_key, field=change_field)
                point[f"{series_key}_qoq"] = pick(fin, fin_key, field=qoq_field)
        points[period] = point

    _fill_profit_levels(points, docs)
    _fill_statement_levels(points, docs)
    ordered = [points[p] for p in sorted(points)]
    by_period = {p["period"]: p for p in ordered}
    for point in ordered:
        prev = by_period.get(prior_fiscal_period(point["period"]))
        base = by_period.get(yoy_fiscal_period(point["period"]))
        for series_key, _fin_key, ratio in _HISTORY_SPECS:
            if point.get(f"{series_key}_qoq") is None and prev is not None:
                point[f"{series_key}_qoq"] = _rel(point.get(series_key), prev.get(series_key), ratio)
            if point.get(f"{series_key}_yoy") is None and base is not None:
                point[f"{series_key}_yoy"] = _rel(point.get(series_key), base.get(series_key), ratio)
    return ordered


def _next_earnings(ticker: str) -> str:
    try:
        from pipeline.commands.poll_cmd import next_earnings_date

        d = next_earnings_date(ticker)
    except Exception:
        d = None
    return d.isoformat() if d else ""


def _billions_label(value: float) -> str:
    n = value / 1_000_000_000
    if abs(n - round(n)) < 0.05:
        return f"{round(n):.0f}"
    return f"{n:.1f}"


def _company_revenue_guide(doc: dict[str, Any]) -> str:
    """下一季营收指引区间。没有区间时返回空字符串。"""
    from pipeline.extract.guidance import normalize_period

    chosen = None
    for item in (doc.get("guidance") or {}).get("items") or []:
        if item.get("metric_key") != "revenue":
            continue
        if item.get("low") is None or item.get("high") is None:
            continue
        if not re.fullmatch(r"FY\d{4}Q[1-4]", normalize_period(item.get("period"))):
            continue
        chosen = item
    if not chosen:
        return ""
    return f"${_billions_label(float(chosen['low']))}–{_billions_label(float(chosen['high']))}B"


def _surprise_text(verdict: str | None, diff_pct: float | None) -> str | None:
    """beat / miss 用英文加幅度。其他判定返回 None，由调用方保留中文。"""
    if verdict == "beat":
        word = "beat"
    elif verdict == "miss":
        word = "miss"
    else:
        return None
    shown = _change(diff_pct)["text"].lstrip("+-") if diff_pct is not None else ""
    if not shown or shown == "—":
        return word
    return f"{word} {shown}"


def _chip_subject(label: str) -> str:
    """卡片和页首标签默认按 Non-GAAP 一致预期理解，不再把口径写在 beat/miss 后面。"""
    text = re.sub(r"[（(]\s*Non-GAAP\s*[）)]", "", label or "")
    text = re.sub(r"\s*·?\s*Non-GAAP", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _score_lines(
    ticker: str,
    doc: dict[str, Any],
    market: dict[str, Any] | None,
    cfg: dict[str, Any],
) -> list[dict[str, str]]:
    """META 判断条：营收、每股收益、公司下季指引、下次财报。不写次日涨跌，也不标注 Non-GAAP。"""
    del market  # 次日涨跌已从卡片和页首拿掉，保留参数以免旧调用崩掉
    if ticker != "META" or not (doc.get("scorecard") or []):
        return []
    sc = {c.get("metric"): c for c in doc.get("scorecard") or []}
    words = {"beat": "超预期", "miss": "不及预期", "inline": "符合预期"}
    cls_of = {"beat": "ok", "miss": "bad", "inline": ""}

    def vs(metric: str, label: str) -> dict[str, str]:
        row = sc.get(metric) or {}
        verdict = row.get("verdict") or "unknown"
        diff = _num((row.get("benchmark") or {}).get("diff_pct"))
        surprise = _surprise_text(verdict, diff)
        if surprise:
            return {"text": f"{label} {surprise}", "cls": cls_of.get(verdict, "muted")}
        gap = _change(diff)["text"] if diff is not None else ""
        text = f"{label} {words.get(verdict, '待核对')}"
        if gap and gap != "—":
            text += f" {gap}"
        return {"text": text, "cls": cls_of.get(verdict, "muted")}

    guide = _company_revenue_guide(doc)
    when = _next_earnings(ticker)
    timing = "盘后" if (cfg.get("release_timing") or "amc") == "amc" else "盘前"
    return [
        vs("revenue", "营收"),
        vs("eps", "每股 EPS"),
        {"text": f"下季指引 {guide}" if guide else "下季指引待定", "cls": ""},
        {"text": f"下次财报 {when} {timing}" if when else "下次财报日待定", "cls": "muted" if not when else ""},
    ]


STATUS_SHORT = {"hold": "持仓", "wait": "等待", "watch": "观察"}
# 类型筛选的习惯顺序。配置里还有别的类型时排在这些后面，按出现顺序。
_KIND_ORDER = ("半导体", "软件与云", "金融", "消费")


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return dt


def _plain_cfg(value: Any) -> str:
    return _plain(value)


def _surprise_cell(row: dict[str, Any] | None) -> dict[str, str]:
    """营收 / EPS / 下季 EPS 指引的 beat/miss。数字来自 scorecard，和财报页同一套比较。

    没有实际值时留空，不把营收区间拿来充当下季指引。
    """
    row = row or {}
    if row.get("actual") is None:
        return {"verdict": "", "label": "—", "cls": "muted"}
    verdict = row.get("verdict") or ""
    diff = _num((row.get("benchmark") or {}).get("diff_pct"))
    surprise = _surprise_text(verdict, diff)
    if surprise:
        return {"verdict": verdict, "label": surprise, "cls": verdict}
    if verdict == "inline":
        return {"verdict": "inline", "label": "符合预期", "cls": "inline"}
    if verdict == "unknown":
        return {"verdict": "unknown", "label": "待核对", "cls": "unknown"}
    if verdict:
        return {"verdict": verdict, "label": _verdict_label(verdict), "cls": ""}
    return {"verdict": "", "label": "—", "cls": "muted"}


def _saved_quote(ticker: str) -> dict[str, Any]:
    """已保存的 TradingView 行情列。没有文件或没有 close 就当没有行情。"""
    path = ROOT / "data" / ticker / "consensus" / "tradingview_scan.json"
    if not path.exists():
        return {}
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    quote = snap.get("quote") or {}
    cols = quote.get("columns") or {}
    fx = quote.get("fx") if isinstance(quote.get("fx"), dict) else None
    return {
        "columns": cols,
        "fetched_at": quote.get("fetched_at") or snap.get("fetched_at") or "",
        "fx": fx,
    }


def _quote_view(
    cols: dict[str, Any] | None,
    *,
    quote_ccy: str = "USD",
    eps_ccy: str = "USD",
    fx: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """股价、较昨日涨跌、RSI、Forward PE。缺列就留空，不临时拉行情。

    同一币种时 Forward PE = close ÷ eps_estimate_ntm。
    港币股价对人民币 EPS 时，先乘当天 HKDCNY。没有当天汇率则写「待核对」。
    涨跌只认已经存好的小数比例 day_change_pct / change_pct（0.012 = +1.2%）。
    现有快照没有上一交易日收盘，所以这一列通常是「—」。
    """
    cols = cols or {}
    close = _finite(cols.get("close"))
    ntm = _finite(cols.get("eps_estimate_ntm"))
    rsi = _finite(cols.get("RSI"))
    price = f"{_ccy_prefix(quote_ccy)}{close:,.2f}" if close is not None else "—"
    multiple = pe_multiple(close, ntm, price_ccy=quote_ccy, eps_ccy=eps_ccy, fx=fx)
    pe = multiple["text"]
    rsi_text = f"{rsi:.1f}" if rsi is not None else "—"
    pct = _finite(cols.get("day_change_pct"))
    if pct is None:
        pct = _finite(cols.get("change_pct"))
    if pct is None:
        change = {"text": "—", "cls": "na"}
    else:
        shown = _change(pct)
        change = {"text": shown["text"], "cls": shown["cls"]}
    return {"price": price, "pe": pe, "pe_fx": multiple["fx"], "rsi": rsi_text, "change": change}


def _pool_labels(cfg: dict[str, Any], pos: dict[str, Any] | None) -> dict[str, str]:
    """分组、类型、目标估值。股票配置优先，仓位档案兜底。两处都没有就空。"""
    pos = pos or {}
    group = _plain_cfg(cfg.get("分组") or cfg.get("group") or pos.get("group"))
    kind = _plain_cfg(cfg.get("类型") or cfg.get("type") or cfg.get("category") or pos.get("kind"))
    if "目标估值" in cfg:
        target = _plain_cfg(cfg.get("目标估值"))
    elif cfg.get("target_valuation") is not None:
        target = _plain_cfg(cfg.get("target_valuation"))
    else:
        target = ""
    if not target:
        target = pos.get("target_value") or ""
    # 只写了一种分类时，筛选和分组列用同一个名字
    if not kind and group:
        kind = group
    if not group and kind:
        group = kind
    return {"group": group, "kind": kind, "target": target}


def _trigger_view(pos: dict[str, Any] | None, doc: dict[str, Any] | None) -> dict[str, str]:
    """买卖条件是否触发。没有条件，或这季还没评估，都显示「—」，不把未评估写成否。"""
    triggers = (pos or {}).get("triggers") or []
    if not triggers:
        return {"text": "—", "cls": "na"}
    review = (doc or {}).get("thesis_review") or {}
    rows = [r for r in (review.get("triggers") or []) if isinstance(r, dict)]
    if not rows:
        return {"text": "—", "cls": "na"}
    by = {r.get("name"): r.get("state") for r in rows}
    states = [by.get(t.get("name")) for t in triggers]
    if any(s == "triggered" for s in states):
        return {"text": "是", "cls": "yes"}
    if all(s == "not_triggered" for s in states):
        return {"text": "否", "cls": "no"}
    return {"text": "—", "cls": "na"}


def _release_view(doc: dict[str, Any] | None, ticker: str, today: date) -> dict[str, str]:
    """最近一次财报的日期（美东）。优先用新闻稿发布时间，否则用快照里已过去的财报时点。悬停仍可看时刻。"""
    raw = ((doc or {}).get("meta") or {}).get("release_at_utc")
    dt = _parse_dt(raw)
    title = _fmt_release(raw) if dt else ""
    if dt is None:
        dt = _past_earnings_dt(ticker, today)
        title = dt.astimezone(ET).strftime("%Y-%m-%d %H:%M 美东") if dt else ""
    if dt is None:
        return {"text": "—", "title": ""}
    return {"text": dt.astimezone(ET).strftime("%Y-%m-%d"), "title": title}


def _past_earnings_dt(ticker: str, today: date) -> datetime | None:
    folder = data_dir(ticker) / "snapshots"
    paths = sorted(folder.glob("*.json")) if folder.exists() else []
    if not paths:
        return None
    try:
        snap = json.loads(paths[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    best: datetime | None = None
    for raw in (snap.get("earnings_dates") or {}).get("index") or []:
        dt = _parse_dt(str(raw))
        if dt is None or dt.astimezone(ET).date() > today:
            continue
        if best is None or dt > best:
            best = dt
    return best


def _next_text(when: str, timing: str) -> str:
    if when:
        return f"{when} {timing}"
    return "下次财报日待定"


def _kind_filters(rows: list[dict[str, Any]]) -> list[str]:
    found: list[str] = []
    for row in rows:
        kind = row.get("kind") or ""
        if kind and kind not in found:
            found.append(kind)

    def sort_key(kind: str) -> tuple[int, int]:
        pref = _KIND_ORDER.index(kind) if kind in _KIND_ORDER else len(_KIND_ORDER)
        return pref, found.index(kind)

    return sorted(found, key=sort_key)



def _pool_source_note(tickers: list[str], index_rows: list[dict[str, Any]]) -> str:
    """首页脚注：分别标 TradingView / Alpha Vantage 截止时间。没有 fetched_at 不编造。"""
    from pipeline.sources.av_estimates import load_earnings_record

    tv_stamps: list[str] = []
    av_stamps: list[str] = []
    for ticker in tickers:
        tv = _tradingview(ticker) or {}
        stamp = str(((tv.get("quote") or {}).get("fetched_at") or tv.get("fetched_at") or ""))
        if stamp:
            tv_stamps.append(stamp)
        av_stamp = str(load_earnings_record(ticker, fetch=False).get("fetched_at") or "")
        if av_stamp:
            av_stamps.append(av_stamp)
    parts: list[str] = []
    if tv_stamps:
        latest_tv = max(tv_stamps)
        parts.append(
            _source_cutoff("股价 / 涨跌 / 市值 / RSI / 四季 EPS·营收一致预期：TradingView scanner", latest_tv)
        )
    else:
        parts.append("股价 / 涨跌 / 市值 / RSI / 四季一致预期：TradingView scanner（尚未写入 tradingview_scan.json）")
    if av_stamps:
        latest_av = max(av_stamps)
        parts.append(
            _source_cutoff("当前季 EPS / 覆盖 / 上修下修 / 全年 EPS：Alpha Vantage EARNINGS_ESTIMATES", latest_av)
        )
    elif index_rows and not any(row["quarter"]["has_numbers"] for row in index_rows):
        parts.append(
            "当前季 EPS、覆盖人数和 30 日上修/下修来自 Alpha Vantage；"
            "没有 ALPHAVANTAGE_KEY 且没有 av_estimates.json 时这些格子是「—」。"
        )
    else:
        parts.append("全年 EPS：Alpha Vantage EARNINGS_ESTIMATES（fiscal year）")
    return "；".join(parts) + "。"


def _pool_as_of(tickers: list[str]) -> str:
    """首页副标题的数据时间：各股最新快照和行情抓取时间里最晚的一个，写成北京时间。"""
    latest: datetime | None = None
    for ticker in tickers:
        folder = data_dir(ticker) / "snapshots"
        paths = sorted(folder.glob("*.json")) if folder.exists() else []
        if paths:
            try:
                snap = json.loads(paths[-1].read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                snap = {}
            dt = _parse_dt(snap.get("snapshot_at"))
            if dt and (latest is None or dt > latest):
                latest = dt
        quote = _saved_quote(ticker)
        dt = _parse_dt(quote.get("fetched_at"))
        if dt and (latest is None or dt > latest):
            latest = dt
    if latest is None:
        return ""
    return latest.astimezone(BJ).strftime("%Y-%m-%d %H:%M")


def _quarter_view(
    ticker: str,
    cfg: dict[str, Any],
    doc: dict[str, Any] | None,
    today: date,
    period: str | None = None,
) -> dict[str, Any]:
    """当前季 EPS 与修正评价。季度只认已披露财季的下一季，再对齐 Alpha Vantage。"""
    from pipeline.commands.poll_cmd import known_earnings_dates
    from pipeline.compute.revision_signal import (
        choose_current_estimate,
        earnings_drive_tag,
        format_current_quarter,
    )
    from pipeline.sources.av_estimates import load_earnings_record

    fye = int(cfg.get("fiscal_year_end_month") or 12)
    record = load_earnings_record(ticker)
    reported = period if period and re.fullmatch(r"FY\d{4}Q[1-4]", period) else None
    row = choose_current_estimate(
        record.get("estimates"),
        reported_period=reported,
        fiscal_year_end_month=fye,
        today=today,
    )
    dates = list(known_earnings_dates(ticker))
    release = _parse_dt(((doc or {}).get("meta") or {}).get("release_at_utc"))
    if release is not None:
        day = release.astimezone(ET).date()
        if day not in dates:
            dates.append(day)
    return format_current_quarter(
        row,
        earnings_drive_tag(dates, today),
        fetched_at=record.get("fetched_at"),
        now=datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc),
    )


def _display_name(cfg: dict[str, Any], ticker: str) -> str:
    """首页名称：中文简称 (英文简称/代码)。优先 name_zh / 简称。"""
    zh = _plain_cfg(cfg.get("name_zh") or cfg.get("简称") or "")
    if not zh:
        name = str(cfg.get("name") or "").strip()
        if any("\u4e00" <= ch <= "\u9fff" for ch in name):
            zh = name.replace("控股", "").replace("集团", "").strip() or name
        else:
            zh = ticker
    return f"{zh} ({ticker})"


def _index_row(
    t: str,
    cfg: dict[str, Any],
    period: str | None,
    doc: dict[str, Any] | None,
    today: date | None = None,
) -> dict[str, Any]:
    doc = doc or {}
    today = today or datetime.now(ET).date()
    sc = {c["metric"]: c for c in doc.get("scorecard") or []}
    pos = load_position(t)
    labels = _pool_labels(cfg, pos)
    saved = _saved_quote(t)
    quote_ccy = price_currency(cfg)
    eps_ccy = eps_currency(cfg)
    cols = saved.get("columns") or {}
    fund = str(cols.get("fundamental_currency_code") or "").strip().upper()
    fx = None
    if quote_ccy != eps_ccy:
        from pipeline.sources.fx import rate_on

        fx = rate_on(_market_day(saved.get("fetched_at"), quote_ccy), attached=saved.get("fx"))
    # 扫描把 eps_estimate_ntm 标成股价币种（港股是港币）时直接相除。
    # 先当成人民币再等 HKDCNY，缺当天汇率就会把本来同币种的倍数写成待核对。
    if fund and fund == quote_ccy and fund != eps_ccy:
        quote = _quote_view(cols, quote_ccy=quote_ccy, eps_ccy=fund, fx=None)
    else:
        quote = _quote_view(cols, quote_ccy=quote_ccy, eps_ccy=eps_ccy, fx=fx)
    shares, share_note = _stored_shares(t, period)
    cap = _market_cap_view(
        saved.get("columns"),
        price=_finite((saved.get("columns") or {}).get("close")),
        shares=shares,
        currency=quote_ccy,
        share_note=share_note,
    )
    status_key = (pos or {}).get("stage") or ""
    timing = "盘后" if (cfg.get("release_timing") or "amc") == "amc" else "盘前"
    when = _next_earnings(t)
    return {
        "ticker": t,
        "name": cfg.get("name") or "",
        "display_name": _display_name(cfg, t),
        "pos_stage": status_key,
        "status_key": status_key,
        "status_label": STATUS_SHORT.get(status_key, "—"),
        "group": labels["group"] or "—",
        "kind": labels["kind"],
        "period": period or "—",
        "revenue": _surprise_cell(sc.get("revenue")),
        "eps": _surprise_cell(sc.get("eps")),
        # 下季指引只看 EPS 对一致预期，不用营收区间
        "guide": _surprise_cell(sc.get("next_q_eps_guidance")),
        "quote_price": quote["price"],
        "market_cap": cap["value"],
        "change": quote["change"],
        "rsi": quote["rsi"],
        "forward_pe": quote["pe"],
        "forward_pe_fx": quote.get("pe_fx") or "",
        "target": labels["target"] or "—",
        "triggered": _trigger_view(pos, doc),
        "reported": _release_view(doc, t, today),
        "price": _price_view(doc) if doc else {"text": "—", "cls": "na", "detail": ""},
        "stage": _stage_label((doc.get("status") or {}).get("stage") if doc else "scheduled"),
        "headline": (doc.get("summary") or {}).get("headline") or "",
        "next_earnings": when,
        "next_text": _next_text(when, timing),
        "timing": timing,
        "quarter": _quarter_view(t, cfg, doc, today, period),
        "href": f"stocks/{t}/index.html",
        "listing": str(cfg.get("listing") or "domestic"),
        "reporting_currency": str(cfg.get("reporting_currency") or ""),
    }


# ---------------------------------------------------------------- 判断层（规则，不调用 LLM）

GAP_SCALE = 0.15  # 预期差尺子的满刻度：±15%
GAP_ROWS = [
    ("revenue", "本季营收"),
    ("eps", "本季 EPS"),
    ("next_q_revenue_guidance", "下季营收指引"),
    ("next_q_eps_guidance", "下季 EPS 指引"),
]


def _gap_rows(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """预期差尺子：竖线 = 财报前一致预期，圆点 = 实际 / 指引中值。"""
    sc = {c.get("metric"): c for c in doc.get("scorecard") or []}
    rows = []
    for key, label in GAP_ROWS:
        row = sc.get(key)
        if not row or row.get("actual") is None:
            continue
        b = row.get("benchmark") or {}
        comparable = key == "eps" and row.get("basis") in {"ex_legal", "non_gaap"}
        if comparable:
            label = "本季 EPS（Non-GAAP）"
        fmt_key = "eps" if "eps" in key else key
        actual = _fmt_value(row.get("actual"), fmt_key)
        diff = _num(b.get("diff_pct"))
        if b.get("value") is None or diff is None:
            prefix = "Non-GAAP" if comparable else ("指引中值" if "guidance" in key else "实际")
            rows.append({"label": label, "has": False, "detail": f"{prefix} {actual}"})
            continue
        clipped = max(-GAP_SCALE, min(GAP_SCALE, diff))
        if comparable:
            detail = f"Non-GAAP {actual} · 一致预期 {_fmt_value(b.get('value'), fmt_key)}"
        else:
            detail = f"{'指引中值' if 'guidance' in key else '实际'} {actual} · 预期 {_fmt_value(b.get('value'), fmt_key)}"
        rows.append(
            {
                "label": label,
                "has": True,
                "pos": round(50 + clipped / GAP_SCALE * 50, 1),
                "clipped": abs(diff) > GAP_SCALE,
                "gap": _change(diff),
                "verdict": row.get("verdict") or "",
                "detail": detail,
            }
        )
    return rows


def _fq_label(period: str) -> str:
    year, quarter = parse_fiscal_period(period)
    return f"FQ{quarter} {year}"


def _label_to_period(label: str | None) -> str | None:
    match = re.fullmatch(r"FQ([1-4]) (\d{4})", str(label or "").strip())
    if not match:
        return None
    return f"FY{match.group(2)}Q{match.group(1)}"


def _scorecard_row(doc: dict[str, Any], metric: str) -> dict[str, Any] | None:
    for row in doc.get("scorecard") or []:
        if row.get("metric") == metric:
            return row
    return None


def _scorecard_consensus(doc: dict[str, Any], metric: str) -> float | None:
    row = _scorecard_row(doc, metric) or {}
    bench = row.get("benchmark") or {}
    if bench.get("source") != "consensus":
        return None
    return _finite(bench.get("value"))


def _tv_period_estimate(tv: dict[str, Any] | None, keys: tuple[str, ...], period: str) -> float | None:
    rows_used = (tv or {}).get("rows_used") or {}
    rows: list[Any] = []
    for key in keys:
        rows = rows_used.get(key) or []
        if rows:
            break
    for item in rows:
        if _period_from_tv(item.get("FiscalPeriod")) == period:
            return _finite(item.get("Estimate"))
    return None


def _aligned_consensus(actual: float | None, consensus: float | None) -> float | None:
    """数量级差出五倍以上就不当成同一口径，避免把台币共识或 ADR 共识画到另一套实际旁边。"""
    if consensus is None or actual is None:
        return consensus
    if actual > 0 and consensus > 0:
        ratio = actual / consensus
        if ratio > 5 or ratio < 0.2:
            return None
    return consensus


def _positive_hkdcny(fx: dict[str, Any] | None) -> float | None:
    """1 港币折合的人民币。日期、货币对或非正数都不认。"""
    if not isinstance(fx, dict):
        return None
    if str(fx.get("pair") or "HKDCNY").upper() != "HKDCNY":
        return None
    if not str(fx.get("date") or "").strip():
        return None
    rate = _finite(fx.get("rate"))
    if rate is None or rate <= 0:
        return None
    return rate


def _quote_fx(ticker: str, cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """行情日当天的 HKDCNY。没有这一天的收盘就 None，不拿前后日顶上。"""
    from pipeline.sources.fx import rate_on

    if cfg is None:
        try:
            cfg = load_ticker_config(ticker)
        except (FileNotFoundError, OSError):
            cfg = {}
    saved = _saved_quote(ticker)
    return rate_on(_market_day(saved.get("fetched_at"), price_currency(cfg)), attached=saved.get("fx"))


def _implied_cny_per_hkd(ticker: str, tv: dict[str, Any] | None) -> dict[str, float]:
    """已披露季度的人民币营收 / TradingView 港币营收实际。

    扫描按季把人民币换成港币，汇率不是常数。用营收而不是 EPS，避免把 GAAP 和非国际财务报告准则的差额当成汇率。
    比例超出 0.5–1.5 就当口径对不上，这一季不采用。
    """
    used = (tv or {}).get("rows_used") if isinstance((tv or {}).get("rows_used"), dict) else {}
    rows = used.get("revenues_fq_h") or []
    if not ticker or not isinstance(rows, list):
        return {}
    docs = _load_docs(ticker)
    found: dict[str, float] = {}
    for item in rows:
        if not isinstance(item, dict) or not item.get("IsReported"):
            continue
        period = _period_from_tv(item.get("FiscalPeriod"))
        tv_actual = _finite(item.get("Actual"))
        if not period or not tv_actual or tv_actual <= 0:
            continue
        cny = _finite((((docs.get(period) or {}).get("financials") or {}).get("revenue") or {}).get("value"))
        if cny is None or cny <= 0:
            continue
        ratio = cny / tv_actual
        if 0.5 <= ratio <= 1.5:
            found[period] = ratio
    return found


def _tv_fx_context(
    ticker: str,
    cfg: dict[str, Any] | None = None,
    tv: dict[str, Any] | None = None,
    fx: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把 TradingView 金额换到报表币种。

    没写 fundamental_currency_code，或已经是报表币种：原样使用。
    港币对人民币：已披露季用营收隐含汇率；还没有报表实际的季度用行情日 HKDCNY。
    两种都没有就把这笔数字丢掉，不把港币当成人民币。
    """
    if tv is None and ticker:
        tv = _tradingview(ticker) or {}
    tv = tv or {}
    if cfg is None and ticker:
        try:
            cfg = load_ticker_config(ticker)
        except (FileNotFoundError, OSError):
            cfg = {}
    cfg = cfg or {}
    src = str(((tv.get("quote") or {}).get("columns") or {}).get("fundamental_currency_code") or "").strip().upper()
    dst = eps_currency(cfg)
    if not src or src == dst:
        return {"identity": True, "spot": 1.0, "by_period": {}}
    if {src, dst} != {"HKD", "CNY"}:
        return {"identity": False, "spot": None, "by_period": {}}
    if fx is None and ticker:
        fx = _quote_fx(ticker, cfg)
    rate = _positive_hkdcny(fx)
    if src == "HKD":
        spot = rate
    else:
        spot = (1.0 / rate) if rate else None
    by_period = _implied_cny_per_hkd(ticker, tv) if src == "HKD" and dst == "CNY" and ticker else {}
    return {"identity": False, "spot": spot, "by_period": by_period}


def _scale_tv_money(value: float | None, period: str | None, ctx: dict[str, Any]) -> float | None:
    """同一币种不缩放。港币金额优先用该季隐含汇率，否则用行情日汇率。"""
    if value is None:
        return None
    if ctx.get("identity"):
        return value
    factor = None
    by_period = ctx.get("by_period") or {}
    if period and period in by_period:
        factor = by_period[period]
    elif ctx.get("spot") is not None:
        factor = ctx["spot"]
    if factor is None:
        return None
    return float(value) * float(factor)


def _apply_tv_scale(scale, period: str | None, value: float | None) -> float | None:
    if value is None:
        return None
    if scale is None:
        return value
    return scale(period, value)


def _tv_scaler(ticker: str, tv: dict[str, Any] | None = None, fx: dict[str, Any] | None = None):
    """返回 (换算上下文, (period, value) -> 报表币种金额)。"""
    ctx = _tv_fx_context(ticker, tv=tv, fx=fx) if ticker else {"identity": True, "spot": 1.0, "by_period": {}}

    def scale(period: str | None, value: float | None) -> float | None:
        return _scale_tv_money(value, period, ctx)

    return ctx, scale


def _saved_consensus(doc: dict[str, Any], metric: str, period: str, ticker: str, actual: float | None) -> float | None:
    consensus = _scorecard_consensus(doc, metric)
    if consensus is None and ticker:
        tv = _tradingview(ticker)
        if metric == "eps":
            consensus = _tv_period_estimate(tv, ("earnings_fq_h_chart", "earnings_fq_h"), period)
        elif metric == "revenue":
            consensus = _tv_period_estimate(tv, ("revenues_fq_h",), period)
        if consensus is not None:
            consensus = _scale_tv_money(consensus, period, _tv_fx_context(ticker, tv=tv))
    return _aligned_consensus(actual, consensus)


def _eps_basis_label(doc: dict[str, Any]) -> str:
    basis = str((_scorecard_row(doc, "eps") or {}).get("basis") or "")
    if basis in {"non_gaap", "ex_legal"}:
        return "Non-GAAP"
    if basis == "gaap":
        return "GAAP"
    return ""


def _tv_reported_actuals(tv: dict[str, Any] | None, metric: str, scale=None) -> dict[str, float]:
    """扫描里已经公布的实际。只作同比基数或补季报里没写上的 EPS，不覆盖已有实际。

    scale 把港币实际换到报表币种。换不了的季度丢掉，避免和人民币实际比数量级。
    """
    used = (tv or {}).get("rows_used") if isinstance((tv or {}).get("rows_used"), dict) else {}
    if metric == "eps":
        groups = [used.get("earnings_fq_h_chart") or [], used.get("earnings_fq_h") or []]
    else:
        groups = [used.get("revenues_fq_h") or []]
    found: dict[str, float] = {}
    for rows in groups:
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict) or not item.get("IsReported"):
                continue
            period = _period_from_tv(item.get("FiscalPeriod"))
            actual = _apply_tv_scale(scale, period, _finite(item.get("Actual")))
            if period and actual is not None and period not in found:
                found[period] = actual
    return found


def _same_scale(candidate: float | None, anchors: list[float]) -> bool:
    """和相邻实际差出五倍以上就当拆股或另一套单位，不拿来补这个点。"""
    if candidate is None or candidate == 0:
        return False
    for anchor in anchors:
        if anchor in (None, 0):
            continue
        ratio = abs(float(candidate)) / abs(float(anchor))
        if 0.2 <= ratio <= 5:
            return True
    return False


def _nearby_actuals(period: str, actuals: dict[str, float], *, limit: int = 4) -> list[float]:
    """先看紧邻季度。紧邻没有已保存实际，再往外看。"""
    prev = nxt = period
    for _ in range(limit):
        prev = prior_fiscal_period(prev)
        nxt = next_fiscal_period(nxt)
        found = [actuals[key] for key in (prev, nxt) if actuals.get(key) not in (None, 0)]
        if found:
            return found
    return []


def _scan_matches_actuals(scan: dict[str, float], actuals: dict[str, float]) -> bool:
    """最近 8 个重叠季都要落在 ±3% 以内（至少 3 季），才把扫描实际当成同一套 EPS。

    超出这个范围就整段不用：港股另一套口径、Non-GAAP 对 GAAP，或只有个别季度碰巧重合。
    """
    common = [
        period
        for period in sorted(actuals)
        if actuals.get(period) not in (None, 0) and scan.get(period) not in (None, 0)
    ]
    recent = common[-8:]
    if len(recent) < 3:
        return False
    return all(0.97 <= abs(scan[period]) / abs(actuals[period]) <= 1.03 for period in recent)


def _scaled_scan_actual(period: str, scan: dict[str, float], actuals: dict[str, float]) -> float | None:
    """季报没有这季实际时，只用和已保存实际同一套口径的扫描已公布数。"""
    if period in actuals or not _scan_matches_actuals(scan, actuals):
        return None
    candidate = scan.get(period)
    if candidate is None:
        return None
    if not _same_scale(candidate, _nearby_actuals(period, actuals)):
        return None
    return candidate


def _stored_eps_yoy(doc: dict[str, Any]) -> float | None:
    basis = str((_scorecard_row(doc, "eps") or {}).get("basis") or "")
    fin = doc.get("financials") or {}
    if basis == "non_gaap":
        return _finite((fin.get("eps_nongaap") or {}).get("yoy_pct"))
    if basis == "gaap":
        return _finite((fin.get("eps_gaap") or {}).get("yoy_pct"))
    return _finite((fin.get("eps_nongaap") or {}).get("yoy_pct")) or _finite((fin.get("eps_gaap") or {}).get("yoy_pct"))


def _eps_quotes_are_ntd(docs: dict[str, dict[str, Any]], periods: list[str]) -> bool:
    seen = False
    for period in periods:
        fin = (docs.get(period) or {}).get("financials") or {}
        quotes = [
            str((fin.get(key) or {}).get("source_quote") or "")
            for key in ("eps_gaap", "eps_nongaap", "eps")
        ]
        quotes = [quote for quote in quotes if quote]
        if not quotes:
            continue
        seen = True
        if not any("NT$" in quote or "NT Dollar" in quote for quote in quotes):
            return False
    return seen


_ADR_USD_RE = re.compile(r"US\$\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)\s+per ADR", re.IGNORECASE)


def _statement_quarter(ticker: str, period: str) -> dict[str, Any] | None:
    if not ticker or not period:
        return None
    for row in _load_statement(ticker).get("quarters") or []:
        if row.get("period") == period:
            return row
    return None


def _doc_period(doc: dict[str, Any]) -> tuple[str, str]:
    meta = doc.get("meta") or {}
    return str(meta.get("ticker") or ""), str(meta.get("fiscal_period") or "")


def _quote_adr_usd(doc: dict[str, Any]) -> float | None:
    fin = doc.get("financials") or {}
    for key in ("eps_gaap", "eps_nongaap", "eps"):
        quote = str((fin.get(key) or {}).get("source_quote") or "")
        match = _ADR_USD_RE.search(quote)
        if match:
            return float(match.group(1).replace(",", ""))
    return None


def _disclosed_adr_usd(doc: dict[str, Any]) -> float | None:
    """新闻稿写明的 US$ per ADR。记分卡被币种守卫清空时，回看 statement history 里的同一句。"""
    quoted = _quote_adr_usd(doc)
    if quoted is not None:
        return quoted
    ticker, period = _doc_period(doc)
    row = _statement_quarter(ticker, period)
    if row and row.get("eps_adr_method") == "press_release":
        return _finite(row.get("eps_adr_usd"))
    return None


def _ntd_only_eps_quote(doc: dict[str, Any]) -> bool:
    fin = doc.get("financials") or {}
    quotes = [
        str((fin.get(key) or {}).get("source_quote") or "")
        for key in ("eps_gaap", "eps_nongaap", "eps")
    ]
    quotes = [quote for quote in quotes if quote]
    if not quotes:
        return False
    has_ntd = any("NT$" in quote or "NT Dollar" in quote for quote in quotes)
    has_usd = any(_ADR_USD_RE.search(quote) for quote in quotes)
    return has_ntd and not has_usd


def _statement_ordinary_ntd(doc: dict[str, Any]) -> float | None:
    ticker, period = _doc_period(doc)
    row = _statement_quarter(ticker, period) or {}
    return _finite(row.get("eps_ordinary_ntd"))


def _adr_eps_sentence(ticker: str, period: str) -> str:
    quarter = _statement_quarter(ticker, period) or {}
    ntd = quarter.get("eps_ordinary_ntd")
    adr_eps = quarter.get("eps_adr_usd") if quarter.get("eps_adr_method") == "press_release" else None
    if adr_eps is None:
        if ntd is None:
            return ""
        return f" 新闻稿没有写 US$ per ADR。NT${ntd} 不按平均汇率换算成美元，柱上留空。"
    ordinary = f"普通股为 NT${ntd}。" if ntd is not None else ""
    return (
        f" 柱上是新闻稿写明的 US${float(adr_eps):.2f} per ADR，不是把新台币按平均汇率换算。"
        f"{ordinary}记分卡若因只有台币引用被清空，仍用这句已披露的美元。"
    )


def _expect_beat_note(actual: float, consensus: float, *, money: bool) -> tuple[str, str]:
    from pipeline.compute.metrics import round_eps

    if money:
        diff = actual - consensus
        shown = _fmt_money(abs(diff))
    else:
        diff = round(actual - (round_eps(consensus) or consensus), 2)
        shown = f"${abs(diff):.2f}"
    if diff >= 0:
        return f"超预期 {shown}", "beat"
    return f"不及预期 {shown}", "miss"


def _tv_forward_rows(tv: dict[str, Any] | None, *, limit: int = 4, scale=None) -> list[dict[str, Any]]:
    """扫描文件里尚未公布的季度。EPS 优先；营收只有扫描写了才带上。

    不读全年 scalar，也不把 Alpha Vantage 的财年行拆成季度。两边都空的行丢掉。
    scale 为 None 时保持扫描原币。港币扫描要先换成报表币种，换不了的行丢掉。
    """
    if not isinstance(tv, dict):
        return []
    parsed: list[dict[str, Any]] = []
    rows = tv.get("future_quarters")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            period = _period_from_tv(row.get("tv_label"))
            if not period:
                continue
            parsed.append({
                "period": period,
                "eps": _apply_tv_scale(scale, period, _finite(row.get("eps_est"))),
                "revenue": _apply_tv_scale(scale, period, _finite(row.get("rev_est"))),
            })
    if not parsed:
        used = tv.get("rows_used") if isinstance(tv.get("rows_used"), dict) else {}
        earnings = used.get("earnings_fq_h_chart") or used.get("earnings_fq_h") or []
        revenues: dict[str, float | None] = {}
        for item in used.get("revenues_fq_h") or []:
            if not isinstance(item, dict):
                continue
            period = _period_from_tv(item.get("FiscalPeriod"))
            if period:
                revenues[period] = _finite(item.get("Estimate"))
        for item in earnings:
            if not isinstance(item, dict) or item.get("IsReported"):
                continue
            period = _period_from_tv(item.get("FiscalPeriod"))
            if not period:
                continue
            parsed.append({
                "period": period,
                "eps": _apply_tv_scale(scale, period, _finite(item.get("Estimate"))),
                "revenue": _apply_tv_scale(scale, period, revenues.get(period)),
            })
    parsed.sort(key=lambda row: row["period"])
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in parsed:
        if row["period"] in seen:
            continue
        seen.add(row["period"])
        if row["eps"] is None and row["revenue"] is None:
            continue
        deduped.append(row)
        if len(deduped) >= limit:
            break
    return deduped


def _page_is_latest(page: str, docs: dict[str, Any] | None) -> bool:
    periods = [key for key in (docs or {}) if _parse_year_quarter(key)]
    if not periods:
        return True
    return page == max(periods)


def _forward_periods_for_page(page: str, forward: list[dict[str, Any]], *, limit: int = 4) -> list[dict[str, Any]]:
    """本页之后、扫描里已经写明的未来季，最多 4 季。

    中间缺一季就跳过，不拿更远的季去填那个空位，也不用全年数字补。
    """
    if not page or not _parse_year_quarter(page):
        return []
    chosen: list[dict[str, Any]] = []
    for row in forward:
        if row["period"] <= page:
            continue
        chosen.append(row)
        if len(chosen) >= limit:
            break
    return chosen


def _reported_expect_chart(doc: dict[str, Any], docs: dict[str, dict[str, Any]] | None) -> dict[str, Any] | None:
    """没有 eps_surprise_chart 时，用已保存的实际和记分卡 / TradingView 共识画同一套柱图。

    本页之后的未来四季 EPS / 营收只取 tradingview_scan.json。缺的字段留空。
    共识对不上数量级就留空，并在说明里写缺一致预期。管理层指引只写在刻度下，不画成灰柱。
    """
    ticker = str((doc.get("meta") or {}).get("ticker") or "").upper()
    period = _page_period(doc)
    if not period or not _parse_year_quarter(period):
        return None
    if docs is None and ticker:
        docs = _load_docs(ticker)
    docs = docs or {}
    reported = [key for key in sorted(docs) if _parse_year_quarter(key) and key <= period]
    if period not in reported:
        reported.append(period)
        reported.sort()
    window = reported[-4:]
    nxt = next_fiscal_period(period)
    guide_eps = _finite((_scorecard_row(doc, "next_q_eps_guidance") or {}).get("actual"))
    guide_rev = _finite((_scorecard_row(doc, "next_q_revenue_guidance") or {}).get("actual"))
    tv = _tradingview(ticker) if ticker else None
    _fx_ctx, scale_tv = _tv_scaler(ticker, tv)
    forward_rows: list[dict[str, Any]] = []
    if _page_is_latest(period, docs):
        forward_rows = _forward_periods_for_page(period, _tv_forward_rows(tv, scale=scale_tv))
    forward_by = {row["period"]: row for row in forward_rows}

    def metric_actual(item: dict[str, Any], metric: str) -> float | None:
        if metric == "eps":
            disclosed = _disclosed_adr_usd(item)
            if disclosed is not None:
                return disclosed
            if _ntd_only_eps_quote(item):
                return None
            return _comparable_quarter_eps(item)
        return _finite(((item.get("financials") or {}).get("revenue") or {}).get("value"))

    def series(metric: str, guide: float | None) -> list[dict[str, Any]]:
        periods = list(window)
        next_doc = docs.get(nxt) or {}
        next_actual = metric_actual(next_doc, metric) if nxt in docs else None
        if nxt not in periods and (next_actual is not None or guide is not None):
            periods.append(nxt)
        for row in forward_rows:
            if row["period"] not in periods:
                periods.append(row["period"])
        quarters = []
        actuals: dict[str, float] = {}
        for key, item in docs.items():
            actual = metric_actual(item, metric)
            if actual is not None:
                actuals[key] = actual
        page_actual = metric_actual(doc, metric)
        if page_actual is not None:
            actuals.setdefault(period, page_actual)
        # 营收实际以季报为准。EPS 季报常缺 Q4（不做累计相减），扫描里同一数量级的已公布实际可以补。
        scan_actuals = _tv_reported_actuals(tv, metric, scale_tv) if metric == "eps" else {}
        filled_scan: set[str] = set()
        for key in periods:
            if key in forward_by or key in actuals:
                continue
            scanned = _scaled_scan_actual(key, scan_actuals, actuals)
            if scanned is None:
                continue
            actuals[key] = scanned
            filled_scan.add(key)
        for key in periods:
            item = docs.get(key) or (doc if key == period else {})
            actual = actuals.get(key)
            from_scan = key in filled_scan
            if key in forward_by:
                field = "eps" if metric == "eps" else "revenue"
                consensus = _aligned_consensus(actual, forward_by[key].get(field))
            else:
                consensus = _saved_consensus(item, metric, key, ticker, actual)
                if key == nxt and actual is None and consensus is None:
                    guide_metric = "next_q_eps_guidance" if metric == "eps" else "next_q_revenue_guidance"
                    consensus = _aligned_consensus(guide, _scorecard_consensus(doc, guide_metric))
            yoy = None
            if not from_scan:
                if metric == "eps":
                    yoy = _stored_eps_yoy(item)
                else:
                    yoy = _finite(((item.get("financials") or {}).get("revenue") or {}).get("yoy_pct"))
            if yoy is None:
                current = actual if actual is not None else (consensus if key in forward_by else None)
                prior_key = yoy_fiscal_period(key)
                base_saved = actuals.get(prior_key) not in (None, 0)
                base = actuals[prior_key] if base_saved else _scaled_scan_actual(prior_key, scan_actuals, actuals)
                # 两边都是季报时，高增长也照算。只要有一边来自扫描，数量级对不上（拆股前后）就不除。
                if (
                    current is not None
                    and base
                    and ((not from_scan and base_saved) or _same_scale(current, [base]))
                ):
                    yoy = current / abs(base) - 1
            guidance_only = actual is None and key == nxt and guide is not None and consensus is None
            forward_point = key in forward_by and actual is None
            if actual is not None and consensus is not None:
                note, note_cls = _expect_beat_note(actual, consensus, money=metric == "revenue")
            elif yoy is not None and actual is not None:
                change = _change(yoy)
                note, note_cls = f"同比 {change['text']}", "beat" if yoy > 0 else "miss" if yoy < 0 else "muted"
            elif forward_point and yoy is not None:
                change = _change(yoy)
                note, note_cls = f"同比 {change['text']}", "beat" if yoy > 0 else "miss" if yoy < 0 else "muted"
            elif guidance_only:
                fmt = "eps" if metric == "eps" else "revenue"
                note, note_cls = f"指引中值 {_fmt_value(guide, fmt)}", "muted"
            elif forward_point and consensus is not None:
                note, note_cls = "一致预期", "muted"
            else:
                note, note_cls = "缺一致预期", "muted"
            shown_actual = actual
            shown_consensus = consensus
            if metric == "revenue":
                shown_actual = None if actual is None else round(actual / 1_000_000_000, 2)
                shown_consensus = None if consensus is None else round(consensus / 1_000_000_000, 2)
            elif consensus is not None:
                from pipeline.compute.metrics import round_eps

                shown_consensus = round_eps(consensus)
            quarters.append(
                {
                    "label": _fq_label(key),
                    "consensus": shown_consensus,
                    "actual": shown_actual,
                    "yoy_pct": None if yoy is None else yoy * 100,
                    "note": note,
                    "note_cls": note_cls,
                    "basis": (
                        "扫描已公布"
                        if from_scan
                        else _eps_basis_label(item)
                        if metric == "eps" and actual is not None
                        else ("营收" if metric == "revenue" and actual is not None else ("营收指引" if guidance_only and metric == "revenue" else ""))
                    ),
                }
            )
        return quarters

    eps_quarters = series("eps", guide_eps)
    revenue_quarters = series("revenue", guide_rev)
    if not any(row["actual"] is not None or row["consensus"] is not None or "指引" in row["note"] for row in eps_quarters + revenue_quarters):
        return None

    def chart_note(quarters: list[dict[str, Any]], kind: str) -> str:
        if any(row.get("consensus") is not None for row in quarters):
            base = "深灰柱是已保存的一致预期，浅蓝柱是实际，橙线是同比。没有一致预期的季度不画灰柱。"
        elif kind == "revenue":
            base = "缺一致预期。浅蓝柱是已保存的营收，灰柱留空。下一季若只有管理层指引，写在刻度下，不画成一致预期。"
        else:
            base = "缺一致预期。浅蓝柱是已保存的实际，灰柱留空。"
        tv_fetched = ""
        if isinstance(tv, dict):
            tv_fetched = str(((tv.get("quote") or {}).get("fetched_at") or tv.get("fetched_at") or ""))
        cut = _source_cutoff("TradingView scanner", tv_fetched) if tv_fetched else ""
        if cut and any(row.get("consensus") is not None for row in quarters):
            return f"{base} {cut}。"
        return base

    eps_note = chart_note(eps_quarters, "eps")
    payload: dict[str, Any] = {
        "quarters": eps_quarters,
        "note": eps_note,
        "sides": [],
        "hint": "深灰柱 = street 一致预期，浅蓝柱 = 实际，橙线 = 同比",
        "revenue": {
            "quarters": revenue_quarters,
            "horizons": [],
            "note": chart_note(revenue_quarters, "revenue"),
            "hint": "深灰柱 = street 一致预期，浅蓝柱 = 营收实际，橙线 = 同比",
            "sides": [],
        }
        if revenue_quarters
        else None,
    }
    adr_periods = [
        key
        for key in window
        if _disclosed_adr_usd(docs.get(key) or (doc if key == period else {})) is not None
    ]
    if adr_periods:
        extra = _adr_eps_sentence(ticker, period) if ticker else ""
        if extra:
            payload["note"] = f"{eps_note}{extra}"
    elif _eps_quotes_are_ntd(docs, window):
        payload["mark"] = "NT$"
        payload["unit"] = "新台币"
        by_label: dict[str, float] = {}
        for key, item in list(docs.items()) + [(period, doc)]:
            if not _parse_year_quarter(key):
                continue
            ntd = _statement_ordinary_ntd(item)
            if ntd is not None:
                by_label[_fq_label(key)] = ntd
        for row in payload["quarters"]:
            if row.get("actual") is None and row["label"] in by_label:
                row["actual"] = by_label[row["label"]]
                row["basis"] = "普通股新台币"
        payload["note"] = (
            f"{eps_note} 柱上是已保存的普通股新台币。新闻稿没有 US$ per ADR 时，不按平均汇率换算成美元。"
        )
    if ticker and not _fx_ctx.get("identity") and (_fx_ctx.get("spot") is not None or _fx_ctx.get("by_period")):
        fx_sentence = " TradingView 金额原为港币。已披露季按该季营收隐含汇率换回人民币，未披露季按行情日 HKDCNY。"
        payload["note"] = f"{payload['note']}{fx_sentence}"
        if _eps_basis_label(doc) == "GAAP":
            payload["note"] += " 扫描里的 EPS 接近非国际财务报告准则，柱上实际仍是报表摊薄每股盈利。"
        if payload.get("revenue"):
            payload["revenue"]["note"] = f"{payload['revenue']['note']}{fx_sentence}"
    return payload


def _overlay_tv_forward_eps(
    doc: dict[str, Any],
    quarters: list[dict[str, Any]],
    docs: dict[str, dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """已保存的 eps_surprise_chart 只留实际口径。未来四季的一致预期改读当前扫描。"""
    from pipeline.compute.metrics import round_eps

    ticker = str((doc.get("meta") or {}).get("ticker") or "").upper()
    page = _page_period(doc)
    if not ticker or not page:
        return quarters
    tv = _tradingview(ticker)
    _, scale_tv = _tv_scaler(ticker, tv)
    forward = _forward_periods_for_page(page, _tv_forward_rows(tv, scale=scale_tv))
    if not forward:
        return quarters
    if docs is None:
        docs = _load_docs(ticker)
    docs = docs or {}
    if not _page_is_latest(page, docs):
        return quarters
    wanted = {row["period"] for row in forward}
    kept: list[dict[str, Any]] = []
    for quarter in quarters:
        period = _label_to_period(quarter.get("label"))
        if quarter.get("actual") is None and period and period > page and period not in wanted:
            continue
        kept.append(quarter)
    quarters = kept
    by_label = {quarter["label"]: quarter for quarter in quarters}

    def base_for(period: str) -> tuple[float | None, bool]:
        """去年同季实际，以及它是不是扫描补上的。"""
        prior = yoy_fiscal_period(period)
        prev = by_label.get(_fq_label(prior))
        if prev and prev.get("actual") is not None:
            return float(prev["actual"]), False
        item = docs.get(prior) or {}
        ours = _comparable_quarter_eps(item) if item else None
        if ours is not None:
            return ours, False
        saved = {
            key: value
            for key, value in (
                (_label_to_period(quarter.get("label")), quarter.get("actual"))
                for quarter in quarters
            )
            if key and value is not None
        }
        for key, value in docs.items():
            eps = _comparable_quarter_eps(value)
            if eps is not None:
                saved.setdefault(key, eps)
        return _scaled_scan_actual(prior, _tv_reported_actuals(tv, "eps", scale_tv), saved), True

    for row in forward:
        label = _fq_label(row["period"])
        eps = row.get("eps")
        shown = round_eps(eps) if eps is not None else None
        target = by_label.get(label)
        if target is not None and target.get("actual") is not None:
            continue
        if target is None:
            target = {
                "label": label,
                "consensus": shown,
                "actual": None,
                "yoy_pct": None,
                "note": "",
                "note_cls": "muted",
                "basis": "",
            }
            quarters.append(target)
            by_label[label] = target
        else:
            target["consensus"] = shown
        base, base_from_scan = base_for(row["period"])
        if eps is not None and base and (not base_from_scan or _same_scale(eps, [base])):
            yoy = eps / abs(base) - 1
            target["yoy_pct"] = yoy * 100
            change = _change(yoy)
            target["note"] = f"同比 {change['text']}"
            target["note_cls"] = "beat" if yoy > 0 else "miss" if yoy < 0 else "muted"
            target["basis"] = target.get("basis") or "一致预期"
        elif eps is None:
            target["yoy_pct"] = None
            target["note"] = "缺一致预期"
            target["note_cls"] = "muted"
            target["basis"] = ""
        else:
            target["yoy_pct"] = None
            target["note"] = "一致预期"
            target["note_cls"] = "muted"
            target["basis"] = target.get("basis") or "一致预期"
    return quarters


def _eps_chart(doc: dict[str, Any], docs: dict[str, dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """有 street 预期图时沿用那份季度的实际数；未来四季 EPS 改读 TradingView 扫描。没有预期图时用已保存实际画同一套柱图。"""
    from pipeline.compute.metrics import round_eps

    raw = (doc.get("meta") or {}).get("eps_surprise_chart") or None
    if not raw or not raw.get("quarters"):
        return _reported_expect_chart(doc, docs)
    quarters = []
    for q in raw["quarters"]:
        consensus_raw = _num(q.get("consensus"))
        consensus = round_eps(consensus_raw) if consensus_raw is not None else None
        actual = _num(q.get("actual"))
        yoy = _num(q.get("yoy"))
        if q.get("reported") and actual is not None and consensus is not None:
            diff = round(actual - consensus, 2)
            if diff >= 0:
                note, note_cls = f"超预期 ${diff:.2f}", "beat"
            else:
                note, note_cls = f"不及预期 ${abs(diff):.2f}", "miss"
        elif q.get("reported"):
            note, note_cls = q.get("missing_actual_label") or "未公布剔除后", "muted"
        elif yoy is not None:
            note, note_cls = f"同比 {_change(yoy)['text']}", "beat" if yoy > 0 else "miss" if yoy < 0 else "muted"
        else:
            note, note_cls = q.get("missing_yoy_label") or "同比缺基数", "muted"
        quarters.append(
            {
                "label": q.get("label") or "",
                "consensus": consensus,
                "actual": actual,
                "yoy_pct": None if yoy is None else yoy * 100,
                "note": note,
                "note_cls": note_cls,
                "basis": q.get("basis_label") or "",
            }
        )
    quarters = _overlay_tv_forward_eps(doc, quarters, docs)
    def chip(g: dict[str, Any], *, strip_basis: bool = False) -> dict[str, str]:
        label = _chip_subject(g["label"]) if strip_basis else g["label"]
        text = f"{label} {g['detail']}" if g.get("detail") else label
        if g.get("has"):
            text += f" {g['gap']['text']}"
        return {"text": text, "cls": g["gap"]["cls"] if g.get("has") else "muted"}

    sides = []
    eps_sides = []
    for g in _gap_rows(doc):
        if "EPS" in g["label"]:
            eps_sides.append(chip(g, strip_basis=True))
        else:
            sides.append(chip(g))
    revenue = _revenue_expect(doc, docs, eps_sides)
    return {
        "quarters": quarters,
        "note": raw.get("note") or "",
        "sides": sides,
        "hint": "深灰柱 = street 一致预期，浅蓝柱 = Non-GAAP 实际，橙线 = 同比",
        "revenue": revenue,
    }


def _market_view(doc: dict[str, Any]) -> dict[str, Any]:
    """Stage3 已停用：次日股价反应与分析师修正不再展示。"""
    del doc
    return {
        "price": {"text": "—", "cls": "na", "detail": ""},
        "revision": None,
        "stance": None,
        "bench": {"symbol": "", "pct": None, "excess": None},
    }


def _quality_checks(doc: dict[str, Any], cfg: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """增长质量：通用层（settings.yaml quality.defaults）+ 公司层（股票配置 quality_checks）。

    规则全部在配置里，阈值和说明可按公司调整；缺数据的检查不显示。
    """
    defaults = (load_settings().get("quality") or {}).get("defaults") or []
    specs = merge_checks(defaults, (cfg or {}).get("quality_checks") or [])
    metrics = derived_metrics(doc)
    out = []
    for spec in specs:
        r = evaluate(spec, metrics)
        if r["status"] == "na":
            continue
        out.append({"status": r["status"], "name": r["label"], "text": r["text"], "layer": spec["layer"]})
    return out


THESIS_STATUS = {
    "strengthened": ("强化", "ok"),
    "unchanged": ("不变", ""),
    "weakened": ("削弱", "bad"),
}
TRIGGER_STATE = {
    "triggered": ("已触发", "bad"),
    "not_triggered": ("未触发", ""),
    "unknown": ("需人工判断", "muted"),
}


def _deadline_view(text: str | None, today: date) -> dict[str, str]:
    """证伪条件里写了日期（YYYY-MM-DD）就显示倒计时；过期提示重新评估。"""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text or "")
    if not m:
        return {"left": "", "cls": ""}
    days = (date(int(m.group(1)), int(m.group(2)), int(m.group(3))) - today).days
    if days < 0:
        return {"left": f"已过 {-days} 天", "cls": "warn"}
    return {"left": f"还有 {days} 天", "cls": ""}


def _quote_of(r: dict[str, Any] | None) -> str:
    return (r or {}).get("quote") or "" if r and not r.get("quote_unverified") else ""


def _position_view(ticker: str, doc: dict[str, Any], today: date) -> dict[str, Any] | None:
    """仓位卡：观察仓看问题和事实，等待仓 / 持仓看论点和买卖条件。"""
    pos = load_position(ticker)
    if not pos:
        return None
    review = doc.get("thesis_review") or {}
    th = {int(r.get("index", -1)): r for r in review.get("theses") or []}
    an = {int(r.get("index", -1)): r for r in review.get("answers") or []}
    tr = {r.get("name"): r for r in review.get("triggers") or []}

    theses = []
    for i, t in enumerate(pos["theses"]):
        r = th.get(i)
        word, cls = THESIS_STATUS.get((r or {}).get("status"), ("待评估", "muted"))
        if r and r.get("falsified"):
            word, cls = "已证伪", "bad"
        theses.append({**t, "status": word, "cls": cls, "evidence": (r or {}).get("evidence") or "",
                       "quote": _quote_of(r), "deadline": _deadline_view(t["falsify"], today)})
    triggers = []
    for t in pos["triggers"]:
        r = tr.get(t["name"])
        word, cls = TRIGGER_STATE.get((r or {}).get("state"), ("待评估", "muted"))
        # 买入 / 加仓触发是好事，卖出触发才是警示
        if r and r.get("state") == "triggered" and t["name"] != "卖出":
            cls = "ok"
        triggers.append({**t, "state": word, "cls": cls, "reason": (r or {}).get("reason") or ""})
    questions = []
    for i, q in enumerate(pos["questions"]):
        r = an.get(i)
        questions.append({"text": q, "answered": bool(r and r.get("answered")),
                          "answer": (r or {}).get("answer") or "", "quote": _quote_of(r)})

    # 观察仓的「本季事实」：管理层在问答里新给出的数字，直接来自已有数据，不调用 LLM
    facts = []
    if pos["stage"] == "watch":
        for q in (doc.get("qa") or {}).get("items") or []:
            for n in q.get("new_numbers") or []:
                if isinstance(n, dict) and n.get("number"):
                    facts.append({"number": n["number"], "meaning": n.get("meaning") or "", "who": q.get("analyst") or ""})
    counts = {w: sum(1 for x in theses if x["status"] == w) for w in ("强化", "不变", "削弱", "已证伪")}
    return {
        **{k: pos[k] for k in ("stage", "stage_label", "updated", "draft")},
        "theses": theses,
        "triggers": triggers,
        "questions": questions,
        "facts": facts[:6],
        "counts": counts,
        # 只认按仓位档案生成的评估（带 stage）；旧版论点评估不算，避免显示过期的评估日期
        "reviewed": bool(review.get("fingerprint") and review.get("stage")),
        "reviewed_at": (review.get("reviewed_at") or "")[:10],
        "concerns": review.get("new_concerns") or [],
    }


def _drivers_pending(doc: dict[str, Any]) -> str:
    """变动原因没生成时，原样用数据里的状态，不编一句原因。"""
    drivers = doc.get("drivers") or {}
    if drivers.get("metrics"):
        return ""
    status = str(drivers.get("status") or "").strip()
    if status:
        return status
    stage = (doc.get("status") or {}).get("stage")
    if stage == "stage1_done" and not (doc.get("meta") or {}).get("transcript_source"):
        return "待电话会文字稿"
    if stage == "stage1_done":
        return "变动原因尚未生成"
    return ""


def _verdict_chips(doc: dict[str, Any], gaps: list[dict[str, Any]],
                   checks: list[dict[str, str]], thesis: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """判断条上的状态标签：每个标签把一块内容压成一句，数据缺口也直说。"""
    chips: list[dict[str, str]] = []
    if thesis:
        if thesis["stage"] != "watch" and thesis["reviewed"]:
            c = thesis["counts"]
            parts = [f"{c[w]} {w}" for w in ("强化", "不变", "削弱", "已证伪") if c[w]]
            bad = c["削弱"] + c["已证伪"]
            chips.append({"text": "论点：" + " · ".join(parts), "cls": "bad" if bad > c["强化"] else "ok" if c["强化"] > bad else ""})
        for t in thesis["triggers"]:
            if t["state"] == "已触发":
                chips.append({"text": f"{t['name']}条件已触发", "cls": t["cls"]})
        if thesis["stage"] == "watch" and thesis["questions"]:
            n = sum(1 for q in thesis["questions"] if q["answered"])
            chips.append({"text": f"问题有答案 {n}/{len(thesis['questions'])}" if thesis["reviewed"] else "问题待回答", "cls": "muted"})
    cls_of = {"beat": "ok", "miss": "bad", "inline": "", "unknown": "warn"}
    for g in gaps:
        if g["has"]:
            subject = _chip_subject(g["label"])
            if g.get("verdict") in {"beat", "miss"}:
                pct = str((g.get("gap") or {}).get("text") or "").lstrip("+-")
                word = "beat" if g["verdict"] == "beat" else "miss"
                text = f"{subject} {word} {pct}".strip() if pct and pct != "—" else f"{subject} {word}"
            else:
                word = {"inline": "符合预期", "unknown": "待核对"}.get(g.get("verdict") or "", "")
                text = f"{subject} {word} {g['gap']['text']}".strip()
            chips.append({"text": text, "cls": cls_of.get(g["verdict"], "")})
    warns = [c for c in checks if c["status"] == "warn"]
    if warns:
        chips.append({"text": f"增长质量 {len(warns)} 条需注意", "cls": "warn"})
    missing = [_chip_subject(g["label"]) for g in gaps if not g["has"]]
    if missing:
        chips.append({"text": "缺一致预期：" + "、".join(missing), "cls": "muted"})
    if (doc.get("status") or {}).get("needs_review"):
        chips.append({"text": "需要人工复核", "cls": "bad"})
    return chips


# ---------------------------------------------------------------- 渲染


def _is_management_nongaap_eps(g: dict[str, Any]) -> bool:
    """只要管理层自己给出的 Non-GAAP / adjusted EPS。GAAP、营收和费用都不算。"""
    key = (g.get("metric_key") or "").strip().lower()
    if key not in {"eps", "eps_nongaap"}:
        return False
    basis = re.sub(r"[\s_\-]", "", (g.get("basis") or "").lower())
    if basis == "gaap":
        return False
    if key == "eps_nongaap":
        return True
    return "nongaap" in basis or "adjusted" in basis


def _eps_scale_ok(value: float | None) -> bool:
    return value is not None and abs(value) < 10000


def _eps_figure(g: dict[str, Any]) -> str | None:
    """管理层写明的每股数字或区间。没有数字、或只是方向性表述，就不显示。"""
    kind = g.get("type") or ""
    if kind in {"qualitative", "directional"} and g.get("mid") is None and not (g.get("low_raw") and g.get("high_raw")):
        return None
    key = g.get("metric_key") or "eps"
    low, high, mid = _num(g.get("low")), _num(g.get("high")), _num(g.get("mid"))
    if g.get("low_raw") and g.get("high_raw") and low is not None and high is not None and low != high:
        if _eps_scale_ok(low) and _eps_scale_ok(high):
            return f"{_fmt_guide(low, key)}{_RANGE}{_fmt_guide(high, key)}"
        return None
    if _eps_scale_ok(mid):
        return _fmt_guide(mid, key)
    return None


def _eps_source_names(g: dict[str, Any]) -> list[str]:
    """页头来源只写短名。新闻稿和电话会可以同时出现。"""
    names: list[str] = []
    src = g.get("source") or ""
    origin = g.get("guidance_origin") or ""
    if src == "press_release" or origin == "sec_exhibit_99_1":
        names.append("新闻稿")
    if src in _CALL_SOURCES or g.get("confirmed_by") in _CALL_SOURCES:
        names.append("电话会")
    return names


def _eps_source_caption(g: dict[str, Any]) -> str:
    """来源写回格子：新闻稿按 Exhibit 99.1，电话会单独标，并带上原文。"""
    from pipeline.extract.guidance import normalize_period

    src = g.get("source") or ""
    origin = g.get("guidance_origin") or ""
    if src == "press_release" or origin == "sec_exhibit_99_1":
        where = "新闻稿 Exhibit 99.1"
    elif src in _CALL_SOURCES:
        where = "电话会"
    else:
        where = _source_label(src)
    if g.get("confirmed_by") in _CALL_SOURCES and where != "电话会":
        where = f"{where} · 电话会"
    period = _period_label(normalize_period(g.get("period")))
    parts = [where, "Non-GAAP"]
    if period:
        parts.append(period)
    quote = " ".join(str(g.get("source_quote") or "").split())
    text = " · ".join(parts)
    if quote:
        text = f"{text}。原文：{quote}"
    return text


def _prefer_guidance_source(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not candidates:
        return None

    def rank(pair: tuple[int, dict[str, Any]]) -> tuple:
        i, g = pair
        return (
            0 if g.get("source") == "press_release" else 1,
            0 if g.get("guidance_origin") == "sec_exhibit_99_1" else 1,
            i,
        )

    return min(enumerate(candidates), key=rank)[1]


def _eps_cell(label: str, chosen: dict[str, Any] | None, looked_period: str | None) -> dict[str, Any]:
    """一格的展示值和来源记录。没有管理层数字时价值为「无」，来源留空。"""
    from pipeline.extract.guidance import normalize_period

    if chosen is None:
        return {
            "label": label,
            "value": "无",
            "sources": [],
            "source": "管理层未提供该 Non-GAAP EPS 数字。不用营收或费用倒推，也不采用分析师一致预期。",
            "record": {
                "basis": "non-GAAP",
                "provided": False,
                "source": None,
                "guidance_origin": None,
                "source_quote": None,
                "period": looked_period,
                "metric_key": None,
            },
        }
    return {
        "label": label,
        "value": _eps_figure(chosen) or "无",
        "sources": _eps_source_names(chosen),
        "source": _eps_source_caption(chosen),
        "record": {
            "basis": "non-GAAP",
            "provided": True,
            "source": chosen.get("source"),
            "guidance_origin": chosen.get("guidance_origin"),
            "source_quote": chosen.get("source_quote"),
            "period": normalize_period(chosen.get("period")),
            "metric_key": chosen.get("metric_key"),
        },
    }


def _management_eps_cells(doc: dict[str, Any], fiscal_period: str) -> list[dict[str, Any]]:
    """页头两格：下季度、年度的 Non-GAAP EPS 指引。只读 guidance.items 里管理层原文。"""
    from pipeline.extract.guidance import normalize_period

    items = (doc.get("guidance") or {}).get("items") or []
    usable = [g for g in items if _is_management_nongaap_eps(g) and _eps_figure(g)]

    m = re.fullmatch(r"FY(\d{4})Q([1-4])", fiscal_period or "")
    next_q = full_year = None
    if m:
        year, quarter = int(m.group(1)), int(m.group(2))
        next_q = f"FY{year + 1}Q1" if quarter == 4 else f"FY{year}Q{quarter + 1}"
        full_year = f"FY{year + 1}" if quarter == 4 else f"FY{year}"

    next_hit = _prefer_guidance_source(
        [g for g in usable if next_q and normalize_period(g.get("period")) == next_q]
    )

    by_year: dict[str, list[dict[str, Any]]] = {}
    for g in usable:
        period = normalize_period(g.get("period"))
        if re.fullmatch(r"FY\d{4}", period):
            by_year.setdefault(period, []).append(g)
    if full_year and full_year in by_year:
        year_hit = _prefer_guidance_source(by_year[full_year])
    elif len(by_year) == 1:
        year_hit = _prefer_guidance_source(next(iter(by_year.values())))
    else:
        year_hit = None

    return [
        _eps_cell("下季度 EPS 指引", next_hit, next_q),
        _eps_cell("年度 EPS 指引", year_hit, full_year),
    ]


def stamp_management_eps(doc: dict[str, Any], fiscal_period: str) -> dict[str, Any]:
    """把页头两格的来源写进 guidance.management_eps。数字仍只来自管理层 Non-GAAP 条目。"""
    cells = _management_eps_cells(doc, fiscal_period)
    record = {
        "basis": "non-GAAP",
        "next_quarter": {**cells[0]["record"], "display": cells[0]["value"]},
        "full_year": {**cells[1]["record"], "display": cells[1]["value"]},
    }
    doc.setdefault("guidance", {})["management_eps"] = record
    return record


def _finite(value: Any) -> float | None:
    """把缺失、NaN 和无穷当成没有这个数。"""
    number = _num(value)
    if number is None or number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _tradingview(ticker: str) -> dict[str, Any] | None:
    if not ticker:
        return None
    return _read_json(data_dir(ticker) / "consensus" / "tradingview_scan.json")


def _tv_columns(ticker: str) -> dict[str, Any]:
    tv = _tradingview(ticker) or {}
    cols = (tv.get("quote") or {}).get("columns") or {}
    return cols if isinstance(cols, dict) else {}


def _as_date(raw: str | None) -> date | None:
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(raw or ""))
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _period_from_tv(raw: str | None) -> str | None:
    match = re.fullmatch(r"(\d{4})-Q([1-4])", str(raw or ""))
    if not match:
        return None
    return f"FY{match.group(1)}Q{match.group(2)}"


def _parse_year_quarter(raw: str | None) -> tuple[int, int] | None:
    match = re.fullmatch(r"(\d{4})-Q([1-4])", str(raw or ""))
    if not match:
        match = re.fullmatch(r"FY(\d{4})Q([1-4])", str(raw or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _current_fiscal_year(day: date, fy_end_month: int) -> int:
    """财年 FY{N} 在结束月当月及之前仍叫 N；过了结束月就是下一年。"""
    if day.month > fy_end_month:
        return day.year + 1
    return day.year


def _months_remaining(day: date, fy_end_month: int) -> int:
    """距离本财年结束还有几个月，包含当前这个没过完的月。"""
    if day.month > fy_end_month:
        end_year = day.year + 1
    else:
        end_year = day.year
    return (end_year - day.year) * 12 + (fy_end_month - day.month) + 1


def _fy_estimate_label(year: int) -> str:
    return f"FY{year % 100:02d}E"


def _fmt_px(value: float, currency: str = "USD") -> str:
    return f"{_ccy_prefix(currency)}{value:.2f}"


def _fmt_cap(value: float, currency: str = "USD") -> str:
    return f"{_ccy_prefix(currency)}{value / 1_000_000_000:.1f}B"


def _latest_snapshot(ticker: str) -> dict[str, Any] | None:
    """最新快照。文件名是 UTC 日期，同日再写一份会加上 T 和时间。只解析名字最新、且能读的那一份。"""
    folder = data_dir(ticker) / "snapshots"
    if folder.is_dir():
        names = tuple(sorted(path.name for path in folder.glob("*.json")))
    else:
        names = ()

    def load() -> dict[str, Any] | None:
        for name in reversed(names):
            data = _read_json(folder / name)
            if data:
                return data
        return None

    return _memo("snapshot", str(folder), names, load)


def _price_quote(ticker: str, doc: dict[str, Any]) -> dict[str, Any]:
    """最新股价只读已经落盘的行情，不在页面上留手填框。"""
    try:
        cfg = load_ticker_config(ticker)
    except (FileNotFoundError, OSError):
        cfg = {}
    quote_ccy = price_currency(cfg)
    tv = _tradingview(ticker) or {}
    quote = tv.get("quote") or {}
    close = _finite((quote.get("columns") or {}).get("close"))
    attached = quote.get("fx") if isinstance(quote.get("fx"), dict) else None
    if close is not None:
        fetched = quote.get("fetched_at")
        as_of = _market_day(fetched, quote_ccy) if quote_ccy == "HKD" else _session_date(fetched)
        return {
            "price": close,
            "as_of": as_of,
            "origin": "tradingview",
            "currency": quote_ccy,
            "fx": attached,
        }
    reaction = doc.get("price_reaction") or {}
    after = _finite(reaction.get("close_after"))
    before = _finite(reaction.get("close_before"))
    if after is not None:
        return {
            "price": after,
            "as_of": str(reaction.get("after_date") or ""),
            "origin": "price_reaction",
            "currency": quote_ccy,
            "fx": None,
        }
    if before is not None:
        return {
            "price": before,
            "as_of": str(reaction.get("before_date") or ""),
            "origin": "price_reaction",
            "currency": quote_ccy,
            "fx": None,
        }
    return {"price": None, "as_of": "", "origin": "", "currency": quote_ccy}


def _diluted_shares(ticker: str, period: str) -> float | None:
    """新闻稿里的稀释加权股本。单位按抽取结果；没写单位且数字很小就按百万股。"""
    path = data_dir(ticker) / "raw" / period / "extracted_financials.json"
    node = (_read_json(path) or {}).get("diluted_shares") or {}
    if not isinstance(node, dict):
        return None
    raw = str(node.get("raw") or "").replace(",", "").strip()
    count = _finite(raw)
    if count is None:
        return None
    unit = str(node.get("unit") or "").lower()
    if "billion" in unit:
        return count * 1_000_000_000
    if "million" in unit or "百万" in unit:
        return count * 1_000_000
    if "thousand" in unit:
        return count * 1_000
    if count < 100_000:
        return count * 1_000_000
    return count


def _stored_shares(ticker: str, period: str | None = None) -> tuple[float | None, str]:
    """已经落盘的股本。当前季新闻稿优先，其次更早季、XBRL、股票配置。没有就不编。"""
    if period:
        current = _diluted_shares(ticker, period)
        if current:
            return current, "股价 × 稀释加权股本"
    root = data_dir(ticker) / "raw"
    if root.is_dir():
        names = sorted(
            (
                path.name
                for path in root.iterdir()
                if path.is_dir()
                and path.name != period
                and (path / "extracted_financials.json").is_file()
            ),
            reverse=True,
        )
        for name in names:
            older = _diluted_shares(ticker, name)
            if older:
                return older, "股价 × 已披露股本"
    activity = _read_json(data_dir(ticker) / "xbrl_activity.json") or {}
    rows = [
        row
        for row in (activity.get("diluted_shares") or [])
        if isinstance(row, dict) and _finite(row.get("value"))
    ]
    if rows:
        rows.sort(key=lambda row: str(row.get("end") or ""))
        value = _finite(rows[-1].get("value"))
        if value:
            return value, "股价 × 已披露股本"
    try:
        cfg = load_ticker_config(ticker)
    except (FileNotFoundError, OSError):
        cfg = {}
    for key in ("shares_outstanding", "diluted_shares", "shares"):
        value = _finite(cfg.get(key))
        if value and value >= 100_000:
            return value, "股价 × 已披露股本"
    return None, ""


_CAP_KEYS = ("market_cap_basic", "market_cap_calc", "market_cap")


def _market_cap_view(
    columns: dict[str, Any] | None,
    *,
    price: float | None,
    shares: float | None,
    currency: str,
    share_note: str = "股价 × 稀释加权股本",
) -> dict[str, Any]:
    """市值。扫描里的 market_cap_basic 优先；否则用股价乘已有股本。两边都没有就留空。"""
    columns = columns or {}
    cap = None
    for key in _CAP_KEYS:
        candidate = _finite(columns.get(key))
        if candidate is not None and candidate > 0:
            cap = candidate
            break
    ccy = str(columns.get("currency") or "").strip().upper()
    if ccy not in {"USD", "HKD", "CNY", "RMB"}:
        ccy = (currency or "USD").upper()
    if cap is not None:
        return {"value": _fmt_cap(cap, ccy), "sub": "TradingView", "vendor": "TradingView"}
    if price is not None and shares:
        return {
            "value": _fmt_cap(price * shares, currency or "USD"),
            "sub": share_note or "股价 × 已披露股本",
        }
    return {"value": "—", "sub": ""}


def _guidance_sub(cell: dict[str, Any]) -> str:
    parts = ["管理层 Non-GAAP"]
    for name in cell.get("sources") or []:
        if name not in parts:
            parts.append(name)
    return " · ".join(parts)


def _comparable_quarter_eps(doc: dict[str, Any]) -> float | None:
    """和盈利预期同一套可比每股收益。有官方 US$ per ADR 时用它，不用台币，也不按汇率换算。"""
    disclosed = _disclosed_adr_usd(doc)
    if disclosed is not None:
        return disclosed
    if _ntd_only_eps_quote(doc):
        return None
    for row in doc.get("scorecard") or []:
        if row.get("metric") == "eps":
            actual = _finite(row.get("actual"))
            if actual is not None:
                return actual
    fin = doc.get("financials") or {}
    nongaap = _finite((fin.get("eps_nongaap") or {}).get("value"))
    gaap = _finite((fin.get("eps_gaap") or {}).get("value"))
    if nongaap is not None:
        return nongaap
    return gaap


def _quarter_eps_cells(
    doc: dict[str, Any],
    docs: dict[str, dict[str, Any]] | None,
    tv: dict[str, Any] | None,
    scale=None,
) -> dict[tuple[int, int], dict[str, Any]]:
    """(财年, 季度) → 可比实际和一致预期。图表里的 Non-GAAP 实际优先于数据商 GAAP。

    图上已保存的共识视为报表币种，不缩放。扫描里的 Estimate 经 scale 换到报表币种；换不了就留空。
    """
    cells: dict[tuple[int, int], dict[str, Any]] = {}

    def slot(year: int, quarter: int) -> dict[str, Any]:
        return cells.setdefault((year, quarter), {"reported": False, "actual": None, "consensus": None})

    for quarter in ((doc.get("meta") or {}).get("eps_surprise_chart") or {}).get("quarters") or []:
        parsed = _parse_year_quarter(quarter.get("fiscal_period"))
        if not parsed:
            continue
        cell = slot(*parsed)
        actual = _finite(quarter.get("actual"))
        consensus = _finite(quarter.get("consensus"))
        if quarter.get("reported") and actual is not None:
            cell["reported"] = True
            cell["actual"] = actual
        if consensus is not None and cell["consensus"] is None:
            cell["consensus"] = consensus

    rows = ((tv or {}).get("rows_used") or {}).get("earnings_fq_h_chart") or []
    if not rows:
        rows = ((tv or {}).get("rows_used") or {}).get("earnings_fq_h") or []
    for quarter in rows:
        parsed = _parse_year_quarter(quarter.get("FiscalPeriod"))
        if not parsed:
            continue
        cell = slot(*parsed)
        period = f"FY{parsed[0]}Q{parsed[1]}"
        consensus = _apply_tv_scale(scale, period, _finite(quarter.get("Estimate")))
        if consensus is not None and cell["consensus"] is None:
            cell["consensus"] = consensus

    for period, item in (docs or {}).items():
        parsed = _parse_year_quarter(period)
        if not parsed:
            continue
        cell = slot(*parsed)
        if cell["actual"] is None:
            actual = _comparable_quarter_eps(item)
            if actual is not None:
                cell["reported"] = True
                cell["actual"] = actual
    return cells


def _sum_fiscal_year(cells: dict[tuple[int, int], dict[str, Any]], year: int) -> float | None:
    """四个季度都在才相加。已报告用可比实际，未报告用一致预期。"""
    parts: list[float] = []
    for quarter in range(1, 5):
        cell = cells.get((year, quarter))
        if not cell:
            return None
        if cell.get("reported") and cell.get("actual") is not None:
            parts.append(float(cell["actual"]))
        elif cell.get("consensus") is not None:
            parts.append(float(cell["consensus"]))
        else:
            return None
    return sum(parts)


def _annual_from_av(ticker: str) -> dict[str, Any]:
    """Alpha Vantage EARNINGS_ESTIMATES 的 fiscal year 行 → {years, fetched_at}。

    全年 EPS 只用 AV，不用 TradingView next_fy 拼。date 取财年结束日的年份。
    """
    from pipeline.sources.av_estimates import load_earnings_record

    record = load_earnings_record(ticker, fetch=False)
    years: dict[int, float] = {}
    for row in record.get("estimates") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("horizon") or "").strip().lower() != "fiscal year":
            continue
        day = _as_date(str(row.get("date") or ""))
        value = _finite(row.get("eps_estimate_average"))
        if day is None or value is None:
            continue
        years[day.year] = value
    return {"years": years, "prior": {}, "fetched_at": str(record.get("fetched_at") or ""), "source": "alpha_vantage"}


def _annual_consensus(ticker: str, fy_end_month: int) -> dict[str, Any]:
    """全年 EPS 共识：优先 Alpha Vantage fiscal year；没有再退回最新 yfinance 快照 0y/+1y。"""
    av = _annual_from_av(ticker)
    if av.get("years"):
        return av
    snap = _latest_snapshot(ticker) or {}
    years: dict[int, float] = {}
    prior: dict[int, float] = {}
    est = snap.get("earnings_estimate") or {}
    index = list(est.get("index") or [])
    columns = list(est.get("columns") or [])
    data = est.get("data") or []
    as_of = _as_date(str(snap.get("snapshot_at") or ""))
    if "avg" not in columns or as_of is None:
        return {"years": years, "prior": prior, "fetched_at": "", "source": ""}
    avg_at = columns.index("avg")
    ago_at = columns.index("yearAgoEps") if "yearAgoEps" in columns else None
    base = _current_fiscal_year(as_of, fy_end_month)
    for name, offset in (("0y", 0), ("+1y", 1), ("+2y", 2)):
        if name not in index:
            continue
        row = data[index.index(name)]
        if not isinstance(row, list) or avg_at >= len(row):
            continue
        value = _finite(row[avg_at])
        if value is not None:
            years[base + offset] = value
        if name == "0y" and ago_at is not None and ago_at < len(row):
            ago = _finite(row[ago_at])
            if ago is not None:
                prior[base - 1] = ago
    return {
        "years": years,
        "prior": prior,
        "fetched_at": str(snap.get("snapshot_at") or ""),
        "source": "yfinance_snapshot",
    }


def _choose_fy_eps(
    year: int,
    cells: dict[tuple[int, int], dict[str, Any]],
    annual: dict[str, Any],
) -> float | None:
    summed = _sum_fiscal_year(cells, year)
    if summed is not None:
        return summed
    found = (annual.get("years") or {}).get(year)
    if found is not None:
        return found
    return (annual.get("prior") or {}).get(year)


def _ttm_eps(
    doc: dict[str, Any],
    docs: dict[str, dict[str, Any]] | None,
    annual: dict[str, Any],
    period: str,
) -> float | None:
    """近四季可比每股收益。刚披露完 Q4、手头又没有四季时，用快照里的上一财年。"""
    reported: list[float] = []
    for quarter in ((doc.get("meta") or {}).get("eps_surprise_chart") or {}).get("quarters") or []:
        actual = _finite(quarter.get("actual"))
        if quarter.get("reported") and actual is not None:
            reported.append(actual)
    if len(reported) >= 4:
        return sum(reported[-4:])
    series: list[float] = []
    for key in sorted(docs or {}):
        actual = _comparable_quarter_eps((docs or {})[key])
        if actual is not None:
            series.append(actual)
    if len(series) >= 4:
        return sum(series[-4:])
    match = re.fullmatch(r"FY(\d{4})Q4", period or "")
    if match:
        return (annual.get("prior") or {}).get(int(match.group(1)))
    return None


def _forward_pe_view(
    ticker: str,
    doc: dict[str, Any],
    period: str,
    cfg: dict[str, Any],
    docs: dict[str, dict[str, Any]] | None = None,
    active: bool = True,
) -> dict[str, Any] | None:
    """估值位置的 Forward PE。时间是行，EPS 和 P/E 是列，股价取已保存的最新价。"""
    if not active:
        return None
    quote = _price_quote(ticker, doc)
    price = quote.get("price")
    fy_end = int(cfg.get("fiscal_year_end_month") or 12)
    as_of = _as_date(quote.get("as_of")) or datetime.now(BJ).date()
    if docs is None:
        docs = _load_docs(ticker)
    annual = _annual_consensus(ticker, fy_end)
    quote_ccy = price_currency(cfg)
    eps_ccy = eps_currency(cfg)
    tv = _tradingview(ticker)
    fx = None
    if quote_ccy != eps_ccy:
        from pipeline.sources.fx import rate_on

        attached = quote.get("fx") if isinstance(quote.get("fx"), dict) else None
        fx = rate_on(str(quote.get("as_of") or ""), attached=attached)
    fx_ctx, scale_tv = _tv_scaler(ticker, tv, fx=fx)
    cells = _quarter_eps_cells(doc, docs, tv, scale=scale_tv)
    match = re.fullmatch(r"FY(\d{4})Q[1-4]", period or "")
    anchor = int(match.group(1)) if match else _current_fiscal_year(as_of, fy_end)
    fy_values: dict[int, float] = {}
    for year in range(anchor, anchor + 3):
        value = _choose_fy_eps(year, cells, annual)
        if value is not None:
            fy_values[year] = value
    current = _current_fiscal_year(as_of, fy_end)
    for year in (current, current + 1):
        if year not in fy_values:
            value = _choose_fy_eps(year, cells, annual)
            if value is not None:
                fy_values[year] = value
    months = _months_remaining(as_of, fy_end)
    ntm = None
    if current in fy_values and (current + 1) in fy_values and 0 < months <= 12:
        weight = months / 12
        ntm = weight * fy_values[current] + (1 - weight) * fy_values[current + 1]
    ttm = _ttm_eps(doc, docs, annual, period)

    def row(label: str, kind: str, eps: float) -> dict[str, Any]:
        if price is None:
            return {
                "label": label,
                "kind": kind,
                "eps": f"{_ccy_prefix(eps_ccy)}{eps:.2f}",
                "pe": "待核对",
                "fx": "",
                "eps_raw": eps,
                "pe_raw": None,
            }
        multiple = pe_multiple(price, eps, price_ccy=quote_ccy, eps_ccy=eps_ccy, fx=fx)
        return {
            "label": label,
            "kind": kind,
            "eps": f"{_ccy_prefix(eps_ccy)}{eps:.2f}",
            "pe": multiple["text"],
            "fx": multiple["fx"],
            "eps_raw": eps,
            "pe_raw": multiple["pe"],
        }

    rows: list[dict[str, Any]] = []
    if ttm not in (None, 0):
        rows.append(row("近 4 季（TTM）", "ttm", ttm))
    if ntm not in (None, 0):
        rows.append(row("未来 12 个月（NTM）", "ntm", ntm))
    missing = []
    for year in range(anchor, anchor + 3):
        if year in fy_values and fy_values[year] not in (None, 0):
            rows.append(row(_fy_estimate_label(year), "fy", fy_values[year]))
        else:
            missing.append(_fy_estimate_label(year))
    note = (
        f"NTM 用当前财年与下一财年共识按剩余 {months} 个月加权。"
        "不足 4 个季度且缺少两个财年共识时不显示，避免把两个季度加总当成一年。"
    )
    if missing:
        note += " " + "、".join(missing) + " 没有全年共识，也凑不满四个季度，所以不显示。"
    if price is not None and quote_ccy != eps_ccy:
        if fx:
            source = f"，{fx['source']}" if fx.get("source") else ""
            note += f" 股价按 {fx['pair']} {float(fx['rate']):.4f}（{fx['date']}{source}）换成人民币后，再除以人民币 EPS。"
        else:
            note += " 股价是港币、EPS 是人民币。缺少当日 HKDCNY 时，P/E 显示待核对。"
    if not fx_ctx.get("identity"):
        if fx_ctx.get("spot") is not None or fx_ctx.get("by_period"):
            note += " TradingView 季度一致预期原为港币。已披露季按该季营收隐含汇率换回人民币，未披露季按行情日 HKDCNY，再与报表人民币实际相加。"
        else:
            note += " TradingView 季度一致预期是港币。缺少当日 HKDCNY，也没有能对齐的营收实际，这些预期不并进人民币 EPS。"
    if price is None:
        note = "股价未接入，P/E 显示待核对，不估算股价。" + note
    source_bits: list[str] = []
    tv_doc = _tradingview(ticker) or {}
    tv_fetched = ((tv_doc.get("quote") or {}).get("fetched_at") or tv_doc.get("fetched_at") or "")
    if tv_fetched:
        source_bits.append(_source_cutoff("TradingView scanner（季度 EPS / 股价）", tv_fetched))
    av_fetched = str(annual.get("fetched_at") or "")
    if annual.get("source") == "alpha_vantage" and av_fetched:
        source_bits.append(_source_cutoff("Alpha Vantage（全年 EPS）", av_fetched))
    elif annual.get("source") == "yfinance_snapshot" and av_fetched:
        source_bits.append(_source_cutoff("Yahoo 快照（全年 EPS 后备）", av_fetched))
    if source_bits:
        note += " " + "；".join(source_bits) + "。"
    return {
        "price": _fmt_px(price, quote_ccy) if price is not None else "未接入",
        "rows": rows,
        "note": note,
    }


def _shift_period(period: str, steps: int) -> str | None:
    match = re.fullmatch(r"FY(\d{4})Q([1-4])", period or "")
    if not match:
        return None
    index = int(match.group(1)) * 4 + (int(match.group(2)) - 1) + steps
    if index < 0:
        return None
    return f"FY{index // 4}Q{index % 4 + 1}"


def _snapshot_revenue_quarters(ticker: str, doc: dict[str, Any], period: str) -> dict[str, float]:
    """财报后的快照里，0q 是下一季，+1q 再往后一季。已有季度预期时不覆盖。"""
    snap = _latest_snapshot(ticker) or {}
    est = snap.get("revenue_estimate") or {}
    index = list(est.get("index") or [])
    columns = list(est.get("columns") or [])
    data = est.get("data") or []
    if "avg" not in columns:
        return {}
    avg_at = columns.index("avg")
    release = _as_date((doc.get("meta") or {}).get("release_at_utc"))
    as_of = _as_date(str(snap.get("snapshot_at") or ""))
    anchor = _shift_period(period, 1) if as_of and release and as_of >= release else period
    if not anchor:
        return {}
    out: dict[str, float] = {}
    for name, step in (("0q", 0), ("+1q", 1)):
        if name not in index:
            continue
        row = data[index.index(name)]
        if not isinstance(row, list) or avg_at >= len(row):
            continue
        value = _finite(row[avg_at])
        target = anchor if step == 0 else _shift_period(anchor, step)
        if target and value is not None:
            out[target] = value
    return out


def _annual_revenue(ticker: str, fy_end_month: int) -> dict[int, float]:
    snap = _latest_snapshot(ticker) or {}
    est = snap.get("revenue_estimate") or {}
    index = list(est.get("index") or [])
    columns = list(est.get("columns") or [])
    data = est.get("data") or []
    as_of = _as_date(str(snap.get("snapshot_at") or ""))
    if "avg" not in columns or as_of is None:
        return {}
    avg_at = columns.index("avg")
    base = _current_fiscal_year(as_of, fy_end_month)
    years: dict[int, float] = {}
    for name, offset in (("0y", 0), ("+1y", 1), ("+2y", 2)):
        if name not in index:
            continue
        row = data[index.index(name)]
        if not isinstance(row, list) or avg_at >= len(row):
            continue
        value = _finite(row[avg_at])
        if value is not None:
            years[base + offset] = value
    return years


def _revenue_expect(
    doc: dict[str, Any],
    docs: dict[str, dict[str, Any]] | None,
    eps_sides: list[dict[str, str]],
) -> dict[str, Any] | None:
    """和 EPS 图同一排季度的营收。未来四季只取扫描里的营收预期，没有就留空。"""
    raw_quarters = ((doc.get("meta") or {}).get("eps_surprise_chart") or {}).get("quarters") or []
    if not raw_quarters:
        return None
    ticker = str((doc.get("meta") or {}).get("ticker") or "").upper()
    if docs is None and ticker:
        docs = _load_docs(ticker)
    docs = docs or {}
    tv = _tradingview(ticker) if ticker else None
    _, scale_tv = _tv_scaler(ticker, tv)
    page = _page_period(doc)
    forward = _forward_periods_for_page(page, _tv_forward_rows(tv, scale=scale_tv)) if _page_is_latest(page, docs) else []
    forward_rev = {row["period"]: row.get("revenue") for row in forward}
    consensus: dict[str, float] = {}
    for item in ((tv or {}).get("rows_used") or {}).get("revenues_fq_h") or []:
        period = _period_from_tv(item.get("FiscalPeriod"))
        estimate = _apply_tv_scale(scale_tv, period, _finite(item.get("Estimate")))
        if period and estimate is not None:
            consensus[period] = estimate
    if ticker:
        for period_key, estimate in _snapshot_revenue_quarters(ticker, doc, _page_period(doc)).items():
            consensus.setdefault(period_key, estimate)
    actuals: dict[str, float] = {}
    yoys: dict[str, float] = {}
    for period, item in docs.items():
        fin = ((item.get("financials") or {}).get("revenue") or {})
        actual = _finite(fin.get("value"))
        yoy = _finite(fin.get("yoy_pct"))
        if actual is not None:
            actuals[period] = actual
        if yoy is not None:
            yoys[period] = yoy
        for score in item.get("scorecard") or []:
            if score.get("metric") != "revenue":
                continue
            bench = score.get("benchmark") or {}
            value = _finite(bench.get("value"))
            if bench.get("source") == "consensus" and value is not None:
                consensus.setdefault(period, value)
    quarters = []
    seen: set[str] = set()

    def revenue_row(label: str, actual: float | None, estimate: float | None, yoy: float | None, *, reported: bool, forward: bool) -> dict[str, Any]:
        if actual is not None and estimate is not None and reported:
            diff = actual - estimate
            if diff >= 0:
                note, note_cls = f"超预期 {_fmt_money(diff)}", "beat"
            else:
                note, note_cls = f"不及预期 {_fmt_money(abs(diff))}", "miss"
        elif yoy is not None:
            note = f"同比 {_change(yoy)['text']}"
            note_cls = "beat" if yoy > 0 else "miss" if yoy < 0 else "muted"
        elif forward and estimate is not None:
            note, note_cls = "一致预期", "muted"
        else:
            note, note_cls = "缺一致预期", "muted"
        return {
            "label": label,
            "consensus": None if estimate is None else round(estimate / 1_000_000_000, 2),
            "actual": None if actual is None else round(actual / 1_000_000_000, 2),
            "yoy_pct": None if yoy is None else yoy * 100,
            "note": note,
            "note_cls": note_cls,
            "basis": "营收" if actual is not None else "",
        }

    for quarter in raw_quarters:
        period = _period_from_tv(quarter.get("fiscal_period"))
        if not period:
            continue
        actual = actuals.get(period)
        if period in forward_rev:
            estimate = forward_rev[period]
        else:
            estimate = consensus.get(period)
        if actual is None and estimate is None and period not in forward_rev:
            continue
        yoy = yoys.get(period)
        if yoy is None:
            # 已公布季只用实际；前瞻季才用预期除以去年同季实际。没有实际就留空。
            numerator = actual if actual is not None else (estimate if period in forward_rev else None)
            base = actuals.get(yoy_fiscal_period(period))
            if numerator is not None and base:
                yoy = numerator / abs(base) - 1
        quarters.append(revenue_row(
            quarter.get("label") or _fq_label(period),
            actual,
            estimate,
            yoy,
            reported=bool(quarter.get("reported")),
            forward=period in forward_rev,
        ))
        seen.add(period)
    for row in forward:
        period_key = row["period"]
        if period_key in seen:
            continue
        estimate = row.get("revenue")
        actual = actuals.get(period_key)
        yoy = yoys.get(period_key)
        if yoy is None:
            numerator = actual if actual is not None else estimate
            base = actuals.get(yoy_fiscal_period(period_key))
            if numerator is not None and base:
                yoy = numerator / abs(base) - 1
        quarters.append(revenue_row(
            _fq_label(period_key),
            actual,
            estimate,
            yoy,
            reported=actual is not None,
            forward=True,
        ))
    if not quarters:
        return None
    horizons = _revenue_horizons(ticker, doc, docs, actuals, consensus) if ticker else []
    tv_doc = _tradingview(ticker) if ticker else None
    tv_fetched = ""
    if isinstance(tv_doc, dict):
        tv_fetched = str(((tv_doc.get("quote") or {}).get("fetched_at") or tv_doc.get("fetched_at") or ""))
    rev_note = (
        "浅蓝柱是公司营收，灰柱是已保存的一致预期（TradingView scanner 未公布季）。"
        "更早的季度没有灰柱。橙线是同比。"
    )
    if tv_fetched:
        rev_note += " " + _source_cutoff("TradingView scanner", tv_fetched) + "。"
    return {
        "quarters": quarters,
        "horizons": horizons,
        "note": rev_note,
        "hint": "深灰柱 = street 一致预期，浅蓝柱 = 营收实际，橙线 = 同比",
        "sides": eps_sides,
    }


def _page_period(doc: dict[str, Any]) -> str:
    return str((doc.get("meta") or {}).get("fiscal_period") or "")


def _revenue_horizons(
    ticker: str,
    doc: dict[str, Any],
    docs: dict[str, dict[str, Any]],
    actuals: dict[str, float],
    consensus: dict[str, float],
) -> list[dict[str, str]]:
    """和 Forward PE 同一套时间：TTM、按剩余月份加权的 NTM，以及凑得齐的财年。"""
    try:
        from pipeline.config import load_ticker_config

        fy_end = int((load_ticker_config(ticker) or {}).get("fiscal_year_end_month") or 12)
    except Exception:
        fy_end = 12
    period = _page_period(doc)
    ordered = [key for key in sorted(actuals) if key <= period]
    ttm = sum(actuals[key] for key in ordered[-4:]) if len(ordered) >= 4 else None
    annual = _annual_revenue(ticker, fy_end)
    cells: dict[tuple[int, int], dict[str, Any]] = {}

    def slot(year: int, quarter: int) -> dict[str, Any]:
        return cells.setdefault((year, quarter), {"reported": False, "actual": None, "consensus": None})

    for key, value in actuals.items():
        parsed = _parse_year_quarter(key)
        if parsed:
            cell = slot(*parsed)
            cell["reported"] = True
            cell["actual"] = value
    for key, value in consensus.items():
        parsed = _parse_year_quarter(key)
        if parsed and slot(*parsed)["consensus"] is None:
            slot(*parsed)["consensus"] = value
    match = re.fullmatch(r"FY(\d{4})Q[1-4]", period)
    anchor = int(match.group(1)) if match else None
    fy_values: dict[int, float] = {}
    if anchor is not None:
        for year in range(anchor, anchor + 3):
            summed = _sum_fiscal_year(cells, year)
            if summed is not None:
                fy_values[year] = summed
            elif year in annual:
                fy_values[year] = annual[year]
    quote = _price_quote(ticker, doc)
    as_of = _as_date(quote.get("as_of")) or _as_date(str((_latest_snapshot(ticker) or {}).get("snapshot_at") or ""))
    current = _current_fiscal_year(as_of, fy_end) if as_of else None
    ntm = None
    months = None
    if current is not None and current in fy_values and (current + 1) in fy_values and as_of is not None:
        months = _months_remaining(as_of, fy_end)
        if 0 < months <= 12:
            weight = months / 12
            ntm = weight * fy_values[current] + (1 - weight) * fy_values[current + 1]
    out = []
    if ttm:
        out.append({"label": "近 4 季（TTM）", "value": _fmt_money(ttm)})
    if ntm:
        out.append({"label": "未来 12 个月（NTM）", "value": _fmt_money(ntm)})
    if anchor is not None:
        for year in range(anchor, anchor + 3):
            if year in fy_values:
                out.append({"label": _fy_estimate_label(year), "value": _fmt_money(fy_values[year])})
    return out


def _header_quotes(ticker: str, doc: dict[str, Any], period: str, is_latest: bool) -> dict[str, Any] | None:
    """标题下的六格：股价、市值、RSI、股息率，以及管理层 Non-GAAP EPS 指引。"""
    if not is_latest:
        return None
    quote = _price_quote(ticker, doc)
    columns = _tv_columns(ticker)
    price = quote.get("price")
    quote_ccy = str(quote.get("currency") or "USD")
    items: list[dict[str, Any]] = []
    if price is not None:
        sub = f"{quote['as_of']} 收盘" if quote.get("as_of") else "最新收盘"
        items.append({
            "label": "股价",
            "value": _fmt_px(price, quote_ccy),
            "sub": sub,
            "vendor": "TradingView" if quote.get("origin") == "tradingview" else None,
        })
    else:
        items.append({"label": "股价", "value": "未接入", "sub": "行情未接入"})
    shares, share_note = _stored_shares(ticker, period)
    cap = _market_cap_view(
        columns,
        price=price if isinstance(price, (int, float)) else None,
        shares=shares,
        currency=quote_ccy,
        share_note=share_note,
    )
    cap_item = {"label": "市值", "value": cap["value"], "sub": cap["sub"]}
    if cap.get("vendor"):
        cap_item["vendor"] = cap["vendor"]
    items.append(cap_item)
    rsi = _finite(columns.get("RSI"))
    if rsi is not None:
        items.append({"label": "RSI", "value": f"{rsi:.1f}", "sub": "TradingView", "vendor": "TradingView"})
    else:
        items.append({"label": "RSI", "value": "未接入", "sub": "行情未接入"})
    dividend = _finite(columns.get("dividends_yield"))
    if dividend is not None:
        items.append({"label": "股息率", "value": f"{dividend:.2f}%", "sub": "TTM", "vendor": "TradingView"})
    else:
        items.append({"label": "股息率", "value": "未接入", "sub": "行情未接入"})
    for cell in _management_eps_cells(doc, period):
        shown = dict(cell)
        shown["sub"] = _guidance_sub(shown)
        items.append(shown)
    tv = _tradingview(ticker) or {}
    market_quote = tv.get("quote") or {}
    market = bool(market_quote.get("columns"))
    tv_fetched = market_quote.get("fetched_at") or tv.get("fetched_at") or ""
    as_of = _session_date(tv_fetched) if market else ""
    if not as_of:
        as_of = _session_date(((doc.get("meta") or {}).get("release_at_utc")))
    line = _quote_line(items, as_of)
    # 行情盒子额外标 TradingView scanner 截止时间（PT），有 fetched_at 才写
    tv_cut = _source_cutoff("TradingView scanner", tv_fetched) if market and tv_fetched else ""
    if tv_cut:
        line = f"{line} · {tv_cut}" if line else tv_cut
    return {"items": items, "line": line, "market": market, "tv_cutoff": tv_cut}


_QUOTE_SOURCE_ORDER = ("TradingView", "Alpha Vantage", "新闻稿", "电话会")


def _market_day(raw: str | None, quote_ccy: str) -> str:
    """港股行情日用香港日历。美股仍走 _session_date。"""
    if (quote_ccy or "").upper() != "HKD":
        return _session_date(raw)
    if not raw:
        return ""
    text = str(raw).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        match = re.match(r"(\d{4}-\d{2}-\d{2})", text)
        return match.group(1) if match else ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BJ).strftime("%Y-%m-%d")



def _cutoff_pt(raw: str | None) -> str:
    """把 ISO fetched_at 写成 Ray 时区 PT：YYYY-MM-DD HH:mm PT。没有时间戳就空串，不编造。"""
    if not raw:
        return ""
    text = str(raw).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(PT)
    return local.strftime("%Y-%m-%d %H:%M PT")


def _source_cutoff(label: str, raw: str | None) -> str:
    """例如：TradingView scanner · 截止 2026-10-23 05:25 PT"""
    stamp = _cutoff_pt(raw)
    if not stamp:
        return label
    return f"{label} · 截止 {stamp}"

def _session_date(raw: str | None) -> str:
    """行情时点写成美东交易日。开盘前取到的是上一交易日收盘。"""
    if not raw:
        return ""
    text = str(raw).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        match = re.match(r"(\d{4}-\d{2}-\d{2})", text)
        return match.group(1) if match else ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    local = dt.astimezone(ET)
    if local.hour < 9 or (local.hour == 9 and local.minute < 30):
        local -= timedelta(days=1)
    while local.weekday() >= 5:
        local -= timedelta(days=1)
    return local.strftime("%Y-%m-%d")


def _quote_line(items: list[dict[str, Any]], as_of: str) -> str:
    """盒子底部一行：数据时间和实际用到的来源，不写解释。"""
    found: list[str] = []
    for item in items:
        names = list(item.get("sources") or [])
        vendor = item.get("vendor")
        if vendor:
            names.append(vendor)
        for name in names:
            if name not in found:
                found.append(name)
    found.sort(key=lambda name: _QUOTE_SOURCE_ORDER.index(name) if name in _QUOTE_SOURCE_ORDER else len(_QUOTE_SOURCE_ORDER))
    parts = []
    if as_of:
        parts.append(f"数据时间 {as_of}")
    if found:
        parts.append("来源 " + " / ".join(found))
    return " · ".join(parts)


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(ROOT / "builder" / "templates")),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["money"] = _fmt_money
    env.filters["pct"] = _fmt_pct
    env.filters["verdict"] = _verdict_label
    env.filters["label"] = _metric_label
    env.filters["guide"] = _fmt_guide
    env.filters["dedupe"] = _dedupe
    env.filters["stage"] = _stage_label
    env.filters["source"] = _source_label
    env.filters["direction"] = _direction_label
    env.filters["directness"] = _directness_label
    env.filters["tone"] = _tone_label
    env.filters["period_zh"] = _period_zh
    env.filters["warn_zh"] = _humanize_warning
    env.filters["qa_lines"] = _qa_answer_lines
    env.policies["json.dumps_kwargs"] = {"sort_keys": True, "ensure_ascii": False}
    return env


def _copy_static(out_dir: Path) -> None:
    src = ROOT / "builder" / "static"
    dst = out_dir / "static"
    shutil.copytree(src, dst, dirs_exist_ok=True)


def _load_docs(ticker: str) -> dict[str, dict[str, Any]]:
    folder = data_dir(ticker)
    paths = tuple(sorted(folder.glob("FY*.json")))
    stamp = tuple(_file_stamp(path) for path in paths)

    def load() -> dict[str, dict[str, Any]]:
        loaded: dict[str, dict[str, Any]] = {}
        for path in paths:
            loaded[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        return loaded

    return _memo("docs", str(folder), stamp, load)


def _preload_ticker(ticker: str) -> None:
    """报表、分部和图表序列与财季无关，整只股票只算一次。快照只解析最新一份。"""
    _load_statement(ticker)
    _fin_series(ticker)
    _latest_snapshot(ticker)


def _write_history_files(stock_dir: Path, history: list[dict[str, Any]]) -> None:
    """全历史一份 JSON 给 fetch。history.js 内容相同，file:// 用 script src 读取。"""
    payload = json.dumps(history, ensure_ascii=False, sort_keys=True)
    (stock_dir / "history.json").write_text(payload + "\n", encoding="utf-8")
    script = payload.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    (stock_dir / "history.js").write_text("window.STOCK_HISTORY=" + script + ";\n", encoding="utf-8")


def _clear_history_files(stock_dir: Path) -> None:
    for name in ("history.json", "history.js"):
        path = stock_dir / name
        if path.is_file():
            path.unlink()


def build_site(ticker: str | None = None, out_dir: Path | None = None) -> list[Path]:
    env = _env()
    out_dir = Path(out_dir) if out_dir else site_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    _copy_static(out_dir)

    tickers = list_tickers()
    only = ticker.upper() if ticker else None
    if only and only not in tickers:
        tickers.append(only)
    # 导航和首页都按 持仓 → 等待仓 → 观察仓 排
    order = {"hold": 0, "wait": 1, "watch": 2}
    tickers.sort(key=lambda x: (order.get((load_position(x) or {}).get("stage", "watch"), 3), x))
    nav = [
        {
            "ticker": t,
            "href": f"stocks/{t}/index.html",
            "listing": str((load_ticker_config(t).get("listing") or "domestic")),
        }
        for t in tickers
    ]
    index_rows: list[dict[str, Any]] = []
    pages: list[Path] = []
    today = datetime.now(BJ).date()

    for t in tickers:
        cfg = load_ticker_config(t)
        ccy_token = _REPORTING_CCY.set(_reporting_currency(t))
        docs = _load_docs(t)
        periods = sorted(docs, reverse=True)
        latest = periods[0] if periods else None
        index_rows.append(_index_row(t, cfg, latest, docs.get(latest) if latest else None, today))
        if only is not None and t != only:
            continue
        stock_dir = out_dir / "stocks" / t
        stock_dir.mkdir(parents=True, exist_ok=True)
        if not periods:
            _clear_history_files(stock_dir)
            html = env.get_template("placeholder.html").render(
                ticker=t, company=cfg.get("name"), nav=nav, active=t, root="../../",
                thesis=_position_view(t, {}, today),
                listing=str(cfg.get("listing") or "domestic"),
                reporting_currency=str(cfg.get("reporting_currency") or ""),
                accounting_basis=str(cfg.get("accounting_basis") or ""),
            )
            (stock_dir / "index.html").write_text(html, encoding="utf-8")
            pages.append(stock_dir / "index.html")
            continue
        _preload_ticker(t)
        history = _history(docs)
        if len(history) >= 2:
            _write_history_files(stock_dir, history)
        else:
            _clear_history_files(stock_dir)
        for period in periods:
            doc = docs[period]
            prior = docs.get(prior_fiscal_period(period)) or {}
            gaps = _gap_rows(doc)
            checks = _quality_checks(doc, cfg)
            thesis = _position_view(t, doc, today)
            html = env.get_template("period.html").render(
                gaps=gaps,
                eps_chart=_eps_chart(doc, docs),
                checks=checks,
                thesis=thesis,
                chips=_verdict_chips(doc, gaps, checks, thesis),
                drivers_pending=_drivers_pending(doc),
                quotes=_header_quotes(t, doc, period, period == latest),
                forward_pe=_forward_pe_view(t, doc, period, cfg, docs, period == latest),
                root="../../",
                nav=nav,
                active=t,
                ticker=t,
                company=cfg.get("name"),
                listing=str(cfg.get("listing") or "domestic"),
                reporting_currency=str(cfg.get("reporting_currency") or ""),
                accounting_basis=str(cfg.get("accounting_basis") or ""),
                doc=doc,
                periods=periods,
                current_period=period,
                release=_fmt_release((doc.get("meta") or {}).get("release_at_utc")),
                history=history if len(history) >= 2 else [],
                history_derived=any(h.get("derived") for h in history),
                waiting_transcript=(doc.get("status") or {}).get("stage") == "stage1_done",
                findings=_findings(doc),
                fin_rows=(fin_rows := _fin_rows(doc, docs)),
                fin_view=_fin_panel(fin_rows, _fin_view(t, period)),
                guidance=_guide_section(doc, docs),
                qa=_qa_view(doc, bool((prior.get("qa") or {}).get("items"))),
                watchlist=_watchlist(doc),
                next_earnings=_next_earnings(t),
                warnings=[_humanize_warning(w) for w in _dedupe((doc.get("status") or {}).get("warnings")) if not _is_stage3_warning(w)],
                money_unit=_chart_units(cfg.get("reporting_currency"))[0],
                eps_unit=_chart_units(cfg.get("reporting_currency"))[1],
                eps_mark=_ccy_prefix(cfg.get("reporting_currency")),
            )
            path = stock_dir / f"{period}.html"
            path.write_text(html, encoding="utf-8")
            pages.append(path)
            if period == latest:
                (stock_dir / "index.html").write_text(html, encoding="utf-8")
                pages.append(stock_dir / "index.html")

    _REPORTING_CCY.set("USD")
    av_note = _pool_source_note(tickers, index_rows)
    index_html = env.get_template("index.html").render(
        rows=index_rows,
        kinds=_kind_filters(index_rows),
        as_of=_pool_as_of(tickers),
        av_note=av_note,
        nav=nav,
        active=None,
        root="",
    )
    index_path = out_dir / "index.html"
    index_path.write_text(index_html, encoding="utf-8")
    pages.append(index_path)
    return pages
