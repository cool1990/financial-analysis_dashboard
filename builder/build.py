from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from pipeline import ROOT
from pipeline.compute.periods import prior_fiscal_period, yoy_fiscal_period
from pipeline.config import data_dir, list_tickers, load_ticker_config, site_dir

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
                "summary": drv.get("summary") or "",
                "drivers": drv.get("drivers") or [],
                "conflicts": drv.get("conflicts") or [],
                "from_press": m.get("comparatives_source") == "press_release",
            }
        )
    return rows


def _scorecard_rows(doc: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for row in doc.get("scorecard") or []:
        key = row.get("metric") or ""
        b = row.get("benchmark") if isinstance(row.get("benchmark"), dict) else {"value": row.get("benchmark")}
        is_ratio = _is_ratio_key(key)
        fmt_key = "eps" if "eps" in key else key
        rows.append(
            {
                "label": _metric_label(key),
                "basis": _source_label(row.get("basis")) if row.get("basis") else "",
                "actual": _fmt_value(row.get("actual"), fmt_key),
                "bench": _fmt_value(b.get("value"), fmt_key),
                "source": _source_label(b.get("source")) if b.get("value") is not None else "",
                "diff": _change(b.get("diff_pp") if is_ratio else b.get("diff_pct"), pp=is_ratio),
                "verdict": row.get("verdict") or "",
                "verdict_label": _verdict_label(row.get("verdict")) if row.get("verdict") else "—",
            }
        )
    return rows


def _key_numbers(doc: dict[str, Any]) -> list[dict[str, Any]]:
    fin = doc.get("financials") or {}
    basis = (doc.get("meta") or {}).get("eps_basis")
    eps_key = "eps_gaap" if basis == "gaap" else "eps_nongaap"
    margin_sfx = "gaap" if basis == "gaap" else "nongaap"
    picks = [
        ("revenue", "营收"),
        (eps_key if fin.get(eps_key) else "eps_gaap", "每股收益"),
        (f"gross_margin_{margin_sfx}" if fin.get(f"gross_margin_{margin_sfx}") else "gross_margin_gaap", "毛利率"),
        (
            f"operating_margin_{margin_sfx}" if fin.get(f"operating_margin_{margin_sfx}") else "operating_margin_gaap",
            "营业利润率",
        ),
        ("fcf", "自由现金流"),
    ]
    out = []
    for key, label in picks:
        m = fin.get(key)
        if not m or m.get("value") is None:
            continue
        ratio = _is_ratio_key(key)
        out.append(
            {
                "label": label,
                "value": _fmt_value(m.get("value"), key),
                "yoy": _change(m.get("yoy_pp") if ratio else m.get("yoy_pct"), pp=ratio),
                "qoq": _change(m.get("qoq_pp") if ratio else m.get("qoq_pct"), pp=ratio),
            }
        )
    return out


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


def _guidance_rows(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    quant, qual = [], []
    for g in items:
        key = g.get("metric_key") or ""
        label = _metric_label(key) if key in METRIC_LABELS else (g.get("metric_label") or "其他")
        base = {
            "label": label,
            "period": _period_zh(g.get("period")),
            "source": _source_label(g.get("source")),
            "confirmed": _source_label(g.get("confirmed_by")) if g.get("confirmed_by") else "",
            "statement": g.get("statement") or g.get("source_quote") or "",
            "quote": g.get("source_quote") if g.get("source_quote") and g.get("source_quote") != g.get("statement") else "",
        }
        if _is_quant_guidance(g):
            rng, mid = _guidance_value(g)
            base.update({"range": rng, "mid": mid, "basis": _source_label(g.get("basis"))})
            if g.get("call_variant"):
                cv = g["call_variant"]
                base["variant"] = f"电话会口径：{_guidance_value(cv)[1] or _guidance_value(cv)[0]}"
            quant.append(base)
        else:
            base.update({"direction": g.get("direction") or "", "direction_label": _direction_label(g.get("direction"))})
            # 中文标题：metric_label 多为英文原文，优先用中文指标名
            base["title"] = label if key in METRIC_LABELS else (g.get("metric_label") or "其他")
            qual.append(base)
    return quant, qual


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


def _qa_view(doc: dict[str, Any], has_prior_qa: bool) -> dict[str, Any]:
    qa = doc.get("qa") or {}
    items = []
    for q in qa.get("items") or []:
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


def _history(docs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """趋势图序列：季度 JSON 为准；缺失季度用新闻稿对比列（上季 / 去年同季）补点。"""

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
                "revenue_yoy": None,
                "gross_margin": pick(fin, "gross_margin_nongaap", "gross_margin_gaap", field=which),
                "operating_margin": pick(fin, "operating_margin_nongaap", "operating_margin_gaap", field=which),
                "fcf": pick(fin, "fcf", field=which),
                "derived": True,
            }
    for period, doc in docs.items():
        fin = doc.get("financials") or {}
        points[period] = {
            "period": period,
            "revenue": pick(fin, "revenue"),
            "revenue_yoy": pick(fin, "revenue", field="yoy_pct"),
            "gross_margin": pick(fin, "gross_margin_nongaap", "gross_margin_gaap"),
            "operating_margin": pick(fin, "operating_margin_nongaap", "operating_margin_gaap"),
            "fcf": pick(fin, "fcf"),
            "derived": False,
        }
    return [points[p] for p in sorted(points)]


def _next_earnings(ticker: str) -> str:
    try:
        from pipeline.commands.poll_cmd import next_earnings_date

        d = next_earnings_date(ticker)
    except Exception:
        d = None
    return d.isoformat() if d else ""


def _index_row(t: str, cfg: dict[str, Any], period: str | None, doc: dict[str, Any] | None) -> dict[str, Any]:
    doc = doc or {}
    sc = {c["metric"]: c for c in doc.get("scorecard") or []}

    def chip(metric: str) -> dict[str, str]:
        v = (sc.get(metric) or {}).get("verdict") or ""
        return {"verdict": v, "label": _verdict_label(v) if v else "—"}

    return {
        "ticker": t,
        "name": cfg.get("name"),
        "period": period or "—",
        "revenue": chip("revenue"),
        "eps": chip("eps"),
        "guide": chip("next_q_eps_guidance"),
        "price": _price_view(doc) if doc else {"text": "—", "cls": "na", "detail": ""},
        "stage": _stage_label((doc.get("status") or {}).get("stage") if doc else "scheduled"),
        "headline": (doc.get("summary") or {}).get("headline") or "",
        "next_earnings": _next_earnings(t),
        "timing": "盘后" if (cfg.get("release_timing") or "amc") == "amc" else "盘前",
        "href": f"stocks/{t}/index.html",
    }


# ---------------------------------------------------------------- 渲染


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
    nav = [{"ticker": t, "href": f"stocks/{t}/index.html"} for t in tickers]
    index_rows: list[dict[str, Any]] = []
    pages: list[Path] = []

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
                ticker=t, company=cfg.get("name"), nav=nav, active=t, root="../../"
            )
            (stock_dir / "index.html").write_text(html, encoding="utf-8")
            pages.append(stock_dir / "index.html")
            continue
        history = _history(docs)[-8:]
        for period in periods:
            doc = docs[period]
            prior = docs.get(prior_fiscal_period(period)) or {}
            quant, qual = _guidance_rows((doc.get("guidance") or {}).get("items") or [])
            html = env.get_template("period.html").render(
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
                scorecard=_scorecard_rows(doc),
                key_numbers=_key_numbers(doc),
                price=_price_view(doc),
                fin_rows=_fin_rows(doc),
                quant_guidance=quant,
                qual_guidance=qual,
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

    index_html = env.get_template("index.html").render(rows=index_rows, nav=nav, active=None, root="")
    index_path = out_dir / "index.html"
    index_path.write_text(index_html, encoding="utf-8")
    pages.append(index_path)
    return pages
