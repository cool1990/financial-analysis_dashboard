"""``hkex-poll``：列出腾讯在披露易上的业绩公布。默认不下载。

不推断 FY 期间，不写 ``FY…/press_release.pdf``，因此不会被 ``import-period`` / Stage1 自动吃掉。
选定财季后，把本命令给出的路径传给 ``import-period --pdf``。
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta

from pipeline import ROOT
from pipeline.config import data_dir, is_hk_listing, load_ticker_config
from pipeline.sources.hkex_news import (
    HkexError,
    HkexNewsClient,
    normalize_stock_code,
    snippet,
)

SUPPORTED = frozenset({"0700.HK"})
DEFAULT_MONTHS = 18
MAX_WINDOW_DAYS = 366 * 3
MAX_LIMIT = 100
IMPORT_HINT = (
    "列出的 PDF 不会写入单季 JSON，也不会进入每天的 poll。"
    "选定财季后手工执行 python -m pipeline import-period --ticker 0700.HK "
    "--period FY… --pdf <path>。"
    "中期公告同时包含三个月和六个月，期间必须人工指定；本命令不推断 FY 期间。"
    "历史整包回补不在这个原型里。"
)


def hong_kong_today() -> date:
    return datetime.now(ZoneInfo("Asia/Hong_Kong")).date()


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HkexError(f"日期需为 YYYY-MM-DD: {value}") from exc


def resolve_window(
    since: str | None,
    until: str | None,
    *,
    today: date,
) -> tuple[date, date]:
    until_day = _parse_date(until) if until else today
    since_day = _parse_date(since) if since else until_day - relativedelta(months=DEFAULT_MONTHS)
    if since_day > until_day:
        raise HkexError(f"since {since_day.isoformat()} 晚于 until {until_day.isoformat()}")
    if (until_day - since_day).days > MAX_WINDOW_DAYS:
        raise HkexError(
            "查询窗口超过 3 年。hkex-poll 是试跑轮询，不是历史回补；请缩短 --since/--until，"
            "或使用另行准备的本地 PDF 回补。"
        )
    return since_day, until_day


def stock_code_from_config(cfg: dict[str, Any], ticker: str) -> str:
    raw = str((cfg.get("hkex") or {}).get("stock_code") or "").strip()
    if raw:
        return normalize_stock_code(raw)
    matched = re.fullmatch(r"(\d{1,5})\.HK", ticker.upper())
    if not matched:
        raise HkexError(f"{ticker} 没有 hkex.stock_code，也无法从代码推出 5 位港股代号")
    return normalize_stock_code(matched.group(1))


def _safe_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "", value)


def pdf_filename(item: dict[str, Any]) -> str:
    day = str(item["published_at"])[:10]
    token = _safe_token(str(item.get("news_id") or ""))
    if not token:
        token = hashlib.sha256(str(item["url"]).encode()).hexdigest()[:12]
    return f"{day}_{item['doc_type']}_{token}.pdf"


def _sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(ROOT))
    except ValueError:
        return str(resolved)


def store_pdf(client: HkexNewsClient, item: dict[str, Any], folder: Path) -> dict[str, str]:
    file_type = str(item.get("file_type") or "").upper()
    if file_type and file_type != "PDF":
        raise HkexError(f"不是 PDF（{file_type}），未落盘", url=str(item.get("url") or ""))
    url = str(item["url"])
    if not url.lower().split("?", 1)[0].endswith(".pdf"):
        raise HkexError("链接不是 .pdf，未落盘", url=url)
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / pdf_filename(item)
    if dest.exists() and dest.stat().st_size > 0:
        existing = dest.read_bytes()
        if existing.startswith(b"%PDF"):
            return {"path": _display_path(dest), "sha256": _sha256(existing), "download": "skipped_existing"}
    blob = client.download_pdf(url)
    if not blob.startswith(b"%PDF"):
        raise HkexError("响应不是 PDF", url=url, snippet=snippet(blob))
    dest.write_bytes(blob)
    return {"path": _display_path(dest), "sha256": _sha256(blob), "download": "saved"}


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": item["title"],
        "published_at": item["published_at"],
        "doc_type": item["doc_type"],
        "url": item["url"],
        "headline": item.get("headline") or "",
        "news_id": item.get("news_id") or "",
    }


def hkex_poll(
    ticker: str,
    *,
    since: str | None = None,
    until: str | None = None,
    limit: int = 20,
    download: bool = False,
    today: date | None = None,
    client: HkexNewsClient | None = None,
    dest_dir: Path | None = None,
) -> dict[str, Any]:
    ticker = ticker.upper()
    try:
        cfg = load_ticker_config(ticker)
    except FileNotFoundError as exc:
        raise HkexError(
            f"缺少股票配置: {ticker}",
            hint="先添加 config/tickers/{ticker}.yaml，且 listing: hk。原型目前只支持 0700.HK。",
        ) from exc
    if not is_hk_listing(cfg):
        raise HkexError(f"{ticker} 不是 listing=hk，hkex-poll 只查港交所")
    if ticker not in SUPPORTED:
        raise HkexError("hkex-poll 原型目前只支持 0700.HK（腾讯控股）")
    if limit < 1 or limit > MAX_LIMIT:
        raise HkexError(f"--limit 需在 1 到 {MAX_LIMIT} 之间")
    since_day, until_day = resolve_window(since, until, today=today or hong_kong_today())
    code = stock_code_from_config(cfg, ticker)
    finder = client or HkexNewsClient()
    found = finder.poll(stock_code=code, since=since_day, until=until_day)
    matched = list(found["items"])
    shown = matched[:limit]
    limited = len(matched) > len(shown)
    warnings = list(found["warnings"])
    if limited:
        warnings.append(f"匹配 {len(matched)} 条，按 --limit {limit} 只输出最新的 {len(shown)} 条。")
    folder = dest_dir or (data_dir(ticker) / "raw" / "hkex")
    public: list[dict[str, Any]] = []
    saved = 0
    download_errors = 0
    for item in shown:
        out = _public_item(item)
        if download:
            try:
                stored = store_pdf(finder, item, folder)
            except HkexError as exc:
                download_errors += 1
                out["download_error"] = exc.to_dict()
                warnings.append(f"下载失败: {item['title'][:40]}")
            else:
                out["path"] = stored["path"]
                out["sha256"] = stored["sha256"]
                out["download"] = stored["download"]
                if stored["download"] == "saved":
                    saved += 1
        public.append(out)
    return {
        "ok": download_errors == 0,
        "ticker": ticker,
        "stock_code": found["stock_code"],
        "stock_id": found["stock_id"],
        "stock_name": found["stock_name"],
        "dry_run": not download,
        "since": since_day.isoformat(),
        "until": until_day.isoformat(),
        "limit": limit,
        "matched": len(matched),
        "skipped": found["skipped"],
        "fetched": found["fetched"],
        "truncated": bool(found["truncated"]),
        "limit_applied": limited,
        "saved": saved,
        "source": found["source"],
        "requests": found["requests"],
        "warnings": warnings,
        "items": public,
        "import_hint": IMPORT_HINT,
    }
