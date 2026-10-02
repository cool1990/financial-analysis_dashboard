from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from pipeline import ROOT
from pipeline.config import data_dir, list_tickers, load_ticker_config, site_dir


def _fmt_pct(value: float | None, pp: bool = False) -> str:
    if value is None:
        return "—"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "—"
    if pp:
        return f"{value*100:+.2f} 百分点" if abs(value) < 2 else f"{value:+.2f} 百分点"
    return f"{value*100:+.2f}%"


def _fmt_money(value: float | None) -> str:
    if value is None:
        return "—"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "—"
    av = abs(value)
    sign = "-" if value < 0 else ""
    if av >= 1_000_000_000:
        return f"{sign}${av/1_000_000_000:.2f}B"
    if av >= 1_000_000:
        return f"{sign}${av/1_000_000:.2f}M"
    return f"{sign}${av:,.2f}"


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

DIRECTION_LABELS = {
    "up": "看高",
    "down": "看低",
    "flat": "持平",
}

DIRECTNESS_LABELS = {
    "direct": "直接回答",
    "partial": "部分回答",
    "evasive": "回避",
}

TONE_LABELS = {
    "positive": "偏积极",
    "neutral": "中性",
    "cautious": "偏谨慎",
}

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
    if not d:
        return ""
    return DIRECTION_LABELS.get(d, d)


def _directness_label(d: str | None) -> str:
    return DIRECTNESS_LABELS.get(d or "", d or "—")


def _tone_label(t: str | None) -> str:
    return TONE_LABELS.get(t or "", t or "—")


def _fmt_guide(value: float | None, metric_key: str | None) -> str:
    """指引数值：利润率/税率类按百分比，EPS 按美元，股本按股数，其余按金额。"""
    if value is None:
        return "—"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "—"
    key = metric_key or ""
    if "margin" in key or "rate" in key:
        return f"{value*100:.2f}%" if abs(value) <= 1 else f"{value:.2f}%"
    if "eps" in key:
        return f"${value:.2f}"
    if "share" in key:
        return f"{value/1e9:.2f}B 股" if abs(value) >= 1e9 else f"{value/1e6:.1f}M 股"
    return _fmt_money(value)


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


def _verdict_label(v: str | None) -> str:
    mapping = {
        "beat": "超预期",
        "miss": "不及预期",
        "inline": "符合预期",
        "unknown": "待核对",
        "": "—",
        None: "—",
    }
    return mapping.get(v or "", v or "—")


def _period_zh(period: str | None) -> str:
    if not period:
        return "—"
    p = period.strip()
    repl = [
        (r"(?i)fiscal\s*(\d{4})", r"\1财年"),
        (r"(?i)q([1-4])\s*fiscal\s*(\d{4})", r"\2财年Q\1"),
        (r"(?i)first quarter fiscal\s*(\d{4})", r"\1财年第一季度"),
        (r"(?i)calendar\s*(\d{4})", r"\1日历年"),
        (r"(?i)through\s*(\d{4})", r"至\1年"),
        (r"(?i)beyond\s*(\d{4})", r"\1年以后"),
        (r"(?i)not specified|unspecified", "未指明期间"),
        (r"(?i)second half of next year", "明年下半年"),
        (r"(?i)second half of calendar\s*(\d{4})", r"\1日历年下半年"),
    ]
    out = p
    for pat, rep in repl:
        out = re.sub(pat, rep, out)
    return out


def _is_quant_guidance(g: dict[str, Any]) -> bool:
    return g.get("mid") is not None or g.get("low") is not None or g.get("high") is not None


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
                "label": label,
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


def _index_row(t: str, cfg: dict[str, Any], period: str, doc: dict[str, Any]) -> dict[str, Any]:
    sc = {c["metric"]: c for c in doc.get("scorecard") or []}
    return {
        "ticker": t,
        "name": cfg.get("name"),
        "period": period,
        "revenue_verdict": (sc.get("revenue") or {}).get("verdict"),
        "eps_verdict": (sc.get("eps") or {}).get("verdict"),
        "guide_verdict": (sc.get("next_q_eps_guidance") or {}).get("verdict"),
        "price": (doc.get("price_reaction") or {}).get("next_day_pct"),
        "stage": (doc.get("status") or {}).get("stage"),
        "href": f"stocks/{t}/index.html",
    }


