from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from pipeline.config import data_dir, list_tickers, load_ticker_config, site_dir
from pipeline import ROOT


def _fmt_pct(value: float | None, pp: bool = False) -> str:
    if value is None:
        return "—"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "—"
    if pp:
        return f"{value*100:+.2f} pp" if abs(value) < 2 else f"{value:+.2f} pp"
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
    "gross_margin_gaap": "毛利率 GAAP",
    "gross_margin_nongaap": "毛利率 Non-GAAP",
    "operating_margin": "营业利润率",
    "operating_margin_gaap": "营业利润率 GAAP",
    "operating_margin_nongaap": "营业利润率 Non-GAAP",
    "eps": "EPS",
    "eps_gaap": "稀释 EPS GAAP",
    "eps_nongaap": "稀释 EPS Non-GAAP",
    "operating_cash_flow": "经营现金流",
    "capex": "资本开支",
    "fcf": "自由现金流",
    "opex_gaap": "运营费用 GAAP",
    "opex_nongaap": "运营费用 Non-GAAP",
    "share_count": "股本",
    "next_q_revenue_guidance": "下季营收指引",
    "next_q_eps_guidance": "下季 EPS 指引",
}


def _metric_label(key: str | None) -> str:
    return METRIC_LABELS.get(key or "", key or "—")


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


def _verdict_label(v: str | None) -> str:
    mapping = {"beat": "Beat", "miss": "Miss", "inline": "In-line", "unknown": "待核对", "": "—", None: "—"}
    return mapping.get(v or "", v or "—")


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
    # 用 Jinja 内置 tojson（返回 Markup 且对 <>&' 做 JS 安全转义）；
    # 自定义 json.dumps 会被 autoescape 成 &#34;，导致页面图表脚本语法错误
    env.policies["json.dumps_kwargs"] = {"sort_keys": True, "ensure_ascii": False}

    out_dir = site_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    # copy static
    static_src = ROOT / "builder" / "static"
    static_dst = out_dir / "static"
    static_dst.mkdir(parents=True, exist_ok=True)
    for f in static_src.glob("*"):
        (static_dst / f.name).write_bytes(f.read_bytes())

    # 首页始终列出全部股票；--ticker 只限制重新生成哪只股票的详情页
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
        # history series for charts
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
            html = env.get_template("period.html").render(
                ticker=t,
                company=cfg.get("name"),
                doc=doc,
                periods=periods,
                current_period=period,
                history=history[-8:],
                waiting_transcript=doc.get("status", {}).get("stage") == "stage1_done",
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
