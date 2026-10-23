from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from pipeline.config import cache_dir, sec_user_agent
from pipeline.earnings_config import resolve_earnings_spec
from pipeline.sources.earnings_6k import (
    classify_6k,
    exhibit_descriptions_from_html,
    exhibits_from_index,
    form_matches,
    is_newer_than,
    merge_exhibits,
    select_press_and_presentation,
)


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

    def iter_filing_rows(self, cik: str) -> list[dict[str, Any]]:
        """submissions 最近一批申报（新→旧）。历史归档不并进默认扫描。"""
        data = self.submissions(cik)
        recent = data.get("filings", {}).get("recent", {})
        return _rows_from_columns(recent)

    def find_earnings_8k(
        self,
        cik: str,
        *,
        after: str | None = None,
        forms: list[str] | None = None,
        items: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """返回 Item 2.02 的 8-K 列表（新→旧）。

        forms/items 缺省时保持原行为：只要 8-K，且 items 含 2.02。
        """
        wanted_forms = forms or ["8-K"]
        item_filters = ["2.02"] if items is None else list(items)
        out: list[dict[str, Any]] = []
        for row in self.iter_filing_rows(cik):
            if row["form"] not in wanted_forms:
                continue
            item = str(row.get("items") or "")
            if item_filters and not any(code in item for code in item_filters):
                continue
            accepted = row.get("acceptanceDateTime") or ""
            if after and accepted <= after:
                continue
            out.append(row)
        return out

    def find_earnings_filings(
        self,
        cik: str,
        cfg: dict[str, Any],
        *,
        after: str | None = None,
        limit: int | None = None,
        accession: str | None = None,
    ) -> list[dict[str, Any]]:
        """按 ticker listing 选发现路径。ADR 不把 6-K 并进 8-K 列表。港股不查 EDGAR。"""
        spec = resolve_earnings_spec(cfg)
        if spec.listing == "hk":
            raise RuntimeError("listing=hk，不查询 SEC")
        if spec.listing == "adr":
            return self.scan_earnings_filings(
                cik,
                cfg,
                after=after,
                limit=limit,
                accession=accession,
            ).hits
        if after is None and limit is None and accession is None:
            rows = self.find_earnings_8k(cik)
        else:
            rows = self.find_earnings_8k(
                cik,
                after=after,
                forms=list(spec.earnings_forms),
                items=list(spec.earnings_items),
            )
        if accession:
            rows = [row for row in rows if row.get("accessionNumber") == accession]
        if limit is not None:
            rows = rows[:limit]
        return rows

    def scan_earnings_filings(
        self,
        cik: str,
        cfg: dict[str, Any],
        *,
        after: str | None = None,
        limit: int | None = None,
        accession: str | None = None,
        skip_accessions: set[str] | None = None,
        progress: Any = None,
    ) -> "EarningsScan":
        """ADR：6-K / 6-K/A，主文档噪声先丢，再用 exhibit 描述确认业绩新闻稿。"""
        spec = resolve_earnings_spec(cfg)
        if spec.listing != "adr" or spec.earnings_6k is None:
            raise ValueError(f"scan_earnings_filings 只用于 listing=adr，当前是 {spec.listing}")
        filters = spec.earnings_6k.model_dump()
        skipped_ids = skip_accessions or set()
        hits: list[dict[str, Any]] = []
        skips: list[dict[str, Any]] = []
        for row in self.iter_filing_rows(cik):
            if not form_matches(str(row.get("form") or ""), list(spec.earnings_forms), amendments=True):
                continue
            accepted = str(row.get("acceptanceDateTime") or "")
            acc = str(row.get("accessionNumber") or "")
            if accession:
                if acc != accession:
                    continue
            elif after and not is_newer_than(accepted, after):
                break
            if acc in skipped_ids:
                continue
            decision, reason, press, presentation = self._classify_6k_row(cik, row, filters)
            if progress is not None:
                progress(len(hits) + (1 if decision == "hit" and press is not None else 0), len(skips) + 1)
            if decision != "hit" or press is None:
                skips.append(
                    {
                        "form": row.get("form"),
                        "acceptanceDateTime": accepted,
                        "accessionNumber": acc,
                        "primaryDocument": row.get("primaryDocument") or "",
                        "reason": reason if press is not None or decision != "hit" else "no_press_file",
                    }
                )
                if accession:
                    break
                continue
            hits.append(
                {
                    **row,
                    "press_release_file": press.get("name") or "",
                    "presentation_file": (presentation or {}).get("name") or "",
                    "press_description": str(press.get("description") or "")[:240],
                    "match_reason": reason,
                }
            )
            if accession or (limit is not None and len(hits) >= limit):
                break
        return EarningsScan(hits=hits, skips=skips)

    def _classify_6k_row(
        self,
        cik: str,
        row: dict[str, Any],
        filters: dict[str, Any],
    ) -> tuple[str, str, dict[str, str] | None, dict[str, str] | None]:
        primary = str(row.get("primaryDocument") or "")
        decision, reason = classify_6k(primary, [], filters)
        if decision == "skip" and reason == "primary_doc":
            return decision, reason, None, None
        accession = str(row.get("accessionNumber") or "")
        try:
            index = self._cached_filing_index(cik, accession)
            cover = ""
            if primary.lower().endswith((".htm", ".html", ".txt")):
                raw = self._cached_filing_bytes(cik, accession, primary)
                cover = raw.decode("utf-8", "replace")
        except Exception as exc:
            return "skip", f"fetch_error:{exc.__class__.__name__}", None, None
        exhibits = merge_exhibits(exhibits_from_index(index), exhibit_descriptions_from_html(cover))
        decision, reason = classify_6k(primary, exhibits, filters)
        if decision != "hit":
            return decision, reason, None, None
        press, presentation = select_press_and_presentation(
            exhibits,
            filters,
            primary_document=primary,
        )
        if press is None:
            return "skip", "no_press_file", None, None
        return "hit", reason, press, presentation

    def _cached_filing_index(self, cik: str, accession: str) -> dict[str, Any]:
        path = cache_dir() / "sec" / "filings" / accession.replace("-", "") / "index.json"
        if path.exists() and path.stat().st_size > 0:
            return json.loads(path.read_text(encoding="utf-8"))
        data = self.filing_index(cik, accession)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return data

    def _cached_filing_bytes(self, cik: str, accession: str, filename: str) -> bytes:
        path = cache_dir() / "sec" / "filings" / accession.replace("-", "") / filename
        if path.exists() and path.stat().st_size > 0:
            return path.read_bytes()
        data = self.download_file(cik, accession, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data

    def find_press_release_files(self, cik: str, accession: str) -> list[str]:
        index = self.filing_index(cik, accession)
        items = index.get("directory", {}).get("item", [])
        matches: list[str] = []
        for item in items:
            name = item.get("name", "")
            desc = (item.get("description") or "").upper()
            name_u = name.upper()
            # 有的 8-K 把 Exhibit 99.1 写成 exhibit991.htm（没有 EX-99 / EX99）
            if (
                "EX-99" in name_u
                or "EX99" in name_u
                or "EXHIBIT99" in name_u
                or "EXHIBIT 99" in desc
                or "PRESS" in desc
            ):
                if name_u.endswith((".HTM", ".HTML", ".TXT")):
                    matches.append(name)
        # Prefer EX-99.1
        matches.sort(key=lambda n: (0 if "99.1" in n.upper() else 1, n))
        return matches

    def download_press_release(
        self,
        cik: str,
        accession: str,
        dest: Path,
        *,
        press_release_file: str | None = None,
        presentation_file: str | None = None,
        presentation_dest: Path | None = None,
    ) -> Path:
        """国内 8-K：拼接全部 EX-99 HTML。ADR：只写 Press release，演示文稿另存。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if press_release_file:
            dest.write_bytes(self._cached_filing_bytes(cik, accession, press_release_file))
            if presentation_file and presentation_dest is not None:
                presentation_dest.parent.mkdir(parents=True, exist_ok=True)
                presentation_dest.write_bytes(self._cached_filing_bytes(cik, accession, presentation_file))
            return dest
        files = self.find_press_release_files(cik, accession)
        if not files:
            raise FileNotFoundError(f"未找到 EX-99 新闻稿: {accession}")
        parts: list[bytes] = []
        for name in files:
            parts.append(self.download_file(cik, accession, name))
        dest.write_bytes(b"\n".join(parts))
        return dest


def _rows_from_columns(recent: dict[str, Any]) -> list[dict[str, Any]]:
    forms = recent.get("form") or []
    items = recent.get("items") or []
    dates = recent.get("acceptanceDateTime") or []
    accessions = recent.get("accessionNumber") or []
    primary = recent.get("primaryDocument") or []
    rows: list[dict[str, Any]] = []
    for index, form in enumerate(forms):
        rows.append(
            {
                "form": form,
                "items": items[index] if index < len(items) else "",
                "acceptanceDateTime": dates[index] if index < len(dates) else "",
                "accessionNumber": accessions[index] if index < len(accessions) else "",
                "primaryDocument": primary[index] if index < len(primary) else "",
            }
        )
    return rows


class EarningsScan:
    """hits 进入 Stage1；skips 只记 ignore，不进 failure。"""

    def __init__(self, hits: list[dict[str, Any]], skips: list[dict[str, Any]]):
        self.hits = hits
        self.skips = skips


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
