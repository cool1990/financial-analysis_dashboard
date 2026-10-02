from __future__ import annotations

import re
from pathlib import Path
from typing import Any

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


def _join_cells(cells: list[str]) -> str:
    """把一行单元格拼成一行文本；合并 `$` / `%` / `)` 等被拆到独立单元格的符号。"""
    line = " ".join(c for c in cells if c)
    line = re.sub(r"\$\s+(?=[\d(.])", "$", line)
    line = re.sub(r"(?<=[\d)])\s+%", "%", line)
    line = re.sub(r"(?<=\d)\s+\)", ")", line)
    line = re.sub(r"\(\s+(?=[\d$])", "(", line)
    return line


def _table_rows(table: Any) -> list[str]:
    rows: list[str] = []
    for tr in table.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        line = _join_cells(cells)
        if line:
            rows.append(line)
    return rows


def html_to_text_and_tables(html: str | bytes) -> dict[str, Any]:
    """HTML → 纯文本。

    表格按"一行一条"展开到正文中（单元格以空格连接），这样：
    - LLM 看到的表格行与校验用的正文完全一致，引用表格行可以被精确匹配；
    - 不再额外拼接 pandas markdown 表（含大量重复列 / nan，体积约为正文 6 倍，
      会把后半部分报表挤出 80k 截断窗口）。
    """
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="ignore")
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()

    tables_md: list[str] = []
    # 先处理最内层表格，避免嵌套表格被重复展开
    for i, table in enumerate(reversed(soup.find_all("table"))):
        rows = _table_rows(table)
        if rows:
            tables_md.append(f"### Table {i+1}\n" + "\n".join(rows))
        table.replace_with(soup.new_string("\n" + "\n".join(rows) + "\n"))
    tables_md.reverse()

    body_text = soup.get_text("\n", strip=True)
    body_text = re.sub(r"[ \t\u00a0]+", " ", body_text)

    return {
        "text": body_text,
        "tables_markdown": tables_md,
        "combined": body_text,
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
