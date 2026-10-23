"""在 Actions 里拉取 EARNINGS_ESTIMATES，写入 data/{TICKER}/consensus/av_estimates.json。"""

from __future__ import annotations

import time
from typing import Any, Callable

from pipeline.commands.poll_cmd import next_earnings_date
from pipeline.compute.revision_signal import (
    current_quarter_target,
    format_current_quarter,
    pick_current_quarter_estimate,
)
from pipeline.config import data_dir, list_tickers, load_ticker_config
from pipeline.sources.alpha_vantage import alphavantage_api_key, fetch_earnings_estimates_payload
from pipeline.sources.av_estimates import _read_json, write_estimates_cache


def _rate_limited(reason: str) -> bool:
    text = (reason or "").lower()
    return any(token in text for token in ("frequency", "spread out", "rate limit", "thank you for using alpha vantage"))


def _target_date(symbol: str):
    try:
        cfg = load_ticker_config(symbol)
    except Exception:
        cfg = {}
    fye = int((cfg or {}).get("fiscal_year_end_month") or 12)
    tv = _read_json(data_dir(symbol) / "consensus" / "tradingview_scan.json") or {}
    return current_quarter_target(
        tv if isinstance(tv, dict) else {},
        fiscal_year_end_month=fye,
        yahoo_0q_date=next_earnings_date(symbol),
    )


def refresh_earnings_estimates(
    tickers: list[str] | None = None,
    *,
    get: Callable[..., Any] | None = None,
    pause_seconds: float = 13.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """忽略已有缓存，逐只请求并覆盖写入。两次请求之间暂停，避开每分钟 5 次的限额。"""
    names = [(ticker or "").upper() for ticker in (tickers or list_tickers())]
    if not alphavantage_api_key():
        return {
            "ok": False,
            "written": 0,
            "reason": "缺少 GitHub Secret ALPHAVANTAGE_KEY",
            "results": [],
        }
    results: list[dict[str, Any]] = []
    written = 0
    for index, symbol in enumerate(names):
        if index and pause_seconds:
            sleep(pause_seconds)
        body = fetch_earnings_estimates_payload(symbol, get=get)
        if body.get("status") != "ok" and _rate_limited(str(body.get("reason") or "")):
            sleep(max(pause_seconds, 20))
            body = fetch_earnings_estimates_payload(symbol, get=get)
        estimates = [row for row in body.get("estimates") or [] if isinstance(row, dict)]
        if body.get("status") != "ok" or not estimates:
            results.append({
                "ticker": symbol,
                "ok": False,
                "reason": str(body.get("reason") or "没有 estimates")[:180],
            })
            continue
        write_estimates_cache(symbol, estimates)
        written += 1
        target = _target_date(symbol)
        picked = pick_current_quarter_estimate(estimates, target)
        view = format_current_quarter(picked, None)
        results.append({
            "ticker": symbol,
            "ok": True,
            "rows": len(estimates),
            "target_date": target.isoformat() if target else "",
            "analysts": view["analysts"],
            "eps": view["eps"],
            "chg_30d": view["chg"]["text"],
            "revisions": view["revisions"],
        })
    return {
        "ok": written > 0,
        "written": written,
        "reason": "" if written else "没有写入任何缓存",
        "results": results,
    }
