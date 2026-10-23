from __future__ import annotations

"""根据最近一份业绩申报推断财季标签（用于 period=latest）。

domestic 用 8-K Item 2.02；adr 用通过过滤器的 6-K。
"""

from pipeline.compute.periods import build_period_info
from pipeline.config import data_dir, is_hk_listing, load_ticker_config
from pipeline.sources.sec_edgar import SecEdgarClient


def resolve_latest_period(ticker: str) -> str:
    """优先已有 data/{TICKER}/FY*.json 最新；否则用最近业绩申报的接受日粗映射。"""
    folder = data_dir(ticker)
    existing = sorted(folder.glob("FY*.json"), reverse=True)
    if existing:
        return existing[0].stem

    cfg = load_ticker_config(ticker)
    if is_hk_listing(cfg):
        raise RuntimeError(f"{ticker} 没有已导入的季度，无法解析 latest")
    if not cfg.get("cik"):
        raise RuntimeError(f"{ticker} 尚未 init，无法解析 latest 财季")
    from pipeline.earnings_config import resolve_earnings_spec

    spec = resolve_earnings_spec(cfg)
    client = SecEdgarClient()
    filings = client.find_earnings_filings(cfg["cik"], cfg, limit=1)
    if not filings:
        kind = "6-K 新闻稿" if spec.listing == "adr" else "Item 2.02 8-K"
        raise RuntimeError(f"{ticker} 未找到{kind}")
    accepted = filings[0]["acceptanceDateTime"][:10]
    info = build_period_info(accepted, cfg["fiscal_year_end_month"])
    return info.fiscal_period
