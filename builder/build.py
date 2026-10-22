from __future__ import annotations

import json
import re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from pipeline import ROOT
from pipeline.compute.periods import prior_fiscal_period, yoy_fiscal_period
from pipeline.compute.derived import derived_metrics, evaluate, merge_checks
from pipeline.config import data_dir, list_tickers, load_position, load_settings, load_ticker_config, site_dir

ET = ZoneInfo("America/New_York")
BJ = ZoneInfo("Asia/Shanghai")


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


def _fmt_money(value: float | None) -> str:
    v = _num(value)
    if v is None:
        return "—"
    av = abs(v)
    sign = "-" if v < 0 else ""
    if av >= 1_000_000_000:
        return f"{sign}${av/1_000_000_000:.2f}B"
    if av >= 1_000_000:
        return f"{sign}${av/1_000_000:.2f}M"
    return f"{sign}${av:,.2f}"


def _is_ratio_key(key: str | None) -> bool:
    k = key or ""
    return "margin" in k or k.endswith("_rate")


def _fmt_value(value: float | None, key: str | None) -> str:
    """按指标类型格式化本季数值：利润率 → 87.0%，EPS → $33.42，其余金额。"""
    v = _num(value)
    if v is None:
        return "—"
    if _is_ratio_key(key):
        return f"{v*100:.1f}%" if abs(v) <= 1.5 else f"{v:.1f}%"
    if "eps" in (key or ""):
        return f"${v:.2f}"
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
        return f"${v:.2f}"
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
    "opex_gaap": "运营费用（GAAP）",
    "opex_nongaap": "运营费用（Non-GAAP）",
    "share_count": "稀释股本",
    "next_q_revenue_guidance": "下季营收指引",
    "next_q_eps_guidance": "下季每股收益指引",
}

