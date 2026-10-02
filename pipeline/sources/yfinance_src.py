from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import yfinance as yf
from tenacity import retry, stop_after_attempt, wait_exponential


def _df_to_records(df: pd.DataFrame | None) -> dict[str, Any]:
    if df is None or (isinstance(df, pd.DataFrame) and df.empty):
        return {"columns": [], "index": [], "data": []}
    if not isinstance(df, pd.DataFrame):
        return {"raw": str(df)}
    out = df.copy()
    out.index = out.index.map(lambda x: str(x))
    return json.loads(out.to_json(orient="split", date_format="iso"))


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8), reraise=True)
def _safe_call(fn, *args, **kwargs):
    return fn(*args, **kwargs)


class YFinanceSource:
    def __init__(self, ticker: str):
        self.ticker = ticker.upper()
        self.t = yf.Ticker(self.ticker)

    def earnings_estimate(self) -> dict[str, Any]:
        return _df_to_records(_safe_call(lambda: self.t.earnings_estimate))

    def revenue_estimate(self) -> dict[str, Any]:
        return _df_to_records(_safe_call(lambda: self.t.revenue_estimate))

    def eps_trend(self) -> dict[str, Any]:
        return _df_to_records(_safe_call(lambda: self.t.eps_trend))

    def eps_revisions(self) -> dict[str, Any]:
        return _df_to_records(_safe_call(lambda: self.t.eps_revisions))

    def earnings_history(self) -> dict[str, Any]:
        return _df_to_records(_safe_call(lambda: self.t.earnings_history))

    def earnings_dates(self, limit: int = 12) -> dict[str, Any]:
        return _df_to_records(_safe_call(self.t.get_earnings_dates, limit=limit))

    def calendar(self) -> dict[str, Any]:
        cal = _safe_call(lambda: self.t.calendar)
        if isinstance(cal, dict):
            out = {}
            for k, v in cal.items():
                if hasattr(v, "isoformat"):
                    out[k] = v.isoformat()
                elif isinstance(v, list):
                    out[k] = [x.isoformat() if hasattr(x, "isoformat") else x for x in v]
                elif isinstance(v, (int, float, str, type(None), dict)):
                    out[k] = v
                else:
                    out[k] = str(v)
            return out
        return _df_to_records(cal if isinstance(cal, pd.DataFrame) else None)

    def splits(self) -> dict[str, float]:
        s = _safe_call(lambda: self.t.splits)
        if s is None or getattr(s, "empty", True):
            return {}
        return {str(idx.date() if hasattr(idx, "date") else idx): float(val) for idx, val in s.items()}

    def history(self, start: str, end: str) -> dict[str, Any]:
        df = _safe_call(self.t.history, start=start, end=end, auto_adjust=False)
        return _df_to_records(df)

    def snapshot_bundle(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "snapshot_at": datetime.now(timezone.utc).isoformat(),
            "earnings_estimate": self.earnings_estimate(),
            "revenue_estimate": self.revenue_estimate(),
            "eps_trend": self.eps_trend(),
            "eps_revisions": self.eps_revisions(),
            "earnings_dates": self.earnings_dates(),
            "calendar": self.calendar(),
        }


def estimate_row(split_json: dict[str, Any], row_name: str) -> dict[str, Any] | None:
    """从 to_json(orient='split') 结构取某一行。"""
    if not split_json or not split_json.get("index"):
        return None
    try:
        idx = split_json["index"].index(row_name)
    except ValueError:
        return None
    cols = split_json.get("columns") or []
    data = split_json.get("data") or []
    if idx >= len(data):
        return None
    return dict(zip(cols, data[idx]))


def history_eps_pairs(history_json: dict[str, Any]) -> list[dict[str, Any]]:
    """解析 earnings_history 为 [{end, epsEstimate, epsActual, surprisePercent}]。"""
    if not history_json or not history_json.get("index"):
        return []
    cols = history_json.get("columns") or []
    out = []
    for i, idx in enumerate(history_json.get("index") or []):
        row = dict(zip(cols, history_json["data"][i]))
        out.append(
            {
                "period_end": str(idx)[:10],
                "epsEstimate": row.get("epsEstimate") or row.get("EPS Estimate"),
                "epsActual": row.get("epsActual") or row.get("Reported EPS"),
                "surprisePercent": row.get("surprisePercent") or row.get("Surprise(%)"),
            }
        )
    return out
