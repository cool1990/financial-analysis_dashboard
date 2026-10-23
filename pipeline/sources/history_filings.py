"""从 SEC 拉 10-Q / 10-K，抽出分部实例和收入构成表。

原先这段抓取只在一次性脚本里。这里走 SecEdgarClient：User-Agent 用 SEC_USER_AGENT，
请求间隔按客户端限速（默认不超过每秒 5 次）。实例文档按 accession 缓存在 cache/sec。
"""

from __future__ import annotations

import sys
import time
from typing import Any

from pipeline.config import cache_dir
from pipeline.sources.sec_edgar import SecEdgarClient, SecRateLimitError
from pipeline.sources.segment_history import (
    GEO_AXIS,
    _bucket,
    parse_axis_facts,
    parse_composition_table,
    parse_instance,
)

PERIODIC_FORMS = {"10-Q", "10-K", "10-Q/A", "10-K/A"}
_LINKBASE_SUFFIXES = ("_cal.xml", "_def.xml", "_lab.xml", "_pre.xml", "_xsd.xml")


def base_form(form: str) -> str:
    return form.replace("/A", "")


def instance_name(names: list[str]) -> str | None:
    """申报目录里的 XBRL 实例。优先 inline 的 *_htm.xml，否则去掉 linkbase 后的那份 xml。"""
    lowered = [(name, name.lower()) for name in names if name]
    inline = [name for name, low in lowered if low.endswith("_htm.xml")]
    if inline:
        return sorted(inline)[0]
    xmls = []
    for name, low in lowered:
        if not low.endswith(".xml"):
            continue
        if low.endswith(_LINKBASE_SUFFIXES) or "filingsummary" in low:
            continue
        xmls.append(name)
    if not xmls:
        return None
    return sorted(xmls, key=lambda name: (len(name), name))[0]


def _rows_from_block(block: dict[str, Any]) -> list[dict[str, Any]]:
    forms = block.get("form") or []
    accessions = block.get("accessionNumber") or []
    reports = block.get("reportDate") or []
    filed = block.get("filingDate") or []
    accepted = block.get("acceptanceDateTime") or []
    primary = block.get("primaryDocument") or []
    rows: list[dict[str, Any]] = []
    for index, form in enumerate(forms):
        if form not in PERIODIC_FORMS:
            continue
        if index >= len(accessions):
            continue
        rows.append(
            {
                "form": str(form),
                "accession": accessions[index],
                "report": str(reports[index] or "")[:10] if index < len(reports) else "",
                "filing_date": str(filed[index] or "")[:10] if index < len(filed) else "",
                "accepted": str(accepted[index] or "") if index < len(accepted) else "",
                "primary": str(primary[index] or "") if index < len(primary) else "",
            }
        )
    return rows


def _submission_blocks(client: SecEdgarClient, cik: str) -> list[dict[str, Any]]:
    data = client.submissions(cik)
    filings = data.get("filings") or {}
    blocks = [filings.get("recent") or {}]
    for item in filings.get("files") or []:
        name = item.get("name") if isinstance(item, dict) else None
        if not name:
            continue
        extra = client.get(f"https://data.sec.gov/submissions/{name}")
        if not isinstance(extra, dict):
            continue
        if "form" in extra:
            blocks.append(extra)
        else:
            blocks.append((extra.get("filings") or {}).get("recent") or {})
    return blocks


def list_periodic_filings(client: SecEdgarClient, cik: str, *, start: str) -> list[dict[str, Any]]:
    """reportDate 或 filingDate 不早于 start 的 10-Q / 10-K（含修订）。同一 accession 只留一条。"""
    found: dict[str, dict[str, Any]] = {}
    for block in _submission_blocks(client, cik):
        for row in _rows_from_block(block):
            stamp = row["report"] or row["filing_date"]
            if stamp and stamp < start:
                continue
            prev = found.get(row["accession"])
            if prev is None or (row["filing_date"], row["accepted"]) >= (prev["filing_date"], prev["accepted"]):
                found[row["accession"]] = row
    rows = list(found.values())
    rows.sort(key=lambda row: (row["filing_date"], row["accepted"], row["accession"]))
    return rows


