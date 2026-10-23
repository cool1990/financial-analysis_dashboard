"""Alpha Vantage 一致预期。没 key 或这次没发出请求时，状态必须写成 not_called。"""

from __future__ import annotations

import os
from typing import Any, Callable

import httpx


_KEY_ENVS = ("ALPHAVANTAGE_KEY", "ALPHAVANTAGE_API_KEY", "ALPHA_VANTAGE_API_KEY")
_URL = "https://www.alphavantage.co/query"


def alphavantage_api_key() -> str | None:
    for name in _KEY_ENVS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def fetch_earnings_estimates(
    symbol: str,
    *,
    get: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """返回本轮调用状态，不把预期数字写进财报 JSON。

    get(url, params) 可注入，测试不联网。
    """
    key = alphavantage_api_key()
    result: dict[str, Any] = {
        "function": "EARNINGS_ESTIMATES",
        "symbol": (symbol or "").upper(),
        "called": False,
        "status": "not_called",
        "reason": "没有 API key",
    }
    if not key:
        return result

    def _default_get(url: str, params: dict[str, str]) -> Any:
        with httpx.Client(timeout=20.0) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            return response.json()

    getter = get or _default_get
    try:
        data = getter(
            _URL,
            {"function": "EARNINGS_ESTIMATES", "symbol": result["symbol"], "apikey": key},
        )
    except Exception as exc:
        return {
            **result,
            "called": True,
            "status": "error",
            "reason": str(exc).replace(key, "***")[:240],
        }
    if not isinstance(data, dict):
        return {**result, "called": True, "status": "error", "reason": "返回不是 JSON 对象"}
    note = data.get("Note") or data.get("Information") or data.get("Error Message")
    if note:
        return {
            **result,
            "called": True,
            "status": "error",
            "reason": str(note).replace(key, "***")[:240],
        }
    return {**result, "called": True, "status": "ok", "reason": ""}
