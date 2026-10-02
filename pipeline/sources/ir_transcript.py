from __future__ import annotations

import io
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from pipeline.sources.http_fetch import build_session, http_get_bytes, http_get_text
from pipeline.sources.motley_fool import parse_fiscal_period


def _format_template(template: str, fy: int, q: int) -> str:
    yy = f"{fy % 100:02d}"
    return (
        template.replace("{fy}", str(fy))
        .replace("{year}", str(fy))
        .replace("{q}", str(q))
        .replace("{Q}", str(q))
        .replace("{yy}", yy)
        .replace("{YY}", yy)
    )


def extract_pdf_text(data: bytes) -> str | None:
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception:
        return None
    parts: list[str] = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(parts)).strip()
    return text if len(text) >= 400 else None


def extract_html_document_text(html: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body
    if not main:
        return None
    text = main.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text if len(text) >= 400 else None


def _load_url_text(url: str, *, session) -> tuple[str | None, str | None]:
    try:
        status, final_url, content, ctype = http_get_bytes(url, session=session)
    except Exception:
        return None, None
    if status != 200:
        return None, None
    low_ctype = (ctype or "").lower()
    low_url = final_url.lower()
    if "pdf" in low_ctype or low_url.endswith(".pdf"):
        text = extract_pdf_text(content)
        return (text, final_url) if text else (None, None)
    # 有些 CDN 不带 content-type
    if content[:4] == b"%PDF":
        text = extract_pdf_text(content)
        return (text, final_url) if text else (None, None)
    try:
        html = content.decode("utf-8", errors="replace")
    except Exception:
        return None, None
    text = extract_html_document_text(html)
    return (text, final_url) if text else (None, None)


def _find_links_on_listing(html: str, base_url: str, pattern: str) -> list[str]:
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error:
        rx = re.compile(re.escape(pattern), re.IGNORECASE)
    soup = BeautifulSoup(html, "lxml")
    found: list[str] = []
    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True)
        href = urljoin(base_url, a["href"])
        blob = f"{label} {href}"
        if rx.search(blob) and href not in found:
            found.append(href)
    return found


def fetch_ir_transcript(
    ticker_cfg: dict[str, Any],
    *,
    fiscal_period: str,
) -> tuple[str | None, str | None]:
    """IR 备选：直接 URL / 模板 PDF / 列表页匹配链接。返回 (text, source_url)。"""
    parsed = parse_fiscal_period(fiscal_period)
    if not parsed:
        return None, None
    fy, q = parsed
    sess = build_session()
    candidates: list[str] = []

    prepared = ticker_cfg.get("ir_prepared_remarks_url")
    if isinstance(prepared, str) and prepared.strip():
        candidates.append(_format_template(prepared.strip(), fy, q))

    listing = ticker_cfg.get("ir_transcript_url")
    match = ticker_cfg.get("ir_transcript_match") or r"(?i)transcript|prepared.?remarks"
    if isinstance(listing, str) and listing.strip():
        url = _format_template(listing.strip(), fy, q)
        # 若本身像文档，直接试；否则当列表页
        if re.search(r"\.(pdf|html?)$", url, re.IGNORECASE) or "static-files" in url:
            candidates.append(url)
        else:
            try:
                status, final_url, html = http_get_text(url, session=sess)
                if status == 200:
                    candidates.extend(_find_links_on_listing(html, final_url, str(match)))
            except Exception:
                pass

    seen: set[str] = set()
    for url in candidates:
        if not url or url in seen:
            continue
        seen.add(url)
        text, final_url = _load_url_text(url, session=sess)
        if text:
            return text, final_url
    return None, None
