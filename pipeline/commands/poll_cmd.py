from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pipeline.commands.run_cmd import _in_window, detect_new_filings, run_stage1, run_stage2
from pipeline.config import data_dir, list_tickers, load_settings, load_ticker_config


ET = ZoneInfo("America/New_York")


def known_earnings_dates(ticker: str) -> list[date]:
    """从最新一份 snapshot 读出 Yahoo 给的财报日（calendar + earnings_dates），不联网。"""
    folder = data_dir(ticker) / "snapshots"
    paths = sorted(folder.glob("*.json")) if folder.exists() else []
    if not paths:
        return []
    try:
        snap = json.loads(paths[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw: list[str] = []
    cal = snap.get("calendar") or {}
    ed = cal.get("Earnings Date")
    raw.extend(ed if isinstance(ed, list) else [ed] if ed else [])
    raw.extend((snap.get("earnings_dates") or {}).get("index") or [])
    out: set[date] = set()
    for r in raw:
        try:
            out.add(date.fromisoformat(str(r)[:10]))
        except ValueError:
            continue
    return sorted(out)


def next_earnings_date(ticker: str, today: date | None = None) -> date | None:
    today = today or datetime.now(tz=ET).date()
    return next((d for d in known_earnings_dates(ticker) if d >= today), None)


def near_earnings(ticker: str, today: date, before_days: int = 7, after_days: int = 4) -> bool:
    """今天是否落在某个已知财报日的 [-before, +after] 内；没有任何已知日期时保守返回 True。

    Yahoo 的下次财报日在公司确认前常是估计值，窗口放宽以免漏掉真实发布；
    防止 LLM 误触的主要防线是 detect_new_filings 的发布时间过滤，这里只是少打 SEC。
    """
    dates = known_earnings_dates(ticker)
    if not dates:
        return True
    return any(-before_days <= (today - d).days <= after_days for d in dates)


def _hours_since(release_at: str | None, now: datetime) -> float | None:
    if not release_at:
        return None
    try:
        dt = datetime.fromisoformat(release_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 3600


def poll_once(now: datetime | None = None) -> dict[str, Any]:
    """财报窗口内检测新 8-K；等待文字稿的季度在时限内重试 Stage2。无事可做则立即返回。"""
    now = (now or datetime.now(tz=ET)).astimezone(ET)
    polling = load_settings().get("polling") or {}
    transcript_max_hours = float(polling.get("transcript_max_hours", 36))
    results: dict[str, Any] = {"now_et": now.isoformat(), "actions": []}
    any_work = False
    for ticker in list_tickers():
        cfg = load_ticker_config(ticker)
        timing = cfg.get("release_timing") or "amc"
        state_dir = data_dir(ticker) / "state"
        for sp in sorted(state_dir.glob("*.json")) if state_dir.exists() else []:
            st = json.loads(sp.read_text(encoding="utf-8"))
            if st.get("stage") != "stage1_done":
                continue
            period = st["fiscal_period"]
            doc_path = data_dir(ticker) / f"{period}.json"
            release_at = None
            if doc_path.exists():
                release_at = (json.loads(doc_path.read_text(encoding="utf-8")).get("meta") or {}).get("release_at_utc")
            age = _hours_since(release_at, now)
            if age is not None and age > transcript_max_hours:
                results["actions"].append(
                    {
                        "ticker": ticker,
                        "period": period,
                        "action": "stage2",
                        "ok": False,
                        "skipped": True,
                        "detail": f"发布已超过 {transcript_max_hours:.0f} 小时仍无文字稿，停止自动重试；请用 manual 手动跑 Stage2",
                    }
                )
                continue
            any_work = True
            try:
                run_stage2(ticker, period)
                results["actions"].append({"ticker": ticker, "period": period, "action": "stage2", "ok": True})
            except Exception as e:
                results["actions"].append({"ticker": ticker, "period": period, "action": "stage2", "ok": False, "error": str(e)})
        if not _in_window(timing, now) or not near_earnings(ticker, now.date()):
            continue
        if not cfg.get("cik"):
            continue
        any_work = True
        try:
            if not detect_new_filings(ticker, now=now.astimezone(timezone.utc)):
                results["actions"].append({"ticker": ticker, "action": "poll", "ok": True, "detail": "no new 8-K"})
                continue
            doc = run_stage1(ticker)
            results["actions"].append(
                {"ticker": ticker, "action": "stage1", "ok": True, "period": doc["meta"]["fiscal_period"]}
            )
        except Exception as e:
            results["actions"].append({"ticker": ticker, "action": "stage1", "ok": False, "error": str(e)})
    results["did_work"] = any_work
    if not any_work:
        results["skipped"] = True
        results["reason"] = "不在财报窗口/财报日附近，且无等待文字稿的任务"
    return results
