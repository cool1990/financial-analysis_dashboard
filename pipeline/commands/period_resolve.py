from __future__ import annotations

"""根据 EDGAR 最近一份 Item 2.02 8-K 推断财季标签（用于 period=latest）。"""

from pipeline.compute.periods import build_period_info
from pipeline.config import data_dir, load_ticker_config
from pipeline.sources.sec_edgar import SecEdgarClient


def resolve_latest_period(ticker: str) -> str:
    """优先已有 data/{TICKER}/FY*.json 最新；否则用最近 8-K acceptance 日期粗映射。"""
    folder = data_dir(ticker)
    existing = sorted(folder.glob("FY*.json"), reverse=True)
    if existing:
        return existing[0].stem

    cfg = load_ticker_config(ticker)
    if not cfg.get("cik"):
        raise RuntimeError(f"{ticker} 尚未 init，无法解析 latest 财季")
    client = SecEdgarClient()
    filings = client.find_earnings_8k(cfg["cik"])
    if not filings:
        raise RuntimeError(f"{ticker} 未找到 Item 2.02 8-K")
    accepted = filings[0]["acceptanceDateTime"][:10]
    info = build_period_info(accepted, cfg["fiscal_year_end_month"])
    return info.fiscal_period
