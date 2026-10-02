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

    def daily_closes(self, start: str, end: str) -> list[tuple[str, float]]:
        """返回 [(YYYY-MM-DD, close), ...]，按日期升序。"""
        df = _safe_call(self.t.history, start=start, end=end, auto_adjust=False)
        if df is None or getattr(df, "empty", True):
            return []
        out: list[tuple[str, float]] = []
        for idx, row in df.iterrows():
            try:
                day = idx.date().isoformat() if hasattr(idx, "date") else str(idx)[:10]
                close = float(row["Close"])
            except Exception:
                continue
            out.append((day, close))
        out.sort(key=lambda x: x[0])
        return out

    def next_day_reaction(
        self,
        release_at_utc: str,
        *,
        release_timing: str = "amc",
    ) -> dict[str, Any]:
        """按 AMC/BMO 规则计算财报次日涨跌。

        AMC（盘后）：发布日收盘 → 下一交易日收盘
        BMO（盘前）：上一交易日收盘 → 发布日收盘
        """
        release_dt = datetime.fromisoformat(release_at_utc.replace("Z", "+00:00"))
        release_day = release_dt.date().isoformat()
        # 多取几天覆盖周末/假日
        from datetime import timedelta

        start = (release_dt.date() - timedelta(days=7)).isoformat()
        end = (release_dt.date() + timedelta(days=10)).isoformat()
        closes = self.daily_closes(start, end)
        if not closes:
            return {
                "next_day_pct": None,
                "close_before": None,
                "close_after": None,
                "before_date": None,
                "after_date": None,
                "timing": release_timing,
                "note": "无法取得附近交易日收盘价",
            }

        days = [d for d, _ in closes]
        by_day = {d: c for d, c in closes}
        timing = (release_timing or "amc").lower()

        def _prev(day: str) -> str | None:
            earlier = [d for d in days if d < day]
            return earlier[-1] if earlier else None

        def _next(day: str) -> str | None:
            later = [d for d in days if d > day]
            return later[0] if later else None

        if timing == "bmo":
            after_date = release_day if release_day in by_day else _next(release_day)
            before_date = _prev(after_date) if after_date else None
        else:
            # amc / unknown：发布日收盘为「前」，下一交易日为「后」
            before_date = release_day if release_day in by_day else _prev(release_day)
            after_date = _next(before_date) if before_date else None

        close_before = by_day.get(before_date) if before_date else None
        close_after = by_day.get(after_date) if after_date else None
        pct = None
        if close_before and close_after and close_before != 0:
            pct = (close_after - close_before) / abs(close_before)

        note = None
        if pct is None:
            note = "交易日收盘价不足，暂无法计算次日涨跌"
        elif timing == "bmo":
            note = f"BMO：{before_date} 收盘 → {after_date} 收盘"
        else:
            note = f"AMC：{before_date} 收盘 → {after_date} 收盘"

        return {
            "next_day_pct": pct,
            "close_before": close_before,
            "close_after": close_after,
            "before_date": before_date,
            "after_date": after_date,
            "timing": timing,
            "note": note,
        }

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
