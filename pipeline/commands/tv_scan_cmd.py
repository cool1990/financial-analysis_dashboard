"""CLI：扫 TradingView，写入 data/{TICKER}/consensus/tradingview_scan.json。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from pipeline.sources.tradingview_scan import refresh_quotes


def refresh_tv_quotes(
    tickers: list[str] | None = None,
    *,
    post: Callable[..., Any] | None = None,
    timeout: float = 30.0,
    sleep: Callable[[float], None] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "tickers": tickers,
        "post": post,
        "timeout": timeout,
        "now": now,
    }
    if sleep is not None:
        kwargs["sleep"] = sleep
    return refresh_quotes(**kwargs)
