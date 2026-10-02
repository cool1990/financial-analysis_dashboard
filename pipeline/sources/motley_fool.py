from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote_plus, unquote, urlparse

from bs4 import BeautifulSoup

from pipeline.sources.http_fetch import build_session, http_get, http_get_text

FOOL_TRANSCRIPT_RE = re.compile(
    r"https?://(?:www\.)?fool\.com/earnings/call-transcripts/\d{4}/\d{2}/\d{2}/[^/?#]+/?",
    re.IGNORECASE,
)


def parse_fiscal_period(period: str) -> tuple[int, int] | None:
    """FY2026Q4 -> (2026, 4)。"""
    m = re.fullmatch(r"FY(\d{4})Q([1-4])", period.strip(), re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)


def company_slug_candidates(name: str, ticker: str) -> list[str]:
    full = _slugify(name)
    first = full.split("-")[0] if full else ticker.lower()
    out: list[str] = []
    for s in (first, full):
        if s and s not in out:
            out.append(s)
    return out or [ticker.lower()]


def ticker_token_ok(haystack: str, ticker: str) -> bool:
    """避免 ALMU 误匹配 MU：要求 ticker 两侧非字母。"""
    return bool(re.search(rf"(?<![A-Za-z]){re.escape(ticker)}(?![A-Za-z])", haystack, re.IGNORECASE))


def period_tokens(fy: int, q: int) -> list[str]:
    return [
        f"q{q}-{fy}",
        f"q{q}{fy}",
        f"{fy}-q{q}",
        f"q{q}-{fy}-earnings",
    ]


def matches_period_and_ticker(url_or_title: str, ticker: str, fy: int, q: int) -> bool:
    if not ticker_token_ok(url_or_title, ticker):
        return False
    low = url_or_title.lower()
    return any(tok in low for tok in period_tokens(fy, q))


def extract_transcript_text(html: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    body = soup.select_one("div.article-body.transcript-content") or soup.select_one(
        "div.transcript-content"
    )
    if not body:
        # 兜底：取最长 article
        articles = soup.find_all("article")
        body = max(articles, key=lambda e: len(e.get_text(" ", strip=True)), default=None)
    if not body:
        return None
    text = body.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 800:
        return None
    # 粗筛：至少像电话会
    markers = ("Operator", "Operator:", "Question-and-Answer", "Q&A", "earnings")
    if not any(m.lower() in text.lower() for m in markers):
        # 仍可能有用，长度够就收下
        if len(text) < 3000:
            return None
    return text


def _parse_release_day(release_at: str | None) -> date | None:
    if not release_at:
        return None
    try:
        s = release_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).date()
    except ValueError:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", release_at)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def candidate_slugs(name: str, ticker: str, fy: int, q: int) -> list[str]:
    t = ticker.lower()
    out: list[str] = []
    for company in company_slug_candidates(name, ticker):
        for suffix in (
            f"{company}-{t}-q{q}-{fy}-earnings-call-transcript",
            f"{company}-{t}-q{q}-{fy}-earnings-call-transcr",
            f"{company}-{t}-q{q}-{fy}-earnings-transcript",
            f"{t}-q{q}-{fy}-earnings-call-transcript",
        ):
            if suffix not in out:
                out.append(suffix)
    return out


def probe_urls_by_date(
    name: str,
    ticker: str,
    fy: int,
    q: int,
    release_at: str | None,
    *,
    session=None,
    day_window: int = 10,
) -> list[str]:
    day0 = _parse_release_day(release_at) or date.today()
    sess = session or build_session()
    found: list[str] = []
    for offset in range(0, day_window + 1):
        day = day0 + timedelta(days=offset)
        for slug in candidate_slugs(name, ticker, fy, q):
            url = (
                f"https://www.fool.com/earnings/call-transcripts/"
                f"{day.year:04d}/{day.month:02d}/{day.day:02d}/{slug}/"
            )
            try:
                resp = http_get(url, session=sess)
            except Exception:
                continue
            if resp.status_code == 200 and extract_transcript_text(resp.text):
                found.append(str(resp.url))
                return found  # 找到即停
    return found


def _unwrap_ddg_href(href: str) -> str:
    # DuckDuckGo 有时包一层 //duckduckgo.com/l/?uddg=...
    if "uddg=" in href:
        m = re.search(r"uddg=([^&]+)", href)
        if m:
            return unquote(m.group(1))
    if href.startswith("//"):
        return "https:" + href
    return href


def search_duckduckgo(ticker: str, fy: int, q: int, *, session=None) -> list[str]:
    query = f"site:fool.com/earnings/call-transcripts {ticker} Q{q} {fy}"
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    sess = session or build_session()
    try:
        status, _, html = http_get_text(url, session=sess)
    except Exception:
        return []
    if status != 200:
        return []
    soup = BeautifulSoup(html, "lxml")
    urls: list[str] = []
    for a in soup.select("a.result__a[href], a[href]"):
        href = _unwrap_ddg_href(a.get("href") or "")
        if not FOOL_TRANSCRIPT_RE.search(href):
            # 有时只有 path
            if "/earnings/call-transcripts/" in href and "fool.com" in href:
                pass
            else:
                continue
        m = FOOL_TRANSCRIPT_RE.search(href)
        if not m:
            continue
        cand = m.group(0)
        title = a.get_text(" ", strip=True)
        blob = f"{cand} {title}"
        if matches_period_and_ticker(blob, ticker, fy, q) and cand not in urls:
            urls.append(cand.rstrip("/") + "/")
    return urls


def fetch_motley_fool_transcript(
    ticker_cfg: dict[str, Any],
    *,
    fiscal_period: str,
    release_at: str | None = None,
) -> tuple[str | None, str | None]:
    """返回 (text, source_url)。"""
    ticker = (ticker_cfg.get("ticker") or "").upper()
    name = ticker_cfg.get("name") or ticker
    parsed = parse_fiscal_period(fiscal_period)
    if not ticker or not parsed:
        return None, None
    fy, q = parsed
    sess = build_session()

    # 配置可直接指定 URL
    direct = ticker_cfg.get("motley_fool_url")
    candidates: list[str] = []
    if isinstance(direct, str) and direct.strip():
        candidates.append(direct.strip())

    candidates.extend(search_duckduckgo(ticker, fy, q, session=sess))
    if not candidates:
        candidates.extend(
            probe_urls_by_date(name, ticker, fy, q, release_at, session=sess)
        )

    seen: set[str] = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        if not matches_period_and_ticker(url, ticker, fy, q) and not (
            isinstance(direct, str) and url == direct.strip()
        ):
            # DDG 已过滤；probe 也应匹配。direct 放行。
            if not ticker_token_ok(url, ticker):
                continue
        try:
            status, final_url, html = http_get_text(url, session=sess)
        except Exception:
            continue
        if status != 200:
            continue
        text = extract_transcript_text(html)
        if text:
            return text, final_url
    return None, None
