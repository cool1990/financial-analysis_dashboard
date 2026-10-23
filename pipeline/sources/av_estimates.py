"""当前季一致预期缓存。

观察池读 data/{TICKER}/consensus/av_estimates.json。
若设置了 HERMES_AV_ESTIMATES_DIR，本仓库没有缓存时再读
{HERMES_AV_ESTIMATES_DIR}/{TICKER}.json。没设置就不查那条路径。
两处都没有、环境里也没有 key，就留空。

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
      "horizon": "fiscal quarter",
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
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from pipeline.config import data_dir
from pipeline.sources.alpha_vantage import alphavantage_api_key, fetch_earnings_estimates_payload


HERMES_DIR_ENV = "HERMES_AV_ESTIMATES_DIR"
CACHE_NAME = "av_estimates.json"
FRESH_HOURS = 20
_AV_FIELDS = {
    "eps_estimate_average",
    "eps_estimate_analyst_count",
    "eps_estimate_average_30_days_ago",
    "eps_estimate_revision_up_trailing_30_days",
    "eps_estimate_revision_down_trailing_30_days",
}


def cache_path(ticker: str) -> Path:
    return data_dir(ticker) / "consensus" / CACHE_NAME


def hermes_cache_path(ticker: str) -> Path | None:
    raw = (os.environ.get(HERMES_DIR_ENV) or "").strip()
    if not raw:
        return None
    return Path(raw) / f"{(ticker or '').upper()}.json"


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


_SECRET_KEYS = {"apikey", "api_key", "alphavantage_key"}


def _clean_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if str(key).lower() not in _SECRET_KEYS}


def write_estimates_cache(symbol: str, estimates: list[dict[str, Any]]) -> Path:
    record = {
        "symbol": symbol,
        "function": "EARNINGS_ESTIMATES",
        "fetched_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "estimates": [_clean_row(row) for row in estimates],
    }
    path = cache_path(symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _fetched_at(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("fetched_at") or "").strip()
    return ""


def _parse_fetched_at(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def cache_is_fresh(
    ticker: str,
    *,
    now: datetime | None = None,
    hours: float = FRESH_HOURS,
) -> bool:
    """本仓库缓存的 fetched_at 距 now 不足 hours 小时。没有时间戳不算新鲜。"""
    payload = _read_json(cache_path(ticker))
    fetched = _parse_fetched_at(_fetched_at(payload))
    if fetched is None:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current - fetched < timedelta(hours=hours)


def load_earnings_record(
    ticker: str,
    *,
    get: Callable[..., Any] | None = None,
    fetch: bool = True,
) -> dict[str, Any]:
    """先读本仓库缓存，再读 HERMES_AV_ESTIMATES_DIR。都没有且有 key 时才请求并写入缓存。

    返回 {"estimates": [...], "fetched_at": "..."}。fetched_at 原样带出，供首页判断是否过期。
    """
    symbol = (ticker or "").upper()
    paths = [cache_path(symbol)]
    hermes = hermes_cache_path(symbol)
    if hermes is not None:
        paths.append(hermes)
    for path in paths:
        payload = _read_json(path)
        if payload is None:
            continue
        return {"estimates": estimates_from_payload(payload), "fetched_at": _fetched_at(payload)}
    if not fetch or not alphavantage_api_key():
        return {"estimates": [], "fetched_at": ""}
    body = fetch_earnings_estimates_payload(symbol, get=get)
    if body.get("status") != "ok":
        return {"estimates": [], "fetched_at": ""}
    estimates = [row for row in body.get("estimates") or [] if isinstance(row, dict)]
    if not estimates:
        return {"estimates": [], "fetched_at": ""}
    path = write_estimates_cache(symbol, estimates)
    payload = _read_json(path) or {}
    return {"estimates": estimates, "fetched_at": _fetched_at(payload)}


def load_earnings_estimates(
    ticker: str,
    *,
    get: Callable[..., Any] | None = None,
    fetch: bool = True,
) -> list[dict[str, Any]]:
    """与 load_earnings_record 相同，只返回 estimates 列表。"""
    return list(load_earnings_record(ticker, get=get, fetch=fetch)["estimates"])
