"""TradingView scanner → data/{TICKER}/consensus/tradingview_scan.json

镜像 Obsidian/Hermes ``tvav_estimates.tv_scan`` / ``tv_normalize``：
- 每个市场一次匿名 POST（america / hongkong），不带 cookie
- 港股 ``0700.HK`` → ``HKEX:700``（带前导零的 ``HKEX:0700`` 扫不到）
- 美股按 NASDAQ → NYSE → AMEX 候选，取扫到的第一个
- ``change`` 是百分比点数（1.23 = +1.23%），写入 ``day_change_pct`` / ``change_pct`` 时除以 100
- 未来未公布季的 EPS / 营收写进 ``rows_used.earnings_fq_h`` / ``revenues_fq_h``
  （build 盈利预期图与记分卡兜底都读这里）；**全年 EPS 不在这里发明**，由 Alpha Vantage
  ``av_estimates.json`` 的 fiscal year 行供给 ``_annual_consensus``
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from pipeline.config import data_dir, is_hk_listing, list_tickers, load_ticker_config

TV_UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "application/json",
}

# Hermes TV_COLS 行情 + 一致预期子集。全年 FY 数字仍走 AV，不拿 next_fy 冒充。
SCAN_COLUMNS = [
    "name",
    "close",
    "change",
    "RSI",
    "volume",
    "currency",
    "fundamental_currency_code",
    "earnings_per_share_forecast_fq",
    "earnings_per_share_forecast_next_fq",
    "earnings_per_share_forecast_next_fh",
    "earnings_per_share_forecast_next_fy",
    "eps_estimate_ntm",
    "revenue_forecast_next_fq",
    "revenue_forecast_next_fy",
    "revenue_estimate_ntm",
    "earnings_fq_h",
    "revenues_fq_h",
    "earnings_per_share_fq",
    "revenue_fq",
    "price_earnings_fwd",
    "price_earnings_ttm",
    "dividends_yield",
    "dividends_yield_current",
    "market_cap_basic",
    "time",
    "last_bar_update_time",
]

# 兼容旧测试 / 外部引用
QUOTE_COLUMNS = SCAN_COLUMNS

SCALAR_KEYS = (
    "earnings_per_share_forecast_fq",
    "earnings_per_share_forecast_next_fq",
    "earnings_per_share_forecast_next_fh",
    "earnings_per_share_forecast_next_fy",
    "eps_estimate_ntm",
    "revenue_forecast_next_fq",
    "revenue_forecast_next_fy",
    "revenue_estimate_ntm",
    "earnings_per_share_fq",
    "revenue_fq",
)


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def scan_path(ticker: str) -> Path:
    return data_dir(ticker) / "consensus" / "tradingview_scan.json"


def tv_symbol_candidates(ticker: str, cfg: dict[str, Any] | None = None) -> tuple[str, list[str]]:
    """返回 (market, [TV 符号候选…])。港股去前导零；美股试三大所。"""
    symbol = (ticker or "").strip().upper()
    cfg = cfg or {}
    if is_hk_listing(cfg) or symbol.endswith(".HK"):
        code = symbol.replace(".HK", "").lstrip("0") or "0"
        return "hongkong", [f"HKEX:{code}"]
    bare = symbol.replace(".", "_")
    exchange = str(cfg.get("exchange") or "").strip().upper()
    ordered: list[str] = []
    if exchange in {"NYSE", "NASDAQ", "AMEX"}:
        ordered.append(f"{exchange}:{bare}")
    for prefix in ("NASDAQ", "NYSE", "AMEX"):
        candidate = f"{prefix}:{bare}"
        if candidate not in ordered:
            ordered.append(candidate)
    if str(cfg.get("listing") or "").strip().lower() == "adr":
        nyse = f"NYSE:{bare}"
        ordered = [nyse] + [c for c in ordered if c != nyse]
    return "america", ordered


def _post_scan(
    market: str,
    symbols: list[str],
    *,
    post: Callable[..., Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, dict[str, Any]]:
    """一次 POST，返回 {TV符号: {列名: 值}}。"""
    url = f"https://scanner.tradingview.com/{market}/scan"
    body = {"symbols": {"tickers": symbols}, "columns": SCAN_COLUMNS}
    if post is None:
        response = requests.post(url, json=body, headers=TV_UA, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    else:
        payload = post(url, body)
    got: dict[str, dict[str, Any]] = {}
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        key = item.get("s")
        row = item.get("d")
        if not key or not isinstance(row, list):
            continue
        got[str(key)] = dict(zip(SCAN_COLUMNS, row))
    return got


def tv_scan(
    tickers: list[str] | None = None,
    *,
    post: Callable[..., Any] | None = None,
    timeout: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """按市场批量扫。返回 ({ticker: {tv_symbol, raw, endpoint}}, errors)。"""
    names = [(t or "").strip().upper() for t in (tickers or list_tickers())]
    by_market: dict[str, list[tuple[str, list[str]]]] = {}
    for name in names:
        try:
            cfg = load_ticker_config(name)
        except FileNotFoundError:
            cfg = {}
        market, candidates = tv_symbol_candidates(name, cfg)
        by_market.setdefault(market, []).append((name, candidates))

    out: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for market, items in by_market.items():
        symbols = [sym for _, cands in items for sym in cands]
        got: dict[str, dict[str, Any]] = {}
        for attempt in range(2):
            try:
                got = _post_scan(market, symbols, post=post, timeout=timeout)
                break
            except Exception as exc:  # noqa: BLE001 — soft-fail 路径，错误汇总后返回
                errors.append(f"{market}: {exc}")
                if attempt == 0:
                    sleep(5)
        endpoint = f"https://scanner.tradingview.com/{market}/scan"
        for name, cands in items:
            matched = next((sym for sym in cands if sym in got), None)
            if matched:
                out[name] = {"tv_symbol": matched, "raw": got[matched], "endpoint": endpoint}
    return out, errors


def quote_columns_from_raw(raw: dict[str, Any]) -> dict[str, Any]:
    """把 scanner 行压成 build._quote_view / _saved_quote 认的 columns。"""
    cols: dict[str, Any] = {}
    close = _num(raw.get("close"))
    if close is not None:
        cols["close"] = close
    rsi = _num(raw.get("RSI"))
    if rsi is not None:
        cols["RSI"] = rsi
    change = _num(raw.get("change"))
    if change is not None:
        frac = change / 100.0
        cols["day_change_pct"] = frac
        cols["change_pct"] = frac
    for key in (
        "eps_estimate_ntm",
        "earnings_per_share_forecast_next_fy",
        "price_earnings_fwd",
        "price_earnings_ttm",
        "dividends_yield",
        "dividends_yield_current",
        "market_cap_basic",
        "volume",
    ):
        value = _num(raw.get(key))
        if value is not None:
            cols[key] = value
    if "price_earnings_fwd" in cols:
        cols["price_earnings_forward"] = cols["price_earnings_fwd"]
    currency = raw.get("currency")
    if isinstance(currency, str) and currency.strip():
        cols["currency"] = currency.strip()
    fund = raw.get("fundamental_currency_code")
    if isinstance(fund, str) and fund.strip():
        cols["fundamental_currency_code"] = fund.strip()
    return cols


def _history_rows(raw: dict[str, Any], key: str) -> list[dict[str, Any]]:
    rows = raw.get(key) or []
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        out.append({
            "Actual": item.get("Actual"),
            "Estimate": item.get("Estimate"),
            "FiscalPeriod": item.get("FiscalPeriod"),
            "IsReported": bool(item.get("IsReported")),
            "Type": item.get("Type"),
        })
    return out


def future_quarters_from_raw(raw: dict[str, Any], *, limit: int = 4) -> list[dict[str, Any]]:
    """Hermes 同款：earnings_fq_h 里 IsReported=false 的前 N 季，营收按 FiscalPeriod 对齐。"""
    earnings = _history_rows(raw, "earnings_fq_h")
    revenues = {
        str(row.get("FiscalPeriod") or ""): row
        for row in _history_rows(raw, "revenues_fq_h")
        if row.get("FiscalPeriod")
    }
    future = [row for row in earnings if not row.get("IsReported")]
    out: list[dict[str, Any]] = []
    for row in future[:limit]:
        label = str(row.get("FiscalPeriod") or "")
        rev = revenues.get(label) or {}
        out.append({
            "tv_label": label,
            "eps_est": _num(row.get("Estimate")),
            "rev_est": _num(rev.get("Estimate")),
        })
    return out


def scalars_from_raw(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in SCALAR_KEYS:
        value = _num(raw.get(key))
        if value is not None:
            out[key] = value
    return out


def rows_used_from_raw(raw: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    earnings = _history_rows(raw, "earnings_fq_h")
    revenues = _history_rows(raw, "revenues_fq_h")
    # chart 与明细共用完整序列；build 优先读 earnings_fq_h_chart
    return {
        "earnings_fq_h": earnings,
        "revenues_fq_h": revenues,
        "earnings_fq_h_chart": earnings,
    }


def build_quote_payload(
    ticker: str,
    *,
    tv_symbol: str,
    endpoint: str,
    raw: dict[str, Any],
    fetched_at: str | None = None,
) -> dict[str, Any]:
    stamp = fetched_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "fetched_at": stamp,
        "endpoint": endpoint,
        "ticker": tv_symbol,
        "columns": quote_columns_from_raw(raw),
        "source": "tradingview scanner (anonymous)",
    }


def build_scan_document(
    ticker: str,
    *,
    tv_symbol: str,
    endpoint: str,
    raw: dict[str, Any],
    fetched_at: str | None = None,
) -> dict[str, Any]:
    """顶层结构对齐 META 手工快照：body / scalars / rows_used / quote，均带 fetched_at。"""
    stamp = fetched_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    quote = build_quote_payload(
        ticker, tv_symbol=tv_symbol, endpoint=endpoint, raw=raw, fetched_at=stamp
    )
    return {
        "fetched_at": stamp,
        "endpoint": endpoint,
        "method": "POST",
        "body": {
            "symbols": {"tickers": [tv_symbol]},
            "columns": list(SCAN_COLUMNS),
        },
        "scalars": scalars_from_raw(raw),
        "rows_used": rows_used_from_raw(raw),
        "future_quarters": future_quarters_from_raw(raw, limit=4),
        "chart_rows_fetched_at": stamp,
        "quote": quote,
        "tv_scan": {
            "fetched_at": stamp,
            "endpoint": endpoint,
            "ticker": tv_symbol,
            "source": "tradingview scanner (anonymous)",
        },
        "sources": {
            "tradingview": {
                "label": "TradingView scanner",
                "fetched_at": stamp,
                "endpoint": endpoint,
                "covers": ["price", "rsi", "day_change_pct", "quarterly_eps", "quarterly_revenue", "eps_estimate_ntm"],
            },
            "alpha_vantage_annual_eps": {
                "label": "Alpha Vantage EARNINGS_ESTIMATES",
                "note": "全年 EPS 不在本文件发明；见同目录 av_estimates.json 的 fiscal year 行",
            },
        },
    }


def write_scan_document(ticker: str, document: dict[str, Any]) -> Path:
    """写入完整扫描文档。quote.columns 与旧文件按键合并，避免丢掉手工补的列。"""
    path = scan_path(ticker)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = {}
    previous_quote = existing.get("quote") if isinstance(existing.get("quote"), dict) else {}
    prev_cols = previous_quote.get("columns") if isinstance(previous_quote.get("columns"), dict) else {}
    new_quote = document.get("quote") if isinstance(document.get("quote"), dict) else {}
    new_cols = new_quote.get("columns") if isinstance(new_quote.get("columns"), dict) else {}
    merged_cols = dict(prev_cols)
    merged_cols.update(new_cols)
    merged_quote = dict(previous_quote)
    merged_quote.update(new_quote)
    merged_quote["columns"] = merged_cols

    out = dict(existing)
    out.update(document)
    out["quote"] = merged_quote
    out["fetched_at"] = document.get("fetched_at") or existing.get("fetched_at")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


# 旧名保留
def write_scan_quote(ticker: str, quote: dict[str, Any]) -> Path:
    path = scan_path(ticker)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = {}
    previous = existing.get("quote") if isinstance(existing.get("quote"), dict) else {}
    prev_cols = previous.get("columns") if isinstance(previous.get("columns"), dict) else {}
    new_cols = quote.get("columns") if isinstance(quote.get("columns"), dict) else {}
    merged_cols = dict(prev_cols)
    merged_cols.update(new_cols)
    merged_quote = dict(previous)
    merged_quote.update(quote)
    merged_quote["columns"] = merged_cols
    existing["fetched_at"] = merged_quote.get("fetched_at") or existing.get("fetched_at")
    existing["quote"] = merged_quote
    existing["tv_scan"] = {
        "fetched_at": merged_quote.get("fetched_at"),
        "endpoint": merged_quote.get("endpoint"),
        "ticker": merged_quote.get("ticker"),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def refresh_quotes(
    tickers: list[str] | None = None,
    *,
    post: Callable[..., Any] | None = None,
    timeout: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
) -> dict[str, Any]:
    """扫观察池并写入各票 tradingview_scan.json（行情 + 未来四季一致预期）。"""
    stamp_dt = now or datetime.now(timezone.utc)
    if stamp_dt.tzinfo is None:
        stamp_dt = stamp_dt.replace(tzinfo=timezone.utc)
    stamp = stamp_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    names = [(t or "").strip().upper() for t in (tickers or list_tickers())]
    raw_map, errors = tv_scan(names, post=post, timeout=timeout, sleep=sleep)
    results: list[dict[str, Any]] = []
    written = 0
    for name in names:
        hit = raw_map.get(name)
        if not hit:
            results.append({"ticker": name, "ok": False, "reason": "scanner 未命中"})
            continue
        document = build_scan_document(
            name,
            tv_symbol=hit["tv_symbol"],
            endpoint=hit["endpoint"],
            raw=hit["raw"],
            fetched_at=stamp,
        )
        cols = (document.get("quote") or {}).get("columns") or {}
        if _num(cols.get("close")) is None:
            results.append({
                "ticker": name,
                "ok": False,
                "tv_symbol": hit["tv_symbol"],
                "reason": "没有 close",
            })
            continue
        write_scan_document(name, document)
        written += 1
        future = document.get("future_quarters") or []
        results.append({
            "ticker": name,
            "ok": True,
            "tv_symbol": hit["tv_symbol"],
            "close": cols.get("close"),
            "day_change_pct": cols.get("day_change_pct"),
            "RSI": cols.get("RSI"),
            "eps_estimate_ntm": cols.get("eps_estimate_ntm"),
            "future_quarters": len(future),
            "fetched_at": stamp,
        })
    failed = [item for item in results if not item.get("ok")]
    ok = written > 0 or (not failed and not errors)
    reason = ""
    if not ok:
        reason = "没有写入任何行情"
        if errors:
            reason = f"{reason}；" + "; ".join(errors[:3])
    return {
        "ok": ok,
        "written": written,
        "reason": reason,
        "errors": errors,
        "fetched_at": stamp,
        "results": results,
    }
