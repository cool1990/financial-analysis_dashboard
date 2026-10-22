"""从 SEC companyfacts 抽出单季 GAAP 序列。

收入、营业利润、净利润优先用 80–100 天的单季事实。
现金流量表通常只有「财年起点至今」，单季用同一起点的相邻累计值相减。
第四季如果没有单季事实，流量科目用全年减去前一段累计（一般是前九个月）。
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
    """没有单季事实时，用同一起点的两段累计相减。"""
    longs = [s for s in spans if s.end == end and s.days > 100]
    if not longs:
        return None
    chosen = max(longs, key=lambda s: s.filed)
    priors = [s for s in spans if s.start == chosen.start and s.end < chosen.end and s.days >= 80]
    if not priors:
        return None
    prior_end = max(s.end for s in priors)
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