def latest_accession(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    chosen = max(rows, key=lambda row: (row["filing_date"], row["accepted"], row["accession"]))
    return str(chosen["accession"])


def _cache_path(accession: str, filename: str):
    safe = filename.replace("/", "_")
    return cache_dir() / "sec" / "filings" / accession.replace("-", "") / safe


def _download(client: SecEdgarClient, cik: str, accession: str, filename: str) -> bytes:
    path = _cache_path(accession, filename)
    if path.exists() and path.stat().st_size > 0:
        return path.read_bytes()
    last: Exception | None = None
    data = b""
    for attempt in range(4):
        try:
            data = client.download_file(cik, accession, filename)
            last = None
            break
        except SecRateLimitError as exc:
            last = exc
            time.sleep(min(2 ** attempt, 8))
    if last is not None:
        raise last
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def _index_names(client: SecEdgarClient, cik: str, accession: str) -> list[str]:
    path = _cache_path(accession, "index.json")
    if path.exists() and path.stat().st_size > 0:
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        payload = client.filing_index(cik, accession)
        path.parent.mkdir(parents=True, exist_ok=True)
        import json

        path.write_text(json.dumps(payload), encoding="utf-8")
    items = (payload.get("directory") or {}).get("item") or []
    return [str(item.get("name") or "") for item in items if isinstance(item, dict)]


def _quarter_ends_with_members(facts: list[dict[str, Any]]) -> set[str]:
    ends: set[str] = set()
    for fact in facts:
        if not fact.get("members"):
            continue
        if _bucket(int(fact.get("days") or 0)) == "quarter":
            ends.add(str(fact.get("end") or ""))
    ends.discard("")
    return ends


def collect_filings(
    client: SecEdgarClient,
    cik: str,
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """返回 (分部 filings, 地区 filings, warnings)。

    分部 filings 的 facts 来自 parse_instance。某季没有成员事实时，再抓 10-Q 正文里的收入构成表。
    地区 filings 的 facts 来自地区轴。
    """
    parsed: list[dict[str, Any]] = []
    warnings: list[str] = []
    for row in rows:
        print(f"[history] {base_form(row['form'])} {row.get('report') or row['filing_date']} {row['accession']}", file=sys.stderr)
        names = _index_names(client, cik, row["accession"])
        filename = instance_name(names)
        segment_facts: list[dict[str, Any]] = []
        geo_facts: list[dict[str, Any]] = []
        if filename:
            raw = _download(client, cik, row["accession"], filename)
            try:
                segment_facts = parse_instance(raw)
                geo_facts = parse_axis_facts(raw, GEO_AXIS)
            except Exception as exc:
                warnings.append(f"{row['accession']} 的 XBRL 没有解析: {exc}")
                segment_facts = []
                geo_facts = []
        elif row["form"].startswith("10-Q") or row["form"].startswith("10-K"):
            warnings.append(f"{row['accession']} 没有 XBRL 实例")
        parsed.append({**row, "segment_facts": segment_facts, "geo_facts": geo_facts, "composition": None})

    covered: set[str] = set()
    for item in parsed:
        covered.update(_quarter_ends_with_members(item["segment_facts"]))
    for item in parsed:
        if not str(item["form"]).startswith("10-Q"):
            continue
        report = item.get("report") or ""
        if not report or report in covered or not item.get("primary"):
            continue
        raw = _download(client, cik, item["accession"], item["primary"])
        composition = parse_composition_table(raw.decode("utf-8", "replace"))
        if composition:
            item["composition"] = composition

    def _pack(facts_key: str, *, with_composition: bool) -> list[dict[str, Any]]:
        packed = []
        for item in parsed:
            entry = {
                "filed": item["filing_date"],
                "form": base_form(item["form"]),
                "accession": item["accession"],
                "report": item.get("report") or "",
                "facts": item[facts_key],
            }
            if with_composition and item.get("composition"):
                entry["composition"] = item["composition"]
            packed.append(entry)
        return packed

    return _pack("segment_facts", with_composition=True), _pack("geo_facts", with_composition=False), warnings
