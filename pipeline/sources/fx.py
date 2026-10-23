"""港币兑人民币。HKDCNY 是 1 港币折合多少人民币，来自 Yahoo Finance 的 HKDCNY=X。

只认汇率日期与行情日同一天的收盘。没有这一天的价，就不要拿前后日顶上。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from pipeline import ROOT

PAIR = "HKDCNY"
SOURCE = "yfinance:HKDCNY=X"
YAHOO_TICKER = "HKDCNY=X"


def fx_path() -> Path:
    return ROOT / "data" / "fx" / "HKDCNY.json"


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


def accept_rate(node: dict[str, Any] | None, day: str) -> dict[str, Any] | None:
    """日期必须等于行情日，货币对必须是 HKDCNY。"""
    if not day or not isinstance(node, dict):
        return None
    if str(node.get("date") or "") != day:
        return None
    pair = str(node.get("pair") or PAIR).upper()
    if pair != PAIR:
        return None
    rate = _num(node.get("rate"))
    if rate is None:
        return None
    source = str(node.get("source") or SOURCE)
    return {"pair": PAIR, "rate": rate, "date": day, "source": source}


def close_on(rows: list[tuple[str, float]], day: str) -> float | None:
    """只取当天收盘。后面的交易日不能拿来填空。"""
    for name, close in rows:
        if name == day:
            return _num(close)
    return None


def _saved(day: str) -> dict[str, Any] | None:
    path = fx_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rates = data.get("rates") if isinstance(data, dict) else None
    if not isinstance(rates, dict) or day not in rates:
        return None
    raw = rates[day]
    source = SOURCE
    if isinstance(data.get("source"), str) and data["source"].strip():
        source = data["source"].strip()
    if isinstance(raw, dict):
        if isinstance(raw.get("source"), str) and raw["source"].strip():
            source = raw["source"].strip()
        raw = raw.get("rate")
    return {"pair": PAIR, "rate": raw, "date": day, "source": source}


def rate_on(day: str, *, attached: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """优先用这条行情自带的汇率，否则读 data/fx/HKDCNY.json 里的同一天。"""
    for node in (attached, _saved(day)):
        picked = accept_rate(node if isinstance(node, dict) else None, day)
        if picked:
            return picked
    return None


def store_rate(record: dict[str, Any]) -> None:
    day = str(record.get("date") or "")
    accepted = accept_rate(record, day)
    if accepted is None:
        raise ValueError("HKDCNY 记录缺少当天正汇率")
    path = fx_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {"pair": PAIR, "source": SOURCE, "rates": {}}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if isinstance(loaded, dict) and isinstance(loaded.get("rates"), dict):
            data = loaded
            data["pair"] = PAIR
            data["source"] = SOURCE
            data.setdefault("rates", {})
    data["rates"][day] = {"rate": accepted["rate"], "source": accepted["source"]}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_hkd_cny(day: str) -> dict[str, Any] | None:
    """拉取 HKDCNY=X 在 day 的收盘，并写入 data/fx/HKDCNY.json。当天没有成交就返回 None。"""
    from pipeline.sources.yfinance_src import YFinanceSource

    start = date.fromisoformat(day)
    end = (start + timedelta(days=1)).isoformat()
    closes = YFinanceSource(YAHOO_TICKER).daily_closes(start.isoformat(), end)
    rate = close_on(closes, day)
    if rate is None:
        return None
    record = {"pair": PAIR, "rate": rate, "date": day, "source": SOURCE}
    store_rate(record)
    return record
