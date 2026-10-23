"""从 SEC companyfacts 抽出单季 GAAP 序列。

收入、营业利润、净利润优先用 80–100 天的单季事实。
现金流量表通常只有「财年起点至今」，单季用同一起点的相邻累计值相减。
第四季如果没有单季事实，流量科目用全年减去前一段累计（一般是前九个月）。
两段结束日必须相距 80–100 天，否则不产出，避免只用半年累计倒挤成单季。
稀释每股收益不做这种相减：加权股本不是可加的，倒挤会偏出一美分以上。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class Span:
    tag: str
    start: date
    end: date
    val: float
    filed: str

    @property
    def days(self) -> int:
        return (self.end - self.start).days


def _as_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _spans(facts: dict[str, Any], tags: list[str], *unit_keys: str) -> list[Span]:
    """同一 (start, end) 只留 filed 最新的一条。多个 tag 也按 filed 合并。"""
    gaap = (facts.get("facts") or {}).get("us-gaap") or {}
    best: dict[tuple[date, date], Span] = {}
    for tag in tags:
        node = gaap.get(tag) or {}
        units = node.get("units") or {}
        series: list[dict[str, Any]] = []
        for key in unit_keys:
            series = units.get(key) or []
            if series:
                break
        if not series and units:
            series = next(iter(units.values()), [])
        for row in series:
            start = _as_date(row.get("start"))
            end = _as_date(row.get("end"))
            if start is None or end is None or row.get("val") is None:
                continue
            try:
                val = float(row["val"])
            except (TypeError, ValueError):
                continue
            span = Span(tag=tag, start=start, end=end, val=val, filed=str(row.get("filed") or ""))
            prev = best.get((start, end))
            if prev is None or span.filed >= prev.filed:
                best[(start, end)] = span
    return list(best.values())


def _direct_quarter(spans: list[Span], end: date) -> Span | None:
    cands = [s for s in spans if s.end == end and 80 <= s.days <= 100]
    if not cands:
        return None
    return max(cands, key=lambda s: s.filed)


def _residual(spans: list[Span], end: date) -> tuple[float, str] | None:
    """没有单季事实时，用同一起点的两段累计相减。

    相减区间必须像一个季度：80 <= (chosen.end - prior.end).days <= 100。
    最近的前一段若是半年而不是前九个月，跨度大约 180 天，这里直接不产出。
    """
    longs = [s for s in spans if s.end == end and s.days > 100]
    if not longs:
        return None
    chosen = max(longs, key=lambda s: s.filed)
    priors = [s for s in spans if s.start == chosen.start and s.end < chosen.end and s.days >= 80]
    if not priors:
        return None
    prior_end = max(s.end for s in priors)
    if not 80 <= (chosen.end - prior_end).days <= 100:
        return None
    prior_cands = [s for s in priors if s.end == prior_end]
    prior = max(prior_cands, key=lambda s: s.filed)
    note = f"xbrl:{chosen.tag} ({prior.end.isoformat()}→{chosen.end.isoformat()} 累计相减)"
    return chosen.val - prior.val, note


def quarterly_values(facts: dict[str, Any], tags: list[str], *unit_keys: str, allow_residual: bool = True) -> dict[date, dict[str, Any]]:
    """返回 {period_end: {value, source_quote, tag}}。"""
    spans = _spans(facts, tags, *unit_keys)
    ends = {s.end for s in spans}
    out: dict[date, dict[str, Any]] = {}
    for end in sorted(ends):
        direct = _direct_quarter(spans, end)
        if direct is not None:
            out[end] = {
                "value": direct.val,
                "source_quote": f"xbrl:{direct.tag}",
                "tag": direct.tag,
                "derived": False,
            }
            continue
        if not allow_residual:
            continue
        got = _residual(spans, end)
        if got is None:
            continue
        val, note = got
        out[end] = {"value": val, "source_quote": note, "tag": tags[0], "derived": True}
    return out


REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
]
COST_OF_REVENUE_TAGS = ["CostOfRevenue", "CostOfGoodsAndServicesSold"]
OI_TAGS = ["OperatingIncomeLoss"]
NI_TAGS = ["NetIncomeLoss"]
EPS_TAGS = ["EarningsPerShareDiluted"]
OCF_TAGS = ["NetCashProvidedByUsedInOperatingActivities"]
CAPEX_TAGS = ["PaymentsToAcquirePropertyPlantAndEquipment"]


def quarterly_statements(facts: dict[str, Any]) -> dict[date, dict[str, Any]]:
    """合并各科目。键是季度结束日。"""
    revenue = quarterly_values(facts, REVENUE_TAGS, "USD")
    # 银行常年没有单季 Revenues。BAC 的 Revenues = 净利息收入 + 非利息收入，分毫不差。
    # 只有该季没有收入事实时才相加，避免和已有 Revenues 重复计算。
    nii = quarterly_values(facts, ["InterestIncomeExpenseNet"], "USD")
    noninterest = quarterly_values(facts, ["NoninterestIncome"], "USD")
    for end in set(nii) & set(noninterest):
        if end in revenue:
            continue
        left, right = nii[end], noninterest[end]
        if left.get("value") is None or right.get("value") is None:
            continue
        revenue[end] = {
            "value": left["value"] + right["value"],
            "source_quote": f"{left['source_quote']} + {right['source_quote']}",
            "tag": "InterestIncomeExpenseNet+NoninterestIncome",
            "derived": True,
        }
    cost = quarterly_values(facts, COST_OF_REVENUE_TAGS, "USD")
    oi = quarterly_values(facts, OI_TAGS, "USD")
    ni = quarterly_values(facts, NI_TAGS, "USD")
    eps = quarterly_values(facts, EPS_TAGS, "USD/shares", allow_residual=False)
    ocf = quarterly_values(facts, OCF_TAGS, "USD")
    capex = quarterly_values(facts, CAPEX_TAGS, "USD")
    ends = set(revenue) | set(cost) | set(oi) | set(ni) | set(eps) | set(ocf) | set(capex)
    out: dict[date, dict[str, Any]] = {}
    for end in sorted(ends):
        rev = revenue.get(end)
        cogs = cost.get(end)
        oper = oi.get(end)
        gross = None
        if rev and cogs and rev["value"]:
            gross = {
                "value": (rev["value"] - cogs["value"]) / rev["value"],
                "source_quote": f"({rev['source_quote']} − {cogs['source_quote']}) / {rev['source_quote']}",
                "derived": True,
            }
        margin = None
        margin_quote = None
        if rev and oper and rev["value"]:
            margin = oper["value"] / rev["value"]
            margin_quote = f"{oper['source_quote']} / {rev['source_quote']}"
        cap = capex.get(end)
        cash = ocf.get(end)
        cap_val = abs(cap["value"]) if cap else None
        fcf = None
        fcf_quote = None
        if cash is not None and cap_val is not None:
            fcf = cash["value"] - cap_val
            fcf_quote = f"{cash['source_quote']} − abs({cap['source_quote']})"
        out[end] = {
            "revenue": rev,
            "cost_of_revenue": cogs,
            "gross_margin": gross,
            "operating_income": oper,
            "operating_margin": None
            if margin is None
            else {"value": margin, "source_quote": margin_quote, "derived": True},
            "net_income": ni.get(end),
            "eps": eps.get(end),
            "ocf": cash,
            "capex": None
            if cap is None
            else {"value": cap_val, "source_quote": cap["source_quote"], "derived": cap["derived"], "tag": cap["tag"]},
            "fcf": None if fcf is None else {"value": fcf, "source_quote": fcf_quote, "derived": True},
        }
    return out


_INSTANCE_UNITS = {
    "usd": "USD",
    "usdpershare": "USD/shares",
    "pershare": "USD/shares",
    "shares": "shares",
}
_DURATION_TAGS = set(
    REVENUE_TAGS
    + COST_OF_REVENUE_TAGS
    + OI_TAGS
    + NI_TAGS
    + EPS_TAGS
    + OCF_TAGS
    + CAPEX_TAGS
    + ["WeightedAverageNumberOfDilutedSharesOutstanding"]
)
_INSTANT_TAGS = {"AccountsReceivableNetCurrent"}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_instance_gaap(xml_bytes: bytes) -> list[dict[str, Any]]:
    """10-Q/10-K 实例里没有维度的标准 us-gaap 数字。companyfacts 滞后时用它补季。"""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    contexts: dict[str, dict[str, Any]] = {}
    for el in root.iter():
        if _local_name(el.tag) != "context":
            continue
        cid = el.get("id")
        if not cid:
            continue
        start = end = instant = None
        members = False
        for child in el.iter():
            name = _local_name(child.tag)
            if name == "explicitMember":
                members = True
            elif name == "startDate" and child.text:
                start = child.text[:10]
            elif name == "endDate" and child.text:
                end = child.text[:10]
            elif name == "instant" and child.text:
                instant = child.text[:10]
        contexts[cid] = {"start": start, "end": end, "instant": instant, "members": members}
    found: list[dict[str, Any]] = []
    for el in root.iter():
        tag = _local_name(el.tag)
        if tag not in _DURATION_TAGS and tag not in _INSTANT_TAGS:
            continue
        ctx = contexts.get(el.get("contextRef") or "")
        if ctx is None or ctx["members"]:
            continue
        unit = _INSTANCE_UNITS.get(str(el.get("unitRef") or "").lower())
        if unit is None or el.text is None:
            continue
        try:
            val = float(el.text.replace(",", ""))
        except ValueError:
            continue
        if tag in _INSTANT_TAGS:
            if not ctx["instant"]:
                continue
            found.append({"tag": tag, "start": "", "end": ctx["instant"], "val": val, "unit": unit})
            continue
        if not ctx["start"] or not ctx["end"]:
            continue
        found.append({"tag": tag, "start": ctx["start"], "end": ctx["end"], "val": val, "unit": unit})
    return found


def merge_instance_gaap(
    facts: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    form: str,
    filed: str,
    accession: str,
) -> set[date]:
    """把实例里的新事实补进 companyfacts 结构。已有同一起止和数值的不重复。"""
    gaap = facts.setdefault("facts", {}).setdefault("us-gaap", {})
    added: set[date] = set()
    for row in rows:
        tag = str(row["tag"])
        unit = str(row["unit"])
        end = _as_date(str(row.get("end") or ""))
        if end is None:
            continue
        node = gaap.setdefault(tag, {"units": {}})
        bucket = node.setdefault("units", {}).setdefault(unit, [])
        start = str(row.get("start") or "")[:10]
        val = row["val"]
        if any(
            str(item.get("start") or "")[:10] == start
            and str(item.get("end") or "")[:10] == end.isoformat()
            and item.get("val") == val
            for item in bucket
        ):
            continue
        bucket.append(
            {
                "start": start,
                "end": end.isoformat(),
                "val": val,
                "form": form,
                "filed": filed,
                "accn": accession,
            }
        )
        added.add(end)
    return added
