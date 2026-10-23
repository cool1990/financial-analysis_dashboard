"""在 Actions 里拉取 EARNINGS_ESTIMATES，写入 data/{TICKER}/consensus/av_estimates.json。"""

from __future__ import annotations

import re
import time
from datetime import date, datetime
from typing import Any, Callable

from pipeline.compute.revision_signal import choose_current_estimate, format_current_quarter
from pipeline.config import data_dir, is_hk_listing, list_tickers, load_ticker_config
from pipeline.sources.alpha_vantage import alphavantage_api_key, fetch_earnings_estimates_payload
from pipeline.sources.av_estimates import cache_is_fresh, write_estimates_cache


def _rate_limited(reason: str) -> bool:
    text = (reason or "").lower()
    return any(token in text for token in ("frequency", "spread out", "rate limit", "thank you for using alpha vantage"))


def _reported_period(symbol: str) -> str | None:
    labels = [
        path.stem
        for path in data_dir(symbol).glob("FY*.json")
        if re.fullmatch(r"FY\d{4}Q[1-4]", path.stem)
    ]
    return max(labels) if labels else None


def _preview(symbol: str, estimates: list[dict[str, Any]], today: date) -> dict[str, Any]:
    try:
        cfg = load_ticker_config(symbol)
    except Exception:
        cfg = {}
    fye = int((cfg or {}).get("fiscal_year_end_month") or 12)
    picked = choose_current_estimate(
        estimates,
        reported_period=_reported_period(symbol),
        fiscal_year_end_month=fye,
        today=today,
    )
    return format_current_quarter(picked, None)


def refresh_earnings_estimates(
    tickers: list[str] | None = None,
    *,
    get: Callable[..., Any] | None = None,
    pause_seconds: float = 13.0,
    sleep: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """逐只请求并覆盖写入。fetched_at 不足 20 小时的跳过。两次请求之间暂停，避开每分钟 5 次的限额。"""
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
    preview_day = today or (now.date() if now else date.today())
    requested = 0
    for symbol in names:
        try:
            hk = is_hk_listing(load_ticker_config(symbol))
        except FileNotFoundError:
            hk = False
        if hk:
            results.append({
                "ticker": symbol,
                "ok": True,
                "skipped": True,
                "reason": "listing=hk，持仓阶段不做 AV 共识",
            })
            continue
        if cache_is_fresh(symbol, now=now):
            results.append({
                "ticker": symbol,
                "ok": True,
                "skipped": True,
                "reason": "缓存不足 20 小时",
            })
            continue
        if requested and pause_seconds:
            sleep(pause_seconds)
        requested += 1
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
        view = _preview(symbol, estimates, preview_day)
        results.append({
            "ticker": symbol,
            "ok": True,
            "rows": len(estimates),
            "analysts": view["analysts"],
            "eps": view["eps"],
            "chg_30d": view["chg"]["text"],
            "revisions": view["revisions"],
        })
    skipped = sum(1 for item in results if item.get("skipped"))
    failed = any(not item.get("ok") for item in results)
    if written:
        ok, reason = True, ""
    elif skipped and not failed:
        ok, reason = True, "缓存都还新鲜"
    else:
        ok, reason = False, "没有写入任何缓存"
    return {
        "ok": ok,
        "written": written,
        "reason": reason,
        "results": results,
    }
