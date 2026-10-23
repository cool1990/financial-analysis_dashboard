from __future__ import annotations

from datetime import date
from typing import Any

from pipeline.compute.periods import build_period_info
from pipeline.config import is_hk_listing, load_ticker_config, save_ticker_config
from pipeline.earnings_config import resolve_earnings_spec
from pipeline.extract.press_release import detect_nongaap_eps_regex, load_press_release
from pipeline.sources.sec_edgar import (
    SecEdgarClient,
    extract_quarterly_gaap_eps,
)
from pipeline.sources.yfinance_src import YFinanceSource, history_eps_pairs
from pipeline.config import data_dir
from pipeline.validate import classify_eps_basis


def init_ticker(ticker: str, *, force: bool = False) -> dict[str, Any]:
    cfg = load_ticker_config(ticker)
    if is_hk_listing(cfg):
        return cfg
    client = SecEdgarClient()
    if not cfg.get("cik") or force:
        cfg["cik"] = client.lookup_cik(ticker)

    spec = resolve_earnings_spec(cfg)
    # 最新业绩新闻稿，用来看有没有 Non-GAAP EPS。ADR 走 6-K，不用 8-K Item 2.02。
    filings = client.find_earnings_filings(cfg["cik"], cfg, limit=1)
    has_nongaap = False
    if filings:
        latest = filings[0]
        raw_path = data_dir(ticker) / "raw" / "_init" / "press_release.html"
        try:
            client.download_press_release(
                cfg["cik"],
                latest["accessionNumber"],
                raw_path,
                press_release_file=latest.get("press_release_file") or None,
            )
            parsed = load_press_release(raw_path)
            has_nongaap = bool(parsed.get("has_nongaap_eps_regex"))
        except Exception as e:
            cfg.setdefault("eps_basis_evidence", [])
            cfg["eps_basis_evidence"] = [{"warning": f"init press release failed: {e}"}]

    if spec.listing == "adr":
        # FPI 的 companyfacts 不是 us-gaap 主历史，不能拿来改 eps_basis。
        basis = cfg.get("eps_basis_override") or cfg.get("eps_basis") or "gaap"
        cfg["eps_basis"] = basis
        cfg["eps_basis_evidence"] = {
            "has_nongaap_eps": has_nongaap,
            "gaap_match_ratio": None,
            "paired_quarters": 0,
            "pairs": [],
            "notes": [
                "ADR 业绩来自 6-K 新闻稿，不用 us-gaap companyfacts 决定 EPS 口径。",
                f"has_nongaap_eps_regex={has_nongaap}",
            ],
        }
        save_ticker_config(ticker, cfg)
        return cfg

    # pair provider reported vs XBRL GAAP EPS
    yf = YFinanceSource(ticker)
    hist = history_eps_pairs(yf.earnings_history())
    facts = client.company_facts(cfg["cik"])
    gaap_rows = extract_quarterly_gaap_eps(facts)
    splits = yf.splits()

    def adjust_for_splits(eps: float, as_of: str) -> float:
        # apply splits after as_of date (yahoo adjusts historical; XBRL does not)
        factor = 1.0
        for d, ratio in splits.items():
            if d > as_of:
                factor *= float(ratio)
        return eps / factor if factor else eps

    pairs = []
    for h in hist[:12]:
        end = h.get("period_end")
        actual = h.get("epsActual")
        if end is None or actual is None:
            continue
        best = None
        best_abs = 8
        for g in gaap_rows:
            try:
                delta = abs((date.fromisoformat(g["end"]) - date.fromisoformat(str(end)[:10])).days)
            except Exception:
                continue
            if delta <= 7 and delta < best_abs:
                best = g
                best_abs = delta
        if not best:
            continue
        gaap_eps = adjust_for_splits(float(best["val"]), best["end"])
        pairs.append(
            {
                "period_end": str(end)[:10],
                "provider_reported": float(actual),
                "gaap_xbrl": gaap_eps,
                "abs_diff": abs(float(actual) - gaap_eps),
                "match": abs(float(actual) - gaap_eps) <= 0.01,
            }
        )

    paired = len(pairs)
    match_ratio = (sum(1 for p in pairs if p["match"]) / paired) if paired else None
    basis, notes = classify_eps_basis(
        has_nongaap_eps=has_nongaap,
        gaap_match_ratio=match_ratio,
        paired_quarters=paired,
        gaap_nongaap_differ=True,
    )
    if cfg.get("eps_basis_override"):
        basis = cfg["eps_basis_override"]
    cfg["eps_basis"] = basis
    cfg["eps_basis_evidence"] = {
        "has_nongaap_eps": has_nongaap,
        "gaap_match_ratio": match_ratio,
        "paired_quarters": paired,
        "pairs": pairs,
        "notes": notes,
    }
    save_ticker_config(ticker, cfg)
    return cfg


def init_missing() -> dict[str, Any]:
    """daily 调用：给还没有 CIK 的股票做初始化（SEC + Yahoo，不调用 LLM）。新加股票无需手动 init。"""
    from pipeline.config import list_tickers, load_ticker_config

    out: dict[str, Any] = {}
    for t in list_tickers():
        cfg = load_ticker_config(t)
        if is_hk_listing(cfg) or cfg.get("cik"):
            continue
        try:
            cfg = init_ticker(t)
            out[t] = {"ok": True, "cik": cfg.get("cik"), "eps_basis": cfg.get("eps_basis")}
        except Exception as e:
            out[t] = {"ok": False, "error": str(e)}
    return out
