"""当前季一致预期缓存。

Hermes 读的是 /home/box/.hermes/data/av_estimates/{TICKER}.json。这台机器读不到那份目录时，
观察池改读 data/{TICKER}/consensus/av_estimates.json。两处都没有、环境里也没有 key，就留空。

需要的环境变量（任一即可，与财报流水线相同）：
ALPHAVANTAGE_API_KEY、ALPHAVANTAGE_KEY、ALPHA_VANTAGE_API_KEY。

缓存形状与 EARNINGS_ESTIMATES 原文一致，不另起字段名：

{
  "symbol": "MU",
  "function": "EARNINGS_ESTIMATES",
  "fetched_at": "2026-10-23T00:00:00+00:00",
  "estimates": [
    {
      "date": "2026-11-30",
      "horizon": "next fiscal quarter",
      "eps_estimate_average": "38.28",
      "eps_estimate_analyst_count": "29",
      "eps_estimate_average_30_days_ago": "35.66",
      "eps_estimate_revision_up_trailing_30_days": "4",
      "eps_estimate_revision_down_trailing_30_days": "1"
    }
  ]
}

也接受：Hermes 文件本身就是这个对象、estimates 数组，或已经只剩当前季一行（仍用上面的字段名）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pipeline.config import data_dir
from pipeline.sources.alpha_vantage import alphavantage_api_key, fetch_earnings_estimates_payload


HERMES_DIR = Path("/home/box/.hermes/data/av_estimates")
CACHE_NAME = "av_estimates.json"
_AV_FIELDS = {
    "eps_estimate_average",
    "eps_estimate_analyst_count",
    "eps_estimate_average_30_days_ago",
    "eps_estimate_revision_up_trailing_30_days",
    "eps_estimate_revision_down_trailing_30_days",
}


def cache_path(ticker: str) -> Path:
    return data_dir(ticker) / "consensus" / CACHE_NAME


def hermes_cache_path(ticker: str) -> Path:
    return HERMES_DIR / f"{(ticker or '').upper()}.json"


def estimates_from_payload(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if not isinstance(data, dict):
        return []
    estimates = data.get("estimates")
    if isinstance(estimates, list):
        return [row for row in estimates if isinstance(row, dict)]
    if _AV_FIELDS & set(data):
        return [data]
    return []


def _read_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_earnings_estimates(
    ticker: str,
    *,
    get: Callable[..., Any] | None = None,
    fetch: bool = True,
) -> list[dict[str, Any]]:
    """先读本仓库缓存，再读 Hermes 路径。都没有且有 key 时才请求 EARNINGS_ESTIMATES 并写入缓存。"""
    symbol = (ticker or "").upper()
    for path in (cache_path(symbol), hermes_cache_path(symbol)):
        payload = _read_json(path)
        if payload is None:
            continue
        return estimates_from_payload(payload)
    if not fetch or not alphavantage_api_key():
        return []
    body = fetch_earnings_estimates_payload(symbol, get=get)
    if body.get("status") != "ok":
        return []
    estimates = [row for row in body.get("estimates") or [] if isinstance(row, dict)]
    if not estimates:
        return []
    record = {
        "symbol": symbol,
        "function": "EARNINGS_ESTIMATES",
        "fetched_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "estimates": estimates,
    }
    path = cache_path(symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")
    return estimates
