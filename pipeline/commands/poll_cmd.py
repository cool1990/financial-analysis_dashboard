from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pipeline.commands.run_cmd import _in_window, detect_new_filings, run_stage1, run_stage2
from pipeline.config import list_tickers, load_ticker_config
from pipeline.config import data_dir
import json


ET = ZoneInfo("America/New_York")


def poll_once() -> dict[str, Any]:
    """若当前无有股票处于财报窗口或等待文字稿则处理，否则立即退出。"""
    now = datetime.now(tz=ET)
    results: dict[str, Any] = {"now_et": now.isoformat(), "actions": []}
    any_work = False
    for ticker in list_tickers():
        cfg = load_ticker_config(ticker)
        timing = cfg.get("release_timing") or "amc"
        # waiting transcript?
        state_dir = data_dir(ticker) / "state"
        if state_dir.exists():
            for sp in state_dir.glob("*.json"):
                st = json.loads(sp.read_text(encoding="utf-8"))
                if st.get("stage") == "stage1_done":
                    any_work = True
                    period = st["fiscal_period"]
                    try:
                        run_stage2(ticker, period)
                        results["actions"].append({"ticker": ticker, "period": period, "action": "stage2", "ok": True})
                    except Exception as e:
                        results["actions"].append({"ticker": ticker, "period": period, "action": "stage2", "ok": False, "error": str(e)})
        if _in_window(timing, now):
            any_work = True
            try:
                filings = detect_new_filings(ticker)
                if not filings:
                    results["actions"].append({"ticker": ticker, "action": "poll", "ok": True, "detail": "no new 8-K"})
                    continue
                doc = run_stage1(ticker)
                results["actions"].append(
                    {
                        "ticker": ticker,
                        "action": "stage1",
                        "ok": True,
                        "period": doc["meta"]["fiscal_period"],
                    }
                )
            except Exception as e:
                results["actions"].append({"ticker": ticker, "action": "stage1", "ok": False, "error": str(e)})
    if not any_work:
        results["skipped"] = True
        results["reason"] = "不在财报窗口且无等待文字稿的任务"
    return results