def _scheduled_row(t: str, cfg: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticker": t,
        "name": cfg.get("name"),
        "period": "—",
        "revenue_verdict": "",
        "eps_verdict": "",
        "guide_verdict": "",
        "price": None,
        "stage": "scheduled",
        "href": f"stocks/{t}/index.html",
    }


def build_site(ticker: str | None = None) -> list[Path]:
    env = Environment(
        loader=FileSystemLoader(str(ROOT / "builder" / "templates")),
        autoescape=select_autoescape(["html", "xml"]),
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
    env.policies["json.dumps_kwargs"] = {"sort_keys": True, "ensure_ascii": False}

    out_dir = site_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    static_src = ROOT / "builder" / "static"
    static_dst = out_dir / "static"
    static_dst.mkdir(parents=True, exist_ok=True)
    for f in static_src.glob("*"):
        (static_dst / f.name).write_bytes(f.read_bytes())

    tickers = list_tickers()
    only = ticker.upper() if ticker else None
    if only and only not in tickers:
        tickers.append(only)
    index_rows: list[dict[str, Any]] = []
    pages: list[Path] = []

    for t in tickers:
        cfg = load_ticker_config(t)
        folder = data_dir(t)
        periods = sorted([p.stem for p in folder.glob("FY*.json")], reverse=True)
        latest = periods[0] if periods else None
        render_pages = only is None or t == only
        if not render_pages:
            if latest:
                doc = json.loads((folder / f"{latest}.json").read_text(encoding="utf-8"))
                index_rows.append(_index_row(t, cfg, latest, doc))
            else:
                index_rows.append(_scheduled_row(t, cfg))
            continue
        docs = {p: json.loads((folder / f"{p}.json").read_text(encoding="utf-8")) for p in periods}
        history = []
        for hp in sorted(periods):
            fin = docs[hp].get("financials") or {}
            history.append(
                {
                    "period": hp,
                    "revenue": (fin.get("revenue") or {}).get("value"),
                    "revenue_yoy": (fin.get("revenue") or {}).get("yoy_pct"),
                    "gross_margin": (fin.get("gross_margin_nongaap") or fin.get("gross_margin_gaap") or {}).get("value"),
                    "operating_margin": (fin.get("operating_margin_nongaap") or fin.get("operating_margin_gaap") or {}).get("value"),
                    "fcf": (fin.get("fcf") or {}).get("value"),
                }
            )
        for period in periods:
            doc = docs[period]
            guidance_items = (doc.get("guidance") or {}).get("items") or []
            html = env.get_template("period.html").render(
                ticker=t,
                company=cfg.get("name"),
                doc=doc,
                periods=periods,
                current_period=period,
                history=history[-8:],
                waiting_transcript=doc.get("status", {}).get("stage") == "stage1_done",
                metric_sections=_metric_sections(doc),
                quant_guidance=[g for g in guidance_items if _is_quant_guidance(g)],
                qual_guidance=[g for g in guidance_items if not _is_quant_guidance(g)],
                warnings=[_humanize_warning(w) for w in _dedupe(doc.get("status", {}).get("warnings"))],
            )
            stock_dir = out_dir / "stocks" / t
            stock_dir.mkdir(parents=True, exist_ok=True)
            path = stock_dir / f"{period}.html"
            path.write_text(html, encoding="utf-8")
            pages.append(path)
            if period == latest:
                (stock_dir / "index.html").write_text(html, encoding="utf-8")
                pages.append(stock_dir / "index.html")
                index_rows.append(_index_row(t, cfg, period, doc))
        if not periods:
            index_rows.append(_scheduled_row(t, cfg))
            stock_dir = out_dir / "stocks" / t
            stock_dir.mkdir(parents=True, exist_ok=True)
            placeholder = env.get_template("placeholder.html").render(ticker=t, company=cfg.get("name"))
            (stock_dir / "index.html").write_text(placeholder, encoding="utf-8")
            pages.append(stock_dir / "index.html")

    index_html = env.get_template("index.html").render(rows=index_rows)
    index_path = out_dir / "index.html"
    index_path.write_text(index_html, encoding="utf-8")
    pages.append(index_path)
    return pages