STAGE_LABELS = {
    "stage1_done": "阶段1 · 新闻稿",
    "stage2_done": "阶段2 · 含电话会",
    "stage3_done": "阶段3 · 市场反应",
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
    ("revenue", "营收", False),
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


def _is_quant_guidance(g: dict[str, Any]) -> bool:
    return g.get("mid") is not None or g.get("low") is not None or g.get("high") is not None


# ---------------------------------------------------------------- 视图模型


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
    for key, drv in drivers.items():
        if key in used:
            continue
        sections.append(
            {
                "key": key,
                "label": _metric_label(key),
                "is_ratio": "margin" in (key or ""),
                "is_eps": "eps" in (key or ""),
                "metric": fin.get(key),
                "driver": drv,
            }
        )
    return sections


def _fin_rows(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """财务解读表：每行一个指标，展开后看变动原因与原文引用。"""
    rows = []
    for s in _metric_sections(doc):
        m = s["metric"] or {}
        bench = m.get("benchmark") or {}
        has_bench = bench.get("value") is not None
        drv = s["driver"] or {}
        rows.append(
            {
                "key": s["key"],
                "label": s["label"],
                "value": _fmt_value(m.get("value"), s["key"]),
                "yoy": _change(m.get("yoy_pp") if s["is_ratio"] else m.get("yoy_pct"), pp=s["is_ratio"]),
                "qoq": _change(m.get("qoq_pp") if s["is_ratio"] else m.get("qoq_pct"), pp=s["is_ratio"]),
                "vs": _change(bench.get("diff_pp") if s["is_ratio"] else bench.get("diff_pct"), pp=s["is_ratio"])
                if has_bench
                else {"text": "—", "cls": "na"},
                "vs_source": _source_label(bench.get("source")) if has_bench else "",
                "summary": drv.get("summary") or m.get("basis_note") or "",
                "drivers": drv.get("drivers") or [],
                "conflicts": drv.get("conflicts") or [],
                "from_press": m.get("comparatives_source") == "press_release",
            }
        )
    return rows


def _price_view(doc: dict[str, Any]) -> dict[str, Any]:
    pr = doc.get("price_reaction") or {}
    pct = _num(pr.get("next_day_pct"))
    if pct is None:
        stage = (doc.get("status") or {}).get("stage")
        return {"text": "—", "cls": "na", "detail": pr.get("note") or ("交易日数据不足" if stage == "stage3_done" else "等待阶段3")}
    detail = ""
    if pr.get("close_before") is not None and pr.get("close_after") is not None:
        detail = (
            f"{pr.get('before_date') or ''} ${float(pr['close_before']):.2f} → "
            f"{pr.get('after_date') or ''} ${float(pr['close_after']):.2f}"
        )
    return {"text": f"{pct*100:+.2f}%", "cls": "up" if pct > 0 else "down" if pct < 0 else "flat", "detail": detail}


# 指引按指标族合并：GAAP / Non-GAAP 两条并成一格
GUIDE_FAMILIES = [
    ("revenue", "营收", ("revenue",)),
    ("gross_margin", "毛利率", ("gross_margin_nongaap", "gross_margin_gaap", "gross_margin")),
    ("operating_margin", "营业利润率", ("operating_margin_nongaap", "operating_margin_gaap", "operating_margin")),
    ("opex", "运营费用", ("opex_nongaap", "opex_gaap")),
    ("eps", "每股收益", ("eps_nongaap", "eps_gaap", "eps")),
    ("share_count", "稀释股本", ("share_count",)),
    ("tax_rate", "税率", ("tax_rate",)),
]
_FAMILY_OF = {k: fam for fam, _label, keys in GUIDE_FAMILIES for k in keys}

# 指引展望只按这五档归类。某一档没有抽到的字段就不显示，不补写。
GUIDE_DIMENSIONS = [
    ("results", "业绩指标"),
    ("operating", "经营指标"),
    ("capex", "资本开支"),
    ("returns", "股东回报"),
    ("other", "其他"),
]
_FINANCIAL_GUIDE_KEYS = {
    "revenue",
    "eps",
    "eps_gaap",
    "eps_nongaap",
    "gross_margin",
    "gross_margin_gaap",
    "gross_margin_nongaap",
    "operating_margin",
    "operating_margin_gaap",
    "operating_margin_nongaap",
    "opex",
    "opex_gaap",
    "opex_nongaap",
    "tax_rate",
    "share_count",
    "fcf",
    "operating_cash_flow",
}
_CAPEX_RE = re.compile(r"资本开支|capital expenditure|\bcapex\b", re.I)
_RETURNS_RE = re.compile(
    r"回购|分红|股息|股东回报|buyback|repurchase|\bdividend\b|capital return|shareholder return|"
    r"拆股|无投票权|stock split|non-?voting",
    re.I,
)
_OPERATING_RE = re.compile(
    r"\bdau\b|\bmau\b|\barpu\b|impression|engagement|subscriber|shipment|出货|日活|月活|"
    r"广告展示|用户数|每用户|数十亿|位元|\bbits?\b|\bpric|\basp\b|定价|价格",
    re.I,
)
_RESULTS_RE = re.compile(
    r"营收|收入|利润率|毛利|每股收益|费用|税率|营业利润|亏损|业绩|"
    r"\brevenue\b|\bearnings\b|\bmargin\b|\beps\b|\bopex\b|tax rate",
    re.I,
)
def _guide_dimension(g: dict[str, Any]) -> str:
    """把一条已抽取的指引放进五档之一。没有对应字段的档不出现在页面上。"""
    key = (g.get("metric_key") or "").lower()
    blob = " ".join(
        str(g.get(name) or "")
        for name in ("metric_key", "metric_label", "statement", "source_quote")
    )
    if key == "capex" or _CAPEX_RE.search(blob):
        return "capex"
    if key in _FINANCIAL_GUIDE_KEYS or key in _FAMILY_OF:
        return "results"
    if _RETURNS_RE.search(blob):
        return "returns"
    if _OPERATING_RE.search(blob):
        return "operating"
    if _RESULTS_RE.search(blob):
        return "results"
    return "other"


def _short_statement(text: str) -> str:
    """去掉「管理层表示」这类开头，保留「预计 / 警告」等动词。"""
    t = (text or "").strip()
    return re.sub(r"^(管理层|公司|首席财务官|首席执行官|CFO|CEO)(表示|称|指出)?[，,：:]?\s*", "", t) or t


def _guidance_value(g: dict[str, Any]) -> tuple[str, str]:
    """(区间, 中值)。other 类没有统一单位，直接用原文，避免把「75%」显示成 $75.00。"""
    key = g.get("metric_key") or ""
    if key == "other" or key not in METRIC_LABELS:
        raw = g.get("point_raw") or " – ".join(x for x in (g.get("low_raw"), g.get("high_raw")) if x)
        if g.get("plus_minus_raw") and raw:
            raw = f"{raw} {g['plus_minus_raw']}"
        return (raw or "—", "")
    low, mid, high = g.get("low"), g.get("mid"), g.get("high")
    if low is not None and high is not None and low != high:
        return (f"{_fmt_guide(low, key)} – {_fmt_guide(high, key)}", _fmt_guide(mid, key))
    return ("", _fmt_guide(mid if mid is not None else low, key))


_RANGE = " \u2013 "
_LEVEL_KEYS = set(_FAMILY_OF) | {"capex", "tax_rate", "fcf", "operating_cash_flow"}
_TOPIC_TITLES = {
    "revenue": "营收",
    "gross_margin": "毛利率",
    "operating_margin": "营业利润率",
    "opex": "运营费用",
    "eps": "每股收益",
    "share_count": "稀释股本",
    "tax_rate": "税率",
    "capex": "资本开支",
    "rd": "研发支出",
    "rl_loss": "Reality Labs 亏损",
    "fx": "汇率",
    "oi": "营业利润",
    "split": "拆股",
    "muse_eff": "Muse 算力效率",
    "muse_users": "Muse 用户规模",
    "muse_arpu": "Muse 每用户收入",
    "muse_profit": "Muse 盈利",
    "ai_return": "AI 投资回报",
    "legal": "监管与诉讼",
    "rev_growth": "营收增长",
    "pricing": "定价",
}
_TOPIC_DIM = {
    "revenue": "results",
    "gross_margin": "results",
    "operating_margin": "results",
    "opex": "results",
    "eps": "results",
    "share_count": "results",
    "tax_rate": "results",
    "fcf": "results",
    "operating_cash_flow": "results",
    "rd": "results",
    "rl_loss": "results",
    "fx": "results",
    "oi": "results",
    "rev_growth": "results",
    "capex": "capex",
    "split": "returns",
    "muse_users": "operating",
    "muse_arpu": "operating",
    "pricing": "operating",
}
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


def _is_percent_item(g: dict[str, Any]) -> bool:
    key = g.get("metric_key") or ""
    raw = " ".join(str(g.get(name) or "") for name in ("low_raw", "high_raw", "point_raw"))
    return "margin" in key or "rate" in key or "%" in raw


def _looks_like_money(raw: str | None) -> bool:
    return bool(raw) and bool(re.search(r"\$|billion|million|亿", str(raw), re.I))


def _fmt_raw_money(raw: str | None) -> str | None:
    if not _looks_like_money(raw):
        return None
    from pipeline.extract.numbers import parse_number

    num = parse_number(str(raw))
    if num is None or abs(num) < 1_000_000:
        return None
    return _fmt_money(num)


def _fmt_pct_value(value: float) -> str:
    shown = value * 100 if abs(value) <= 1 else value
    if abs(shown - round(shown)) < 0.05:
        return f"{shown:.0f}%"
    text = f"{shown:.2f}".rstrip("0").rstrip(".")
    return f"{text}%"


def _is_delta(g: dict[str, Any]) -> bool:
    """「增加超过 10 亿美元」是变动额，不是费用水平。已给出上下限的区间仍是水平值。"""
    if g.get("low_raw") and g.get("high_raw"):
        return False
    blob = f"{g.get('statement') or ''} {g.get('metric_label') or ''} {g.get('point_raw') or ''}"
    return bool(re.search(r"增加|上升|提高|上调|削减|下降|减少|increase|decrease|higher by|lower by", blob, re.I))


def _approx(g: dict[str, Any]) -> bool:
    blob = f"{g.get('statement') or ''} {g.get('point_raw') or ''} {g.get('source_quote') or ''}"
    return bool(re.search(r"约|大约|around|approximately|about", blob, re.I))


def _range_text(g: dict[str, Any]) -> str | None:
    if not (g.get("low_raw") and g.get("high_raw")):
        return None
    low, high = g.get("low"), g.get("high")
    if low is None or high is None or low == high:
        return None
    if _is_percent_item(g):
        return f"{_fmt_pct_value(low)}{_RANGE}{_fmt_pct_value(high)}"
    if _looks_like_money(g.get("low_raw")) or abs(float(low)) >= 1_000_000:
        return f"{_fmt_money(low)}{_RANGE}{_fmt_money(high)}"
    return None


def _point_text(g: dict[str, Any]) -> str | None:
    """水平值。± 区间用中值；other 里的 10x、1% 不换成美元。"""
    if _is_delta(g):
        return None
    key = g.get("metric_key") or ""
    if g.get("plus_minus_raw") and g.get("mid") is not None and key in _LEVEL_KEYS:
        return _fmt_guide(g["mid"], key)
    if key in _LEVEL_KEYS and g.get("mid") is not None and not (g.get("low_raw") and g.get("high_raw")):
        if _is_percent_item(g):
            return _fmt_pct_value(float(g["mid"]))
        return _fmt_guide(g["mid"], key)
    money = _fmt_raw_money(g.get("point_raw"))
    if money:
        return ("约 " if _approx(g) else "") + money
    return None


def _delta_text(rows: list[dict[str, Any]]) -> str | None:
    for g in rows:
        if not _is_delta(g):
            continue
        money = None
        mid = g.get("mid")
        if mid is not None and abs(float(mid)) >= 1_000_000:
            money = _fmt_money(mid)
        else:
            money = _fmt_raw_money(g.get("point_raw"))
        if not money:
            continue
        blob = f"{g.get('statement') or ''} {g.get('point_raw') or ''}"
        if re.search(r"下降|减少|decrease|lower", blob, re.I):
            verb = "下降"
        elif re.search(r"超过|more than|over", blob, re.I):
            verb = "增加超过"
        else:
            verb = "增加"
        return f"{verb} {money}"
    return None


def _descriptive_raw(rows: list[dict[str, Any]]) -> str:
    found: list[str] = []
    for g in rows:
        raw = str(g.get("point_raw") or "").strip()
        if not raw or _looks_like_money(raw) or re.fullmatch(r"[\d.,]+%?", raw):
            continue
        found.append(raw)
    if not found:
        return ""
    return max(found, key=len)


def _compact_statement(text: str) -> str:
    """没有可用数字时，把已有中文表述收成一行。不新增原文里没有的数字。"""
    raw = text or ""
    if re.search(r"1\s*拆\s*2|2-for-1", raw, re.I):
        return "1 拆 2"
    higher = re.search(r"高于\s*(20\d{2})", raw)
    if higher:
        extra = "（剔除法律费用）" if "剔除" in raw and "法律" in raw else ""
        return f"高于 {higher.group(1)} 年{extra}"
    if re.search(r"强劲增长|strong overall revenue", raw, re.I):
        return "强劲增长"
    if re.search(r"数十亿|billions of people", raw, re.I):
        return "扩展到数十亿人"
    if re.search(r"每用户|revenue per user", raw, re.I):
        return "高于现有应用"
    t = _short_statement(raw)
    t = re.sub(r"^(预计|将|仍|警告|认为|计划)\s*", "", t)
    t = re.split(r"[。；]", t)[0].strip("，, ")
    if len(t) > 32:
        t = t[:32].rstrip("，, ") + "…"
    return t or "—"


def _is_textual(value: str) -> bool:
    if re.search(r"\$|%|×", value) and len(value) <= 32:
        return False
    if re.fullmatch(r"[\d.]+x", value, re.I):
        return False
    return True


def _best_statement(rows: list[dict[str, Any]]) -> str:
    def rank(g: dict[str, Any]) -> tuple:
        text = g.get("statement") or ""
        return (
            0 if g.get("source") == "press_release" else 1,
            0 if re.search(r"[\u4e00-\u9fff]", text) else 1,
            -len(text),
        )

    chosen = sorted(rows, key=rank)[0]
    return chosen.get("statement") or chosen.get("source_quote") or ""


def _guide_sources(rows: list[dict[str, Any]]) -> str:
    sources = {g.get("source") for g in rows if g.get("source")}
    for g in rows:
        if g.get("confirmed_by"):
            sources.add(g["confirmed_by"])
    has_press = "press_release" in sources
    has_call = bool(sources & _CALL_SOURCES)
    if has_press and has_call:
        return "新闻稿 · 电话会"
    if has_press:
        return "新闻稿"
    if has_call:
        return "电话会"
    return _source_label(next(iter(sources), None))


def _card_title(topic: str, period: str, rows: list[dict[str, Any]]) -> str:
    if topic == "opex" and re.fullmatch(r"FY\d{4}", period or ""):
        return "全年费用"
    if topic in _TOPIC_TITLES:
        return _TOPIC_TITLES[topic]
    for g in rows:
        label = (g.get("metric_label") or "").strip()
        if re.search(r"[\u4e00-\u9fff]", label):
            return label[:18]
    return (rows[0].get("metric_label") or "其他").strip() or "其他"


def _card_dimension(topic: str, rows: list[dict[str, Any]]) -> str:
    if topic in _TOPIC_DIM:
        return _TOPIC_DIM[topic]
    return _guide_dimension(rows[0])


def _guidance_view(doc: dict[str, Any]) -> dict[str, Any]:
    """每条不同的指引一张卡片：短标题 + 一行数字。同一指标的新闻稿和电话会并成一张。"""
    from pipeline.extract.guidance import guidance_topic, normalize_period

    items = (doc.get("guidance") or {}).get("items") or []
    basis = (doc.get("meta") or {}).get("eps_basis") or "non_gaap"
    std = [g for g in items if _is_quant_guidance(g) and (g.get("metric_key") or "") in _FAMILY_OF]
    quarters = [normalize_period(g.get("period")) for g in std]
    quarters = [q for q in quarters if re.fullmatch(r"FY\d{4}Q[1-4]", q)]
    next_q = max(set(quarters), key=quarters.count) if quarters else None

    sc = {c.get("metric"): c for c in doc.get("scorecard") or []}
    vs_map = {"revenue": sc.get("next_q_revenue_guidance"), "eps": sc.get("next_q_eps_guidance")}

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    order: list[tuple[str, str]] = []
    for g in items:
        key = (guidance_topic(g), normalize_period(g.get("period")))
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(g)

    dim_index = {key: i for i, (key, _label) in enumerate(GUIDE_DIMENSIONS)}
    fam_index = {fam: i for i, (fam, _label, _keys) in enumerate(GUIDE_FAMILIES)}
    cards: list[dict[str, Any]] = []
    for seen, key in enumerate(order):
        topic, period = key
        rows = grouped[key]
        preferred = "_gaap" if basis == "gaap" else "_nongaap"

        def _rank(g: dict[str, Any]) -> tuple:
            metric = g.get("metric_key") or ""
            return (
                0 if metric.endswith(preferred) else 1,
                0 if g.get("source") == "press_release" else 1,
                0 if g.get("low_raw") and g.get("high_raw") else 1,
            )

        ranked = sorted(rows, key=_rank)
        value = ""
        for g in ranked:
            value = _range_text(g) or ""
            if value:
                break
        if not value:
            for g in ranked:
                value = _point_text(g) or ""
                if value:
                    break
        if not value:
            value = _descriptive_raw(rows) or _delta_text(rows) or _compact_statement(_best_statement(rows))
        if re.fullmatch(r"10x", value or "", re.I) and any(
            re.search(r"超过|more than", f"{g.get('statement') or ''} {g.get('source_quote') or ''}", re.I) for g in rows
        ):
            value = "超过 10×"
        textual = _is_textual(value)

        sub: list[str] = []
        period_label = _period_label(period)
        if period_label:
            sub.append(period_label)
        primary_key = (ranked[0].get("metric_key") or "") if value and not textual else ""
        # ± 形式的主值是中值，区间放在副行，避免和显式 low–high 的卡片混成两种样式以外的第三种
        if any(g.get("plus_minus_raw") for g in rows):
            band = next((g for g in ranked if g.get("plus_minus_raw") and g.get("low") is not None and g.get("high") is not None and g["low"] != g["high"]), None)
            if band is not None:
                sub.append(f"{_fmt_guide(band['low'], band.get('metric_key'))}{_RANGE}{_fmt_guide(band['high'], band.get('metric_key'))}")
        seen_basis: set[str] = set()
        for g in rows:
            metric = g.get("metric_key") or ""
            if metric == primary_key or not (metric.endswith("_gaap") or metric.endswith("_nongaap")):
                continue
            tag = "GAAP" if metric.endswith("_gaap") else "Non-GAAP"
            if tag in seen_basis:
                continue
            alt = _point_text(g) or _range_text(g)
            if alt:
                seen_basis.add(tag)
                sub.append(f"{tag} {alt}")
        basis_tag = ""
        primary_metric = ranked[0].get("metric_key") or ""
        if primary_metric.endswith("_nongaap") and seen_basis:
            basis_tag = "Non-GAAP"
        elif primary_metric.endswith("_gaap") and seen_basis:
            basis_tag = "GAAP"

        vs = vs_map.get(topic) if period == next_q else None
        bench = (vs or {}).get("benchmark") or {}
        metric_for_bench = next((g.get("metric_key") for g in ranked if g.get("metric_key") in _FAMILY_OF), topic)
        cards.append(
            {
                "topic": topic,
                "label": _card_title(topic, period, rows),
                "value": value,
                "textual": textual,
                "source": _guide_sources(rows),
                "basis": basis_tag,
                "sub": " · ".join(sub),
                "vs": _change(bench.get("diff_pct")) if bench.get("value") is not None else None,
                "vs_label": (
                    f"一致预期 {_fmt_guide(bench.get('value'), metric_for_bench)}"
                    if bench.get("value") is not None
                    else ""
                ),
                "variant": any(g.get("call_variant") for g in rows),
                "_dim": _card_dimension(topic, rows),
                "_sort": (dim_index.get(_card_dimension(topic, rows), 9), fam_index.get(topic, 50), seen),
            }
        )

    cards.sort(key=lambda c: c["_sort"])
    buckets: dict[str, list[dict[str, Any]]] = {key: [] for key, _label in GUIDE_DIMENSIONS}
    for card in cards:
        dim = card.pop("_dim")
        card.pop("_sort", None)
        buckets[dim].append(card)
    dimensions = []
    for key, label in GUIDE_DIMENSIONS:
        dim_cards = buckets[key]
        if not dim_cards:
            continue
        dimensions.append({"key": key, "label": label, "cards": dim_cards})
    flat = [c for d in dimensions for c in d["cards"]]
    return {
        "next_q": _period_label(next_q) if next_q else "",
        "tiles": flat,
        "outlook": [],
        "outlook_count": len(flat),
        "card_count": len(flat),
        "dimensions": dimensions,
    }


def _revision_rows(doc: dict[str, Any]) -> dict[str, Any]:
    ar = (doc.get("guidance") or {}).get("analyst_revisions") or {}
    tm1 = ar.get("t_minus_1") or {}
    cq = ar.get("t_minus_1_current_quarter") or {}
    has = bool(cq) or tm1.get("eps") is not None or tm1.get("revenue") is not None
    rows = []
    for key, label in (("t_minus_1", "T-1 发布前"), ("t_plus_1", "T+1"), ("t_plus_3", "T+3"), ("t_plus_7", "T+7")):
        r = ar.get(key) or {}
        rows.append(
            {
                "label": label,
                "date": r.get("date") or "待补",
                "eps": f"${float(r['eps']):.2f}" if r.get("eps") is not None else "待补",
                "eps_chg": _change(r.get("eps_chg")),
                "rev": _fmt_money(r.get("revenue")) if r.get("revenue") is not None else "待补",
                "rev_chg": _change(r.get("revenue_chg")),
            }
        )
    return {
        "has": has,
        "rows": rows,
        "current_q": {
            "eps": f"${float(cq['eps']):.2f}" if cq.get("eps") is not None else "—",
            "rev": _fmt_money(cq.get("revenue")),
            "date": cq.get("date") or "—",
        }
        if cq
        else None,
        "notes": ar.get("status_notes") or [],
    }


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
    ("operating_margin", "operating_margin_nongaap", True),
    ("operating_margin_gaap", "operating_margin_gaap", True),
    ("operating_margin_nongaap", "operating_margin_nongaap", True),
    ("eps", "eps_gaap", False),
    ("eps_nongaap", "eps_nongaap", False),
    ("ocf", "operating_cash_flow", False),
    ("capex", "capex", False),
    ("fcf", "fcf", False),
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
                "operating_margin": pick(fin, "operating_margin_nongaap", "operating_margin_gaap", field=which),
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


def _index_row(t: str, cfg: dict[str, Any], period: str | None, doc: dict[str, Any] | None) -> dict[str, Any]:
    doc = doc or {}
    sc = {c["metric"]: c for c in doc.get("scorecard") or []}

    def chip(metric: str) -> dict[str, str]:
        row = sc.get(metric) or {}
        verdict = row.get("verdict") or ""
        surprise = _surprise_text(verdict, _num((row.get("benchmark") or {}).get("diff_pct")))
        if surprise:
            return {"verdict": verdict, "label": surprise}
        return {"verdict": verdict, "label": _verdict_label(verdict) if verdict else "—"}

    eps = chip("eps")

    guide = chip("next_q_eps_guidance")
    if (sc.get("next_q_eps_guidance") or {}).get("actual") is None:
        company = _company_revenue_guide(doc)
        if company:
            guide = {"verdict": "", "label": company}

    pos = load_position(t)
    return {
        "ticker": t,
        "name": cfg.get("name"),
        "pos_stage": (pos or {}).get("stage") or "watch",
        "period": period or "—",
        "revenue": chip("revenue"),
        "eps": eps,
        "guide": guide,
        "price": _price_view(doc) if doc else {"text": "—", "cls": "na", "detail": ""},
        "stage": _stage_label((doc.get("status") or {}).get("stage") if doc else "scheduled"),
        "headline": (doc.get("summary") or {}).get("headline") or "",
        "next_earnings": _next_earnings(t),
        "timing": "盘后" if (cfg.get("release_timing") or "amc") == "amc" else "盘前",
        "href": f"stocks/{t}/index.html",
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


def _eps_chart(doc: dict[str, Any]) -> dict[str, Any] | None:
    """META 预期差图：过去四季可比实际 + 未来四季 street 预期。没有这份数据时沿用横条。"""
    from pipeline.compute.metrics import round_eps

    raw = (doc.get("meta") or {}).get("eps_surprise_chart") or None
    if not raw or not raw.get("quarters"):
        return None
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
    sides = []
    for g in _gap_rows(doc):
        if "EPS" in g["label"]:
            continue
        text = f"{g['label']} {g['detail']}" if g.get("detail") else g["label"]
        if g.get("has"):
            text += f" {g['gap']['text']}"
        sides.append({"text": text, "cls": g["gap"]["cls"] if g.get("has") else "muted"})
    return {"quarters": quarters, "note": raw.get("note") or "", "sides": sides}


def _market_view(doc: dict[str, Any]) -> dict[str, Any]:
    """市场怎么判：次日股价 + 财报后下季一致预期的修正。两者同向才算市场认可。"""
    price = _price_view(doc)
    ar = (doc.get("guidance") or {}).get("analyst_revisions") or {}
    tm1 = ar.get("t_minus_1") or {}
    latest = None
    for key, label in (("t_plus_7", "T+7"), ("t_plus_3", "T+3"), ("t_plus_1", "T+1")):
        r = ar.get(key) or {}
        if r.get("eps_chg") is not None:
            latest = {"label": label, "chg": _change(r.get("eps_chg")), "raw": r.get("eps_chg"),
                      "detail": f"${float(tm1['eps']):.2f} → ${float(r['eps']):.2f}" if tm1.get("eps") is not None and r.get("eps") is not None else ""}
            break
    pr = doc.get("price_reaction") or {}
    excess = _num(pr.get("excess_pct"))
    bench = {
        "symbol": pr.get("benchmark") or "",
        "pct": _change(pr.get("benchmark_pct")) if pr.get("benchmark_pct") is not None else None,
        "excess": _change(excess) if excess is not None else None,
    }
    # 有对照指数时用超额涨跌判断，扣掉板块整体波动
    p = excess if excess is not None else _num(pr.get("next_day_pct"))
    rv = latest["raw"] if latest else None
    if p is None or rv is None:
        stance = None
    elif p > 0 and rv > 0:
        stance = {"text": "市场认可", "cls": "ok"}
    elif p < 0 and rv < 0:
        stance = {"text": "市场不认可", "cls": "bad"}
    else:
        stance = {"text": "市场分歧", "cls": "warn"}
    return {"price": price, "revision": latest, "stance": stance, "bench": bench}


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
        _eps_cell("下季度EPS指引", next_hit, next_q),
        _eps_cell("年度EPS指引", year_hit, full_year),
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


def _market_quotes(ticker: str) -> dict[str, Any] | None:
    """右上角四个行情指标。只读已保存的 TradingView scanner 列，缺列就留空。"""
    path = ROOT / "data" / ticker / "consensus" / "tradingview_scan.json"
    if not path.exists():
        return None
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    cols = (snap.get("quote") or {}).get("columns") or {}
    close = _num(cols.get("close"))
    ntm = _num(cols.get("eps_estimate_ntm"))
    div = _num(cols.get("dividends_yield"))
    rsi = _num(cols.get("RSI"))
    items: list[dict[str, str]] = []
    if ntm is not None:
        items.append({
            "label": "Forward EPS",
            "value": f"${ntm:.2f}",
            "vendor": "TradingView",
            "source": f"TradingView eps_estimate_ntm = {ntm:.6f}，未来 12 个月一致预期。下一财年是另一列，不放在这一格。",
        })
    if close is not None and ntm not in (None, 0):
        items.append({
            "label": "Forward PE",
            "value": f"{close / ntm:.1f}×",
            "vendor": "TradingView",
            "source": (
                f"close {close:.2f} ÷ eps_estimate_ntm {ntm:.6f}。"
                "scanner 的 price_earnings_forward 为空，这一格是两列相除，不是现成的远期市盈率。"
            ),
        })
    if div is not None:
        items.append({
            "label": "股息率（TTM）",
            "value": f"{div:.2f}%",
            "vendor": "TradingView",
            "source": (
                "全年普通股股息 dps_common_stock_prim_issue_fy $2.10 ÷ close，与 TradingView dividends_yield 一致。"
                "名为 dividend_yield_ttm 的列为空，这一格用全年股息除以收盘价。季度股息 dividends_per_share_fq = $0.525。"
            ),
        })
    if rsi is not None:
        items.append({
            "label": "RSI",
            "value": f"{rsi:.1f}",
            "vendor": "TradingView",
            "source": "TradingView 列 RSI。名为 RSI14 的列为空，页面不把周期写成 14。",
        })
    if not items:
        return None
    quote = snap.get("quote") or {}
    return {
        "items": items,
        "fetched_at": quote.get("fetched_at") or "",
        "note": quote.get("note") or "Alpha Vantage 没有调用：当前环境没有 API key。",
    }


_QUOTE_SOURCE_ORDER = ("TradingView", "Alpha Vantage", "新闻稿", "电话会")


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


def _header_quotes(ticker: str, doc: dict[str, Any], period: str, is_latest: bool) -> dict[str, Any] | None:
    """最新财季页头右上角：行情四格，再加上管理层 Non-GAAP EPS 指引两格。"""
    if not is_latest:
        return None
    market = _market_quotes(ticker)
    items = list((market or {}).get("items") or [])
    items.extend(_management_eps_cells(doc, period))
    if not items:
        return None
    as_of = _session_date((market or {}).get("fetched_at"))
    if not as_of:
        as_of = _session_date(((doc.get("meta") or {}).get("release_at_utc")))
    return {
        "items": items,
        "line": _quote_line(items, as_of),
        "market": bool(market),
    }


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
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("FY*.json"))}


def build_site(ticker: str | None = None) -> list[Path]:
    env = _env()
    out_dir = site_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    _copy_static(out_dir)

    tickers = list_tickers()
    only = ticker.upper() if ticker else None
    if only and only not in tickers:
        tickers.append(only)
    # 导航和首页都按 持仓 → 等待仓 → 观察仓 排
    order = {"hold": 0, "wait": 1, "watch": 2}
    tickers.sort(key=lambda x: (order.get((load_position(x) or {}).get("stage", "watch"), 3), x))
    nav = [{"ticker": t, "href": f"stocks/{t}/index.html"} for t in tickers]
    index_rows: list[dict[str, Any]] = []
    pages: list[Path] = []
    today = datetime.now(BJ).date()

    for t in tickers:
        cfg = load_ticker_config(t)
        docs = _load_docs(t)
        periods = sorted(docs, reverse=True)
        latest = periods[0] if periods else None
        index_rows.append(_index_row(t, cfg, latest, docs.get(latest) if latest else None))
        if only is not None and t != only:
            continue
        stock_dir = out_dir / "stocks" / t
        stock_dir.mkdir(parents=True, exist_ok=True)
        if not periods:
            html = env.get_template("placeholder.html").render(
                ticker=t, company=cfg.get("name"), nav=nav, active=t, root="../../",
                thesis=_position_view(t, {}, today),
            )
            (stock_dir / "index.html").write_text(html, encoding="utf-8")
            pages.append(stock_dir / "index.html")
            continue
        history = _history(docs)
        for period in periods:
            doc = docs[period]
            prior = docs.get(prior_fiscal_period(period)) or {}
            gaps = _gap_rows(doc)
            checks = _quality_checks(doc, cfg)
            thesis = _position_view(t, doc, today)
            html = env.get_template("period.html").render(
                gaps=gaps,
                eps_chart=_eps_chart(doc),
                checks=checks,
                thesis=thesis,
                chips=_verdict_chips(doc, gaps, checks, thesis),
                drivers_pending=_drivers_pending(doc),
                quotes=_header_quotes(t, doc, period, period == latest),
                root="../../",
                nav=nav,
                active=t,
                ticker=t,
                company=cfg.get("name"),
                doc=doc,
                periods=periods,
                current_period=period,
                release=_fmt_release((doc.get("meta") or {}).get("release_at_utc")),
                history=history if len(history) >= 2 else [],
                history_derived=any(h.get("derived") for h in history),
                waiting_transcript=(doc.get("status") or {}).get("stage") == "stage1_done",
                findings=_findings(doc),
                fin_rows=_fin_rows(doc),
                guidance=_guidance_view(doc),
                revisions=_revision_rows(doc),
                qa=_qa_view(doc, bool((prior.get("qa") or {}).get("items"))),
                watchlist=_watchlist(doc),
                next_earnings=_next_earnings(t),
                warnings=[_humanize_warning(w) for w in _dedupe((doc.get("status") or {}).get("warnings"))],
            )
            path = stock_dir / f"{period}.html"
            path.write_text(html, encoding="utf-8")
            pages.append(path)
            if period == latest:
                (stock_dir / "index.html").write_text(html, encoding="utf-8")
                pages.append(stock_dir / "index.html")

    groups = [
        {"label": label, "rows": [r for r in index_rows if r["pos_stage"] == key]}
        for key, label in (("hold", "持仓"), ("wait", "等待仓"), ("watch", "观察仓"))
    ]
    index_html = env.get_template("index.html").render(
        groups=[g for g in groups if g["rows"]], nav=nav, active=None, root=""
    )
    index_path = out_dir / "index.html"
    index_path.write_text(index_html, encoding="utf-8")
    pages.append(index_path)
    return pages
