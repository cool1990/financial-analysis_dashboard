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


def _verdict_label(v: str | None) -> str:
    mapping = {"beat": "Beat", "miss": "Miss", "inline": "In-line", "unknown": "待核对", "": "—", None: "—"}
    return mapping.get(v or "", v or "—")


def build_site(ticker: str | None = None) -> list[Path]:
    env = Environment(
        loader=FileSystemLoader(str(ROOT / "builder" / "templates")),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["money"] = _fmt_money
    env.filters["pct"] = _fmt_pct
    env.filters["verdict"] = _verdict_label
    env.filters["tojson"] = lambda obj: json.dumps(obj, ensure_ascii=False)

    out_dir = site_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    # copy static
    static_src = ROOT / "builder" / "static"
    static_dst = out_dir / "static"
    static_dst.mkdir(parents=True, exist_ok=True)
    for f in static_src.glob("*"):
        (static_dst / f.name).write_bytes(f.read_bytes())

    tickers = [ticker.upper()] if ticker else list_tickers()
    index_rows: list[dict[str, Any]] = []
    pages: list[Path] = []

    for t in tickers:
        cfg = load_ticker_config(t)
        folder = data_dir(t)
        periods = sorted([p.stem for p in folder.glob("FY*.json")], reverse=True)
        latest = periods[0] if periods else None
        for period in periods:
            doc = json.loads((folder / f"{period}.json").read_text(encoding="utf-8"))
            # history series for charts
            history = []
            for hp in sorted(periods):
                hdoc = json.loads((folder / f"{hp}.json").read_text(encoding="utf-8"))
                fin = hdoc.get("financials") or {}
                history.append(
                    {
                        "period": hp,
                        "revenue": (fin.get("revenue") or {}).get("value"),
                        "revenue_yoy": (fin.get("revenue") or {}).get("yoy"),
                        "gross_margin": (fin.get("gross_margin_nongaap") or fin.get("gross_margin_gaap") or {}).get("value"),
                        "operating_margin": (fin.get("operating_margin_nongaap") or fin.get("operating_margin_gaap") or {}).get("value"),
                        "fcf": (fin.get("fcf") or {}).get("value"),
                    }
                )
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
                sc = {c["metric"]: c for c in doc.get("scorecard") or []}
                index_rows.append(
                    {
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
                )
        if not periods:
            index_rows.append(
                {
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
            )
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
