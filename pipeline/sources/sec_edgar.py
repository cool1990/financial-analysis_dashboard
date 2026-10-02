from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from pipeline.config import cache_dir, sec_user_agent


class SecRateLimitError(RuntimeError):
    pass


class SecEdgarClient:
    """SEC EDGAR 客户端：限流 ≤5 rps，403/429/5xx 指数退避。"""

    SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
    COMPANY_TICKERS = "https://www.sec.gov/files/company_tickers.json"
    COMPANY_FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    ARCHIVES_INDEX = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/index.json"
    ARCHIVES_FILE = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{filename}"

    def __init__(self, user_agent: str | None = None, max_rps: float = 5.0):
        self.user_agent = user_agent or sec_user_agent()
        self.min_interval = 1.0 / max_rps
        self._last_request = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.user_agent,
                "Accept-Encoding": "gzip, deflate",
                "Accept": "application/json,text/html,application/xhtml+xml,*/*",
            }
        )

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

    @retry(
        retry=retry_if_exception_type((SecRateLimitError, requests.RequestException)),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def get(self, url: str, *, raw: bool = False) -> Any:
        self._throttle()
        resp = self.session.get(url, timeout=60)
        self._last_request = time.monotonic()
        if resp.status_code in {403, 429} or resp.status_code >= 500:
            raise SecRateLimitError(f"SEC {resp.status_code} for {url}")
        resp.raise_for_status()
        if raw:
            return resp.content
        ctype = resp.headers.get("Content-Type", "")
        if "json" in ctype or url.endswith(".json"):
            return resp.json()
        return resp.text

    def company_tickers(self) -> dict[str, Any]:
        cache = cache_dir() / "sec" / "company_tickers.json"
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        data = self.get(self.COMPANY_TICKERS)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data), encoding="utf-8")
        return data

    def lookup_cik(self, ticker: str) -> str:
        tickers = self.company_tickers()
        want = ticker.upper()
        for row in tickers.values():
            if str(row.get("ticker", "")).upper() == want:
                return f"{int(row['cik_str']):010d}"
        raise KeyError(f"未找到 ticker 对应 CIK: {ticker}")

    def submissions(self, cik: str) -> dict[str, Any]:
        return self.get(self.SUBMISSIONS.format(cik=cik.zfill(10)))

    def company_facts(self, cik: str) -> dict[str, Any]:
        return self.get(self.COMPANY_FACTS.format(cik=cik.zfill(10)))

    def filing_index(self, cik: str, accession: str) -> dict[str, Any]:
        acc = accession.replace("-", "")
        cik_num = str(int(cik))
        return self.get(self.ARCHIVES_INDEX.format(cik=cik_num, accession=acc))

    def download_file(self, cik: str, accession: str, filename: str) -> bytes:
        acc = accession.replace("-", "")
        cik_num = str(int(cik))
        url = self.ARCHIVES_FILE.format(cik=cik_num, accession=acc, filename=filename)
        return self.get(url, raw=True)

    def find_earnings_8k(self, cik: str, *, after: str | None = None) -> list[dict[str, Any]]:
        """返回 Item 2.02 的 8-K 列表（新→旧）。"""
        data = self.submissions(cik)
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        items = recent.get("items", [])
        dates = recent.get("acceptanceDateTime", [])
        accessions = recent.get("accessionNumber", [])
        primary = recent.get("primaryDocument", [])
        out: list[dict[str, Any]] = []
        for i, form in enumerate(forms):
            if form != "8-K":
                continue
            item = items[i] if i < len(items) else ""
            if "2.02" not in str(item):
                continue
            accepted = dates[i] if i < len(dates) else ""
            if after and accepted <= after:
                continue
            out.append(
                {
                    "form": form,
                    "items": item,
                    "acceptanceDateTime": accepted,
                    "accessionNumber": accessions[i],
                    "primaryDocument": primary[i] if i < len(primary) else "",
                }
            )
        return out

    def find_press_release_files(self, cik: str, accession: str) -> list[str]:
        index = self.filing_index(cik, accession)
        items = index.get("directory", {}).get("item", [])
        matches: list[str] = []
        for item in items:
            name = item.get("name", "")
            desc = (item.get("description") or "").upper()
            name_u = name.upper()
            if "EX-99" in name_u or "EX99" in name_u or "EXHIBIT 99" in desc or "PRESS" in desc:
                if name_u.endswith((".HTM", ".HTML", ".TXT")):
                    matches.append(name)
        # Prefer EX-99.1
        matches.sort(key=lambda n: (0 if "99.1" in n.upper() else 1, n))
        return matches

    def download_press_release(self, cik: str, accession: str, dest: Path) -> Path:
        files = self.find_press_release_files(cik, accession)
        if not files:
            raise FileNotFoundError(f"未找到 EX-99 新闻稿: {accession}")
        parts: list[bytes] = []
        for name in files:
            parts.append(self.download_file(cik, accession, name))
        dest.parent.mkdir(parents=True, exist_ok=True)
        content = b"\n".join(parts)
        dest.write_bytes(content)
        return dest


def extract_quarterly_gaap_eps(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """从 companyfacts 提取单季 GAAP diluted EPS。"""
    return _extract_quarterly_fact(facts, ["EarningsPerShareDiluted"])


def extract_quarterly_revenue(facts: dict[str, Any]) -> list[dict[str, Any]]:
    tags = [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ]
    for tag in tags:
        rows = _extract_quarterly_fact(facts, [tag])
        if rows:
            return rows
    return []


def _extract_quarterly_fact(facts: dict[str, Any], tags: list[str]) -> list[dict[str, Any]]:
    gaap = facts.get("facts", {}).get("us-gaap", {})
    out: list[dict[str, Any]] = []
    for tag in tags:
        node = gaap.get(tag)
        if not node:
            continue
        units = node.get("units", {})
        # EPS uses USD/shares; revenue uses USD
        series = units.get("USD/shares") or units.get("USD") or next(iter(units.values()), [])
        for row in series:
            start = row.get("start")
            end = row.get("end")
            if not start or not end:
                continue
            try:
                from datetime import date

                d0 = date.fromisoformat(start)
                d1 = date.fromisoformat(end)
                days = (d1 - d0).days
            except Exception:
                continue
            if not (80 <= days <= 100):
                continue
            out.append(
                {
                    "tag": tag,
                    "start": start,
                    "end": end,
                    "val": row.get("val"),
                    "filed": row.get("filed"),
                    "form": row.get("form"),
                    "fy": row.get("fy"),
                    "fp": row.get("fp"),
                }
            )
        if out:
            break
    # de-dupe by end date, keep latest filed
    by_end: dict[str, dict[str, Any]] = {}
    for row in out:
        prev = by_end.get(row["end"])
        if not prev or str(row.get("filed") or "") >= str(prev.get("filed") or ""):
            by_end[row["end"]] = row
    return sorted(by_end.values(), key=lambda r: r["end"], reverse=True)
