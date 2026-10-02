from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from pipeline.config import data_dir, list_tickers, load_ticker_config
from pipeline.sources.yfinance_src import YFinanceSource


def _next_fiscal_guess(ticker: str) -> str | None:
    """粗略绑定 target_fiscal_period：依赖 calendar / earnings dates。"""
    cfg = load_ticker_config(ticker)
    # placeholder — refined when calendar known
    return None


def take_snapshot(ticker: str) -> dict[str, Any]:
    yf = YFinanceSource(ticker)
    bundle = yf.snapshot_bundle()
    # Bind 0q to upcoming fiscal period when possible
    bundle["target_fiscal_period"] = _infer_target_period(bundle, load_ticker_config(ticker))
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = data_dir(ticker) / "snapshots" / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        # 只追加不覆盖：若已存在则写带时间戳文件
        path = data_dir(ticker) / "snapshots" / f"{day}T{datetime.now(timezone.utc).strftime('%H%M%S')}.json"
    path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    return bundle


def take_all_snapshots() -> dict[str, Any]:
    out = {}
    for t in list_tickers():
        try:
            out[t] = {"ok": True, "snapshot": take_snapshot(t)}
        except Exception as e:
            out[t] = {"ok": False, "error": str(e)}
    return out


def _infer_target_period(bundle: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    # Prefer earnings_dates next upcoming
    ed = bundle.get("earnings_dates") or {}
    # Without robust mapping, leave null; run/poll will bind pre_earnings snapshot by acceptance time
    return None


def load_pre_earnings_snapshot(ticker: str, release_at_utc: str) -> dict[str, Any] | None:
    """取 snapshot_at 早于 release 的最后一条。"""
    folder = data_dir(ticker) / "snapshots"
    if not folder.exists():
        return None
    release = datetime.fromisoformat(release_at_utc.replace("Z", "+00:00"))
    best = None
    best_ts = None
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            ts = datetime.fromisoformat(str(data.get("snapshot_at")).replace("Z", "+00:00"))
        except Exception:
            continue
        if ts < release and (best_ts is None or ts > best_ts):
            best = data
            best_ts = ts
    if best:
        best = dict(best)
        best["pre_earnings"] = True
    return best
