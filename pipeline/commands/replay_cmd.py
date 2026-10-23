"""用历史申报回放 earnings 过滤器。不调用 LLM，也不写 processed.json。"""

from __future__ import annotations

import re
import sys
from collections import Counter
from typing import Any

from pipeline.config import is_hk_listing, load_ticker_config
from pipeline.earnings_config import resolve_earnings_spec
from pipeline.sources.sec_edgar import SecEdgarClient


def replay_earnings_filters(ticker: str) -> dict[str, Any]:
    """扫描 submissions 最近一批。ADR 必须命中 Press release，且主文档噪声不能进 hits。"""
    cfg = load_ticker_config(ticker)
    if is_hk_listing(cfg):
        raise RuntimeError(f"{ticker} listing=hk，不查询 SEC")
    spec = resolve_earnings_spec(cfg)
    if spec.listing != "adr" or spec.earnings_6k is None:
        raise RuntimeError(f"{ticker} 的 listing 不是 adr，没有 6-K 过滤器可回放")
    filters = spec.earnings_6k.model_dump()
    def _progress(hits: int, skips: int) -> None:
        done = hits + skips
        if done == 1 or done % 25 == 0:
            print(f"replay checked={done} hits={hits} skips={skips}", file=sys.stderr, flush=True)

    scan = SecEdgarClient().scan_earnings_filings(cfg["cik"], cfg, progress=_progress)
    reason_counts = Counter(str(row.get("reason") or "") for row in scan.skips)
    examples: dict[str, list[dict[str, str]]] = {}
    for row in scan.skips:
        reason = str(row.get("reason") or "")
        bucket = examples.setdefault(reason, [])
        if len(bucket) >= 3:
            continue
        bucket.append(
            {
                "accessionNumber": str(row.get("accessionNumber") or ""),
                "acceptanceDateTime": str(row.get("acceptanceDateTime") or "")[:10],
                "primaryDocument": str(row.get("primaryDocument") or ""),
            }
        )
    leaked = []
    for hit in scan.hits:
        primary = str(hit.get("primaryDocument") or "")
        for pattern in filters["reject_primary_doc_any"]:
            if re.search(pattern, primary):
                leaked.append(
                    {
                        "accessionNumber": hit.get("accessionNumber"),
                        "primaryDocument": primary,
                        "pattern": pattern,
                    }
                )
                break
    press_hits = []
    other_hits = []
    prefer = str(filters.get("prefer_exhibit_desc") or r"(?i)press\s*release")
    for hit in scan.hits:
        item = {
            "accessionNumber": hit.get("accessionNumber"),
            "acceptanceDateTime": str(hit.get("acceptanceDateTime") or "")[:10],
            "form": hit.get("form"),
            "primaryDocument": hit.get("primaryDocument"),
            "press_release_file": hit.get("press_release_file"),
            "press_description": hit.get("press_description"),
        }
        if re.search(prefer, str(hit.get("press_description") or "")):
            press_hits.append(item)
        else:
            other_hits.append(item)
    fetch_errors = sum(count for reason, count in reason_counts.items() if reason.startswith("fetch_error"))
    # 其它命中（描述不像 Press release）留在报告里人工看，不把它算成主文档噪声泄漏。
    ok = bool(press_hits) and not leaked
    return {
        "ticker": ticker.upper(),
        "listing": spec.listing,
        "forms": spec.earnings_forms,
        "items": spec.earnings_items,
        "reporting_currency": spec.reporting_currency,
        "accounting_basis": spec.accounting_basis,
        "hit_count": len(scan.hits),
        "press_release_hit_count": len(press_hits),
        "other_hit_count": len(other_hits),
        "skip_count": len(scan.skips),
        "skip_reasons": dict(reason_counts),
        "skip_examples": examples,
        "press_release_hits": press_hits,
        "other_hits": other_hits,
        "primary_reject_leaks": leaked,
        "fetch_error_count": fetch_errors,
        "ok": ok,
    }
