from __future__ import annotations

import re
from io import StringIO
from pathlib import Path
from typing import Any

import pandas as pd
from bs4 import BeautifulSoup

NON_GAAP_EPS_PATTERNS = [
    r"non[\s\-–—]?gaap.{0,60}(?:diluted\s+)?(?:eps|earnings\s+per\s+share)",
    r"(?:diluted\s+)?(?:eps|earnings\s+per\s+share).{0,40}non[\s\-–—]?gaap",
    r"adjusted\s+(?:diluted\s+)?(?:eps|earnings\s+per\s+share)",
    r"(?:eps|earnings\s+per\s+share)\s+excluding",
    r"before\s+special\s+items",
    r"core\s+eps",
    r"comparable\s+eps",
    r"pro\s+forma\s+eps",
]

RECONCILIATION_PATTERNS = [
    r"reconciliation",
    r"reconcile[sd]?",
    r"non[\s-]?gaap\s+measures?",
]


def html_to_text_and_tables(html: str | bytes) -> dict[str, Any]:
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="ignore")
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    body_text = soup.get_text("\n", strip=True)

    tables_md: list[str] = []
    try:
        dfs = pd.read_html(StringIO(html if isinstance(html, str) else html.decode("utf-8", errors="ignore")))
    except Exception:
        dfs = []
    for i, df in enumerate(dfs):
        # Preserve unit hints from column names / first rows
        md = df.fillna("").to_string(index=False)
        try:
            md = df.to_markdown(index=False)
        except Exception:
            pass
        tables_md.append(f"### Table {i+1}\n{md}")

    return {
        "text": body_text,
        "tables_markdown": tables_md,
        "combined": body_text + "\n\n" + "\n\n".join(tables_md),
    }


def detect_nongaap_eps_regex(text: str) -> dict[str, Any]:
    """确定性预扫描：Non-GAAP EPS 关键词 + 调节表。"""
    lower = text.lower()
    # Exclude "revenue excluding ..."
    hits = []
    for pat in NON_GAAP_EPS_PATTERNS:
        for m in re.finditer(pat, lower, flags=re.IGNORECASE | re.DOTALL):
            snippet = text[max(0, m.start() - 40) : m.end() + 40]
            if re.search(r"revenue\s+excluding", snippet, re.I):
                continue
            hits.append({"pattern": pat, "snippet": snippet.strip()[:200]})
    has_recon = any(re.search(p, lower) for p in RECONCILIATION_PATTERNS)
    has_nongaap = bool(hits) and has_recon
    return {
        "has_nongaap_eps_regex": has_nongaap,
        "keyword_hits": hits,
        "has_reconciliation": has_recon,
    }


def is_preliminary_release(text: str) -> bool:
    head = text[:2000].lower()
    if "preliminary" in head and not re.search(r"consolidated\s+statements?\s+of\s+(operations|income)", text, re.I):
        return True
    return False


def load_press_release(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    parsed = html_to_text_and_tables(raw)
    scan = detect_nongaap_eps_regex(parsed["combined"])
    return {
        "path": str(path),
        **parsed,
        **scan,
        "is_preliminary": is_preliminary_release(parsed["text"]),
    }
