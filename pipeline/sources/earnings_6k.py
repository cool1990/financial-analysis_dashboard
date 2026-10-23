"""ADR 6-K 业绩识别：exhibit 描述过滤，不写死单票文件名。

主规则（必须同时考虑）：
- form 属于 earnings_forms（6-K 自动包含 6-K/A）
- 至少一个 exhibit 描述命中 require_exhibit_desc_any
- 若只命中 reject_exhibit_desc_any（例如合并报表）且没有 Press release，则跳过

辅规则：primaryDocument 命中 reject_primary_doc_any 时直接跳过，不能单靠它认定业绩。
封面 HTML 里的 exhibit 链接文字才是 TSM 这类 FPI 的「Press release」描述；
SEC index.json 的 description 经常只是 EX-99.1。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from html import unescape
from typing import Any


_ANCHOR_RE = re.compile(
    r"<a\b[^>]*\bhref=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_PRESENTATION_RE = re.compile(r"(?i)presentation")


def accepted_datetime(raw: str | None) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    if "." in text:
        head, tail = text.split(".", 1)
        digits: list[str] = []
        rest = ""
        for index, char in enumerate(tail):
            if char.isdigit() and len(digits) < 6:
                digits.append(char)
            elif char.isdigit():
                continue
            else:
                rest = tail[index:]
                break
        text = head + ("." + "".join(digits) if digits else "") + rest
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_newer_than(accepted: str, after: str | None) -> bool:
    """accepted 是否晚于 after。无法解析时退回字符串比较。"""
    if not after:
        return True
    left = accepted_datetime(accepted)
    right = accepted_datetime(after)
    if left is not None and right is not None:
        return left > right
    return accepted > after


def form_matches(form: str, forms: list[str], *, amendments: bool) -> bool:
    text = (form or "").strip().upper()
    wanted = {(item or "").strip().upper() for item in forms}
    if text in wanted:
        return True
    if amendments and text.endswith("/A") and text[:-2] in wanted:
        return True
    return False


def _matches_any(patterns: list[str], text: str) -> bool:
    return any(re.search(pattern, text or "") for pattern in patterns)


def _filename_from_href(href: str) -> str:
    text = unescape(href or "").strip()
    text = text.split("#", 1)[0].split("?", 1)[0].strip()
    if not text or text.lower().startswith(("javascript:", "mailto:")):
        return ""
    return text.rsplit("/", 1)[-1]


def _anchor_text(inner: str) -> str:
    text = unescape(_TAG_RE.sub("", inner or ""))
    return _WS_RE.sub(" ", text).strip()


def exhibit_descriptions_from_html(html: str) -> dict[str, str]:
    """封面（或索引 HTML）里指向同一文件的链接文字拼成描述。"""
    grouped: dict[str, list[str]] = {}
    for href, inner in _ANCHOR_RE.findall(html or ""):
        name = _filename_from_href(href)
        if not name or "." not in name:
            continue
        piece = _anchor_text(inner)
        if not piece:
            continue
        grouped.setdefault(name, [])
        if not grouped[name] or grouped[name][-1] != piece:
            grouped[name].append(piece)
    return {name: _WS_RE.sub(" ", "".join(parts)).strip() for name, parts in grouped.items()}


def exhibits_from_index(index: dict[str, Any] | None) -> list[dict[str, str]]:
    items = ((index or {}).get("directory") or {}).get("item") or []
    out: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        out.append(
            {
                "name": name,
                "description": "",
                "index_description": str(item.get("description") or "").strip(),
            }
        )
    return out


def merge_exhibits(index_items: list[dict[str, str]], cover_descriptions: dict[str, str]) -> list[dict[str, str]]:
    """封面描述优先。index.json 经常只有 EX-99.1，不能拿它代替 Press release。"""
    by_name = {item["name"]: dict(item) for item in index_items}
    for name, description in cover_descriptions.items():
        if name in by_name:
            by_name[name]["description"] = description
        else:
            by_name[name] = {"name": name, "description": description, "index_description": ""}
    merged: list[dict[str, str]] = []
    for item in by_name.values():
        if not item.get("description"):
            item["description"] = item.get("index_description") or ""
        merged.append(item)
    return merged


def classify_6k(
    primary_document: str,
    exhibits: list[dict[str, str]],
    filters: dict[str, Any],
) -> tuple[str, str]:
    """返回 (hit|skip, reason)。primary 拒绝优先于 exhibit，且不能单独认定业绩。"""
    primary = primary_document or ""
    if _matches_any(list(filters.get("reject_primary_doc_any") or []), primary):
        return "skip", "primary_doc"
    require = list(filters.get("require_exhibit_desc_any") or [])
    reject = list(filters.get("reject_exhibit_desc_any") or [])
    prefer = str(filters.get("prefer_exhibit_desc") or r"(?i)press\s*release")
    texts = [str(item.get("description") or "") for item in exhibits]
    has_require = any(_matches_any(require, text) for text in texts) if require else False
    has_reject = any(_matches_any(reject, text) for text in texts)
    has_press = any(re.search(prefer, text or "") for text in texts)
    if has_reject and not has_press:
        return "skip", "reject_exhibit"
    if not has_require:
        return "skip", "no_exhibit_match"
    return "hit", "exhibit_desc"


def _is_html_name(name: str) -> bool:
    lower = name.lower()
    if not lower.endswith((".htm", ".html", ".txt")):
        return False
    if lower.endswith("-index.html") or lower.endswith("-index-headers.html"):
        return False
    return True


def _is_ex99(item: dict[str, str]) -> bool:
    name = item.get("name", "").upper()
    index_description = item.get("index_description", "").upper()
    description = item.get("description", "").upper()
    blob = " ".join((name, index_description, description))
    return any(
        token in blob
        for token in ("EX-99", "EX99", "EXHIBIT99", "EXHIBIT 99")
    )


def _is_ex991(item: dict[str, str]) -> bool:
    name = item.get("name", "").upper()
    index_description = item.get("index_description", "").upper()
    return (
        "99.1" in name
        or "EXHIBIT991" in name
        or "EX-99.1" in index_description
        or index_description.strip() == "EX-99.1"
    )


def select_press_and_presentation(
    exhibits: list[dict[str, str]],
    filters: dict[str, Any],
    *,
    primary_document: str = "",
) -> tuple[dict[str, str] | None, dict[str, str] | None]:
    """Stage1 只取 Press release。排序：prefer 描述 → EX-99.1 文件名 → 其它 EX-99。"""
    prefer = str(filters.get("prefer_exhibit_desc") or r"(?i)press\s*release")
    require = list(filters.get("require_exhibit_desc_any") or [])
    docs = [item for item in exhibits if _is_html_name(item.get("name") or "")]

    def sort_key(item: dict[str, str]) -> tuple:
        description = item.get("description") or ""
        prefer_hit = 0 if re.search(prefer, description) else 1
        require_hit = 0 if _matches_any(require, description) else 1
        presentation_penalty = 1 if _PRESENTATION_RE.search(description) else 0
        return (
            prefer_hit,
            require_hit,
            0 if _is_ex991(item) else 1,
            0 if _is_ex99(item) else 1,
            presentation_penalty,
            item["name"].upper(),
        )

    press: dict[str, str] | None = None
    for item in sorted(docs, key=sort_key):
        description = item.get("description") or ""
        name = item.get("name") or ""
        if name == primary_document and not re.search(prefer, description):
            continue
        if _PRESENTATION_RE.search(description) and not re.search(prefer, description):
            continue
        if re.search(prefer, description) or _matches_any(require, description) or _is_ex99(item):
            press = item
            break

    presentation: dict[str, str] | None = None
    for item in docs:
        if press and item.get("name") == press.get("name"):
            continue
        description = item.get("description") or ""
        name = (item.get("name") or "").upper()
        index_description = (item.get("index_description") or "").upper()
        if item.get("name") == primary_document:
            continue
        if _PRESENTATION_RE.search(description) or "EX-99.2" in index_description or "99.2" in name:
            presentation = item
            break
    return press, presentation
