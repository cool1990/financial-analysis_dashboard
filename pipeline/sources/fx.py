"""港币兑人民币。HKDCNY 是 1 港币折合多少人民币，来自 TradingView 的 FX_IDC:HKDCNY。

只有 CNYHKD 时用它的倒数。不调用其他外汇接口。
`rate_on` 只认汇率日期与行情日同一天的收盘，不用前后日填空。
`rate_for_pe`：当天没有时，向 TradingView 要最新一根；那根日期不晚于行情日才保存。
一笔都没有才留给页面显示待核对。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from pipeline import ROOT

PAIR = "HKDCNY"
SOURCE = "tradingview:FX_IDC:HKDCNY"


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


def _on_or_before(node: dict[str, Any] | None, day: str) -> dict[str, Any] | None:
    """日期不晚于行情日的正 HKDCNY。更晚的日期不能拿来算这一天的 P/E。"""
    if not day or not isinstance(node, dict):
        return None
    node_day = str(node.get("date") or "")
    if not node_day or node_day > day:
        return None
    return accept_rate(node, node_day)


def _prefer(current: dict[str, Any] | None, candidate: dict[str, Any] | None) -> dict[str, Any] | None:
    if candidate is None:
        return current
    if current is None or str(candidate.get("date") or "") >= str(current.get("date") or ""):
        return candidate
    return current


def latest_saved_on_or_before(day: str) -> dict[str, Any] | None:
    """data/fx/HKDCNY.json 里日期不晚于 day 的最近一笔正汇率。"""
    path = fx_path()
    if not day or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rates = data.get("rates") if isinstance(data, dict) else None
    if not isinstance(rates, dict):
        return None
    file_source = SOURCE
    if isinstance(data.get("source"), str) and data["source"].strip():
        file_source = data["source"].strip()
    chosen: dict[str, Any] | None = None
    for name, raw in rates.items():
        rate_day = str(name)
        if not rate_day or rate_day > day:
            continue
        source = file_source
        value = raw
        if isinstance(raw, dict):
            if isinstance(raw.get("source"), str) and raw["source"].strip():
                source = raw["source"].strip()
            value = raw.get("rate")
        chosen = _prefer(chosen, accept_rate({"pair": PAIR, "rate": value, "date": rate_day, "source": source}, rate_day))
    return chosen


# 同一次构建里，某个行情日拉过还是没有，就不要每个财报页再打一次 TradingView。
_FETCH_TRIED: set[tuple[str, str]] = set()


def rate_for_pe(day: str, *, attached: dict[str, Any] | None = None, fetch: bool = True) -> dict[str, Any] | None:
    """Forward PE 用的 HKDCNY。当天优先；否则是不晚于行情日的最近一笔。

    返回里 `matched` 为 `same_day` 或 `recent`，`as_of` 是请求的行情日。
    没有可用汇率时返回 None。调用方据此把 P/E 写成待核对。
    """
    day = str(day or "")
    if not day:
        return None
    attached_node = attached if isinstance(attached, dict) else None
    same = rate_on(day, attached=attached_node)
    if same:
        return {**same, "matched": "same_day", "as_of": day}
    if fetch:
        key = (str(fx_path()), day)
        if key not in _FETCH_TRIED:
            _FETCH_TRIED.add(key)
            try:
                fetch_recent_hkd_cny(day)
            except Exception:
                pass
            same = rate_on(day, attached=attached_node)
            if same:
                return {**same, "matched": "same_day", "as_of": day}
    chosen = _prefer(latest_saved_on_or_before(day), _on_or_before(attached_node, day))
    if chosen is None:
        return None
    matched = "same_day" if chosen["date"] == day else "recent"
    return {**chosen, "matched": matched, "as_of": day}


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


def _record_from_quote(quote: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(quote, dict):
        return None
    day = str(quote.get("date") or "")
    rate = _num(quote.get("rate"))
    if not day or rate is None:
        return None
    source = str(quote.get("source") or SOURCE)
    return {"pair": PAIR, "rate": rate, "date": day, "source": source}


def fetch_hkd_cny(day: str) -> dict[str, Any] | None:
    """向 TradingView 要 HKDCNY。最新一根的香港日期必须等于 day，才写入 data/fx/HKDCNY.json。"""
    from pipeline.sources.tradingview_scan import fetch_hkd_cny_quote

    quote = _record_from_quote(fetch_hkd_cny_quote())
    if quote is None or quote["date"] != day:
        return None
    store_rate(quote)
    return quote


def fetch_recent_hkd_cny(day: str, lookback_days: int = 14) -> dict[str, Any] | None:
    """向 TradingView 要最新一根 HKDCNY。日期不晚于 day 才保存。

    Scanner 只有最新报价，不拉其他外汇源的历史。lookback_days 保留给调用方，这里不用。
    网络或解析失败时返回 None，不抛出，避免构建页面中断。
    """
    del lookback_days
    try:
        date.fromisoformat(day)
    except ValueError:
        return None
    from pipeline.sources.tradingview_scan import fetch_hkd_cny_quote

    try:
        quote = _record_from_quote(fetch_hkd_cny_quote())
    except Exception:
        return None
    if quote is None or quote["date"] > day:
        return None
    try:
        store_rate(quote)
    except (OSError, ValueError):
        return None
    return quote
