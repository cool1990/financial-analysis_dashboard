"""从 10-Q / 10-K XBRL 抽出分部收入。

companyfacts 不含分部维度。这里读申报文件里的实例文档：
业务分部轴和产品轴上的收入，同一 (结束日, 天数, 成员) 保留申报日较新的一条。
第四季如果没有单季事实，且同一份文件里有全年和前九个月，用两者相减。
没有这些事实的季度不补数。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from typing import Any

REVENUE_TAGS = {
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
}
ALLOW_AXES = {
    "StatementBusinessSegmentsAxis",
    "ProductOrServiceAxis",
    "StatementProductOrServiceAxis",
    "ProductsAndServicesAxis",
    "MarketAxis",
}

# 堆叠图用叶子，避免母项和应用家族下面的广告重复计算。
_FOA = frozenset({"FamilyOfAppsMember"})
_RL = frozenset({"RealityLabsMember"})
_ADS = frozenset({"AdvertisingMember"})
_ADS_FOA = frozenset({"AdvertisingMember", "FamilyOfAppsMember"})
_OTH_FOA = frozenset({"FamilyOfAppsMember", "ServiceOtherMember"})
_PAY = frozenset({"PaymentsandOtherFeesMember"})
_OTH = frozenset({"ServiceOtherMember"})


GEO_AXIS = "StatementGeographicalAxis"
GEO_AXES = (
    "StatementGeographicalAxis",
    "RevenueFromCustomersBasedInDifferentGeographicRegionsAxis",
)
# 两套地区不能混成一条线：2016–2017 的 Rest of World 是美国以外，之后的是四区里的剩余部分。
_GEO_FOUR = ("USCanadaMember", "EuropeMember", "AsiaPacificMember", "RestOfWorldMember")
_GEO_LABELS = {
    "USCanadaMember": ("us_ca", "美国和加拿大"),
    "UnitedStatesAndCanadaMember": ("us_ca", "美国和加拿大"),
    "EuropeMember": ("europe", "欧洲"),
    "AsiaPacificMember": ("apac", "亚太"),
    "AsiaMember": ("apac", "亚太"),
    "US": ("us", "美国"),
    "UnitedStatesMember": ("us", "美国"),
}
# 这些成员一出现，就按申报自己的国家/地区列，不再套 Meta 的四区或美国/美国以外。
_NVIDIA_GEO_TRIGGERS = {
    "TW",
    "TaiwanMember",
    "CN",
    "ChinaMember",
    "ChinaIncludingHongKongMember",
    "OtherAmericasMember",
    "OtherAsiaPacificMember",
    "OtherCountriesMember",
    "AllOtherCountriesNotSeparatelyDisclosedMember",
    "SG",
    "SingaporeMember",
    "IL",
    "IN",
}
_NVIDIA_GEO_ORDER = (
    ("US", "us", "美国"),
    ("UnitedStatesMember", "us", "美国"),
    ("TW", "tw", "台湾"),
    ("TaiwanMember", "tw", "台湾"),
    ("CN", "cn", "中国"),
    ("ChinaMember", "cn", "中国"),
    ("ChinaIncludingHongKongMember", "cn_hk", "中国（含香港）"),
    ("EuropeMember", "europe", "欧洲"),
    ("OtherAmericasMember", "other_americas", "其他美洲"),
    ("OtherAsiaPacificMember", "other_apac", "其他亚太"),
    ("SG", "sg", "新加坡"),
    ("SingaporeMember", "sg", "新加坡"),
    ("IL", "il", "以色列"),
    ("IN", "in", "印度"),
    ("OtherCountriesMember", "other_countries", "其他国家"),
    ("AllOtherCountriesNotSeparatelyDisclosedMember", "other_undisclosed", "未单列的其他国家"),
)
# NonUsMember 是美国以外的母项。已经拆出国家时不再并进柱。
_GEO_PARENTS = {"NonUsMember"}

# 产品/市场线。Compute、Networking、Hyperscale 是数据中心内部，不和数据中心叠在一起。
_MARKET_ORDER = (
    ("DataCenterMember", "datacenter", "数据中心"),
    ("DatacenterMember", "datacenter", "数据中心"),
    ("GamingMember", "gaming", "游戏"),
    ("EdgeComputingMember", "edge", "边缘计算"),
    ("ProfessionalVisualizationMember", "proviz", "专业可视化"),
    ("AutomotiveMember", "auto", "汽车"),
    ("OEMAndOtherMember", "oem", "OEM 及其他"),
    ("OEMIPMember", "oem", "OEM 与 IP"),
    ("OEMandIPMember", "oem", "OEM 与 IP"),
)


def _alias_geo_members(members: dict[str, float]) -> dict[str, float]:
    """10-K 把「未单列的其他国家」改称 Other countries。两套名字不会同时出现。"""
    out = dict(members)
    src = "AllOtherCountriesNotSeparatelyDisclosedMember"
    dst = "OtherCountriesMember"
    if src in out and dst not in out:
        out[dst] = out.pop(src)
    return out


def geo_lines(members: dict[str, float]) -> list[dict[str, Any]]:
    """同一季只留一套地区。

    Meta 四区齐全时用四区；否则只有美国和美国以外时用两区。
    申报自己列出台湾、中国等成员时，按那些成员画，不改成 Meta 的区。
    """
    members = _alias_geo_members(members)
    if all(name in members and members[name] > 0 for name in _GEO_FOUR):
        rows = []
        for name in _GEO_FOUR:
            key, label = _GEO_LABELS.get(name, ("row", "世界其他地区"))
            if name == "RestOfWorldMember":
                key, label = "row", "世界其他地区"
            rows.append({"key": key, "name": label, "value": members[name], "depth": 0, "stack": True})
        return rows
    triggered = any(name in members and members[name] > 0 for name in _NVIDIA_GEO_TRIGGERS)
    us = members.get("US", members.get("UnitedStatesMember"))
    broad = members.get("RestOfWorldMember")
    if not triggered and us and broad and us > 0 and broad > 0:
        return [
            {"key": "us", "name": "美国", "value": us, "depth": 0, "stack": True},
            {"key": "row_broad", "name": "美国以外", "value": broad, "depth": 0, "stack": True},
        ]
    if not triggered:
        return []
    rows = []
    seen: set[str] = set()
    for name, key, label in _NVIDIA_GEO_ORDER:
        if name in _GEO_PARENTS or name not in members or members[name] <= 0 or key in seen:
            continue
        seen.add(key)
        rows.append({"key": key, "name": label, "value": members[name], "depth": 0, "stack": True})
    if len(rows) < 2:
        return []
    return rows


def parse_axis_facts(xml_bytes: bytes, axis: str) -> list[dict[str, Any]]:
    """只保留单一轴上的收入事实。带了别的轴的上下文不算这条拆分。"""
    root = ET.fromstring(xml_bytes)
    contexts: dict[str, dict[str, Any]] = {}
    for el in root.iter():
        if _local(el.tag) != "context":
            continue
        cid = el.get("id")
        if not cid:
            continue
        start = end = None
        members: list[str] = []
        axes: list[str] = []
        for child in el.iter():
            name = _local(child.tag)
            if name == "startDate" and child.text:
                start = child.text[:10]
            elif name == "endDate" and child.text:
                end = child.text[:10]
            elif name == "explicitMember" and child.text:
                axes.append((child.get("dimension") or "").split(":")[-1])
                members.append(child.text.split(":")[-1])
        if not start or not end or not axes or not all(item == axis for item in axes):
            continue
        contexts[cid] = {"start": start, "end": end, "members": tuple(sorted(set(members)))}
    facts: list[dict[str, Any]] = []
    for el in root.iter():
        tag = _fact_tag(el)
        if tag not in REVENUE_TAGS:
            continue
        ctx = contexts.get(el.get("contextRef") or "")
        if ctx is None:
            continue
        value = _fact_number(el)
        if value is None:
            continue
        start_d = date.fromisoformat(ctx["start"])
        end_d = date.fromisoformat(ctx["end"])
        facts.append(
            {
                "tag": tag,
                "start": ctx["start"],
                "end": ctx["end"],
                "days": (end_d - start_d).days,
                "members": ctx["members"],
                "value": value,
            }
        )
    return facts


def _shift_month(year: int, month: int) -> tuple[int, int]:
    while month <= 0:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    return year, month


def _quarter_month_end(fiscal_year: int, fiscal_quarter: int, fiscal_year_end_month: int) -> date:
    """财季结束月的最后一天。与 pipeline.compute.periods.fiscal_quarter_end 同一套月份。"""
    year, month = _shift_month(fiscal_year, fiscal_year_end_month - (4 - fiscal_quarter) * 3)
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _quarter_anchor(fiscal_year: int, fiscal_quarter: int, fiscal_year_end_month: int) -> date:
    """财季锚点用结束月的 28 日，和财季推断保持一致。"""
    end = _quarter_month_end(fiscal_year, fiscal_quarter, fiscal_year_end_month)
    return date(end.year, end.month, 28)


def fiscal_quarter_key(end: str, fiscal_year_end_month: int) -> tuple[int, int] | None:
    """结束日所属财季。离该季月末超过 35 天的不算，避免把任意区间当成财季。

    35 天盖住 52/53 周财年：季末经常落在名义月末前大约四周（例如 11 月财年的 1 月 29 日、8 月 2 日）。
    相邻季月末大约相距 90 天，35 天的窗口不会叠到下一季。
    """
    try:
        day = date.fromisoformat(str(end)[:10])
    except ValueError:
        return None
    best: tuple[int, int] | None = None
    best_dist = 10**9
    for year in range(day.year - 1, day.year + 2):
        for quarter in range(1, 5):
            dist = abs((day - _quarter_anchor(year, quarter, fiscal_year_end_month)).days)
            if dist < best_dist:
                best_dist = dist
                best = (year, quarter)
    if best is None:
        return None
    month_end = _quarter_month_end(best[0], best[1], fiscal_year_end_month)
    if abs((day - month_end).days) > 35:
        return None
    return best


def _nine_and_year_ends(
    grouped: dict[tuple[str, str], Any],
    fiscal_year_end_month: int,
) -> tuple[dict[int, Any], list[tuple[int, str]]]:
    """前九个月按财年归集；全年只留财年结束月那一季，不写死 12 月。"""
    nine_by_fy: dict[int, Any] = {}
    year_ends: list[tuple[int, str]] = []
    for (end, bucket), payload in grouped.items():
        key = fiscal_quarter_key(end, fiscal_year_end_month)
        if key is None:
            continue
        fiscal_year, fiscal_quarter = key
        if bucket == "nine" and fiscal_quarter == 3:
            nine_by_fy[fiscal_year] = payload
        elif bucket == "year" and fiscal_quarter == 4:
            year_ends.append((fiscal_year, end))
    year_ends.sort(key=lambda item: item[1])
    return nine_by_fy, year_ends


def assemble_geography(
    filings: list[dict[str, Any]],
    fiscal_year_end_month: int = 12,
) -> list[dict[str, Any]]:
    """filings 的 facts 已是 parse_axis_facts 的结果。没有单季事实时，用全年减前九个月。"""
    history: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    choices: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for filing in filings:
        local: dict[tuple[str, str], dict[str, float]] = {}
        for (end, bucket, members), fact in _prefer(filing["facts"]).items():
            if len(members) != 1:
                continue
            slot_key = (end, bucket, members[0])
            history.setdefault(slot_key, []).append(
                {
                    "value": fact["value"],
                    "filed": filing["filed"],
                    "form": filing["form"],
                    "accession": filing["accession"],
                }
            )
            local.setdefault((end, bucket), {})[members[0]] = fact["value"]
        for slot, member_values in local.items():
            choices.setdefault(slot, []).append(
                {
                    "members": member_values,
                    "filed": filing["filed"],
                    "form": filing["form"],
                    "accession": filing["accession"],
                }
            )
    # 同一季的国家名单以一整份申报为准。后一份 10-Q 会重分类（例如新加坡并进其他国家），
    # 不能把旧名单里多出来的成员再加回去。
    grouped: dict[tuple[str, str], dict[str, float]] = {}
    meta: dict[tuple[str, str], dict[str, Any]] = {}
    for slot, options in choices.items():
        ordered = sorted(options, key=lambda item: item["filed"], reverse=True)
        picked = next((item for item in ordered if geo_lines(item["members"])), ordered[0])
        grouped[slot] = picked["members"]
        meta[slot] = picked
    direct: dict[str, dict[str, Any]] = {}
    for (end, bucket), member_values in grouped.items():
        if bucket != "quarter":
            continue
        lines = geo_lines(member_values)
        if not lines:
            continue
        info = meta[(end, bucket)]
        direct[end] = {
            "end": end,
            "lines": lines,
            "filed": info["filed"],
            "form": info["form"],
            "accession": info["accession"],
            "method": "xbrl",
        }
    nine_by_fy, year_ends = _nine_and_year_ends(grouped, fiscal_year_end_month)
    for fy, end in year_ends:
        if end in direct:
            continue
        nine = _alias_geo_members(nine_by_fy.get(fy) or {})
        # 后来的 10-K 会把 Rest of World 收窄。只拿仍大于前九个月的那条全年数，避免两套口径相减。
        # 「未单列的其他国家」和 Other countries 是同一行换了名字，相减前对齐。
        residual = {}
        members = {member for (mend, bucket, member) in history if mend == end and bucket == "year"}
        for member in members:
            aliased = _alias_geo_members({member: 0})
            nine_key = next(iter(aliased))
            nine_value = nine.get(nine_key)
            if nine_value is None:
                continue
            usable = [item for item in history.get((end, "year", member), []) if item["value"] > nine_value]
            if not usable:
                continue
            annual_value = max(usable, key=lambda item: item["filed"])["value"]
            gap = annual_value - nine_value
            if gap > 0 and nine_key not in residual:
                residual[nine_key] = gap
        lines = geo_lines(residual)
        if not lines:
            continue
        info = meta.get((end, "year")) or {}
        direct[end] = {
            "end": end,
            "lines": lines,
            "filed": info.get("filed") or "",
            "form": info.get("form") or "10-K",
            "accession": info.get("accession") or "",
            "method": "residual",
        }
    rows = list(direct.values())
    rows.sort(key=lambda row: row["end"])
    return rows


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _fact_tag(el: Any) -> str:
    if _local(el.tag) == "nonFraction":
        return (el.get("name") or "").split(":")[-1]
    return _local(el.tag)


def _fact_number(el: Any) -> float | None:
    """经典 XBRL 的 text，或 iXBRL nonFraction（可带千分位和 scale）。"""
    raw = "".join(el.itertext()).replace(",", "").replace("$", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    scale = el.get("scale")
    if scale not in (None, ""):
        try:
            value *= 10 ** int(scale)
        except ValueError:
            return None
    return value


def parse_instance(xml_bytes: bytes) -> list[dict[str, Any]]:
    """返回收入事实。members 是排序后的成员本地名。无分部的公司合计 members 为空。"""
    root = ET.fromstring(xml_bytes)
    contexts: dict[str, dict[str, Any]] = {}
    for el in root.iter():
        if _local(el.tag) != "context":
            continue
        cid = el.get("id")
        if not cid:
            continue
        start = end = None
        members: list[str] = []
        axes: list[str] = []
        for child in el.iter():
            name = _local(child.tag)
            if name == "startDate" and child.text:
                start = child.text[:10]
            elif name == "endDate" and child.text:
                end = child.text[:10]
            elif name == "explicitMember" and child.text:
                axes.append((child.get("dimension") or "").split(":")[-1])
                members.append(child.text.split(":")[-1])
        if not start or not end:
            continue
        if axes and not all(axis in ALLOW_AXES for axis in axes):
            continue
        contexts[cid] = {"start": start, "end": end, "members": tuple(sorted(set(members)))}
    facts: list[dict[str, Any]] = []
    for el in root.iter():
        tag = _fact_tag(el)
        if tag not in REVENUE_TAGS:
            continue
        ctx = contexts.get(el.get("contextRef") or "")
        if ctx is None:
            continue
        value = _fact_number(el)
        if value is None:
            continue
        start_d = date.fromisoformat(ctx["start"])
        end_d = date.fromisoformat(ctx["end"])
        facts.append(
            {
                "tag": tag,
                "start": ctx["start"],
                "end": ctx["end"],
                "days": (end_d - start_d).days,
                "members": ctx["members"],
                "value": value,
            }
        )
    return facts


def parse_nvda_hq_geo_quarter(text: str) -> list[dict[str, Any]] | None:
    """8-K 补充信息里「客户总部所在地」的三个月地区收入。单位按表头百万美元。"""
    marker = "Geographic Revenue based upon Customer Headquarters Location:"
    start = text.find(marker)
    if start < 0:
        return None
    block = text[start : start + 700]
    spec = (
        ("United States", "us", "美国"),
        ("Taiwan", "tw", "台湾"),
        ("China (including Hong Kong)", "cn_hk", "中国（含香港）"),
        ("Other", "other_countries", "其他国家"),
    )
    lines = []
    for label, key, name in spec:
        match = re.search(re.escape(label) + r"\s+\$?\s*([\d,]+)", block)
        if not match:
            return None
        value = float(match.group(1).replace(",", "")) * 1_000_000
        lines.append({"key": key, "name": name, "value": value, "depth": 0, "stack": True})
    total = re.search(r"Total revenue\s+\$?\s*([\d,]+)", block)
    if total is None:
        return None
    reported = float(total.group(1).replace(",", "")) * 1_000_000
    if abs(sum(line["value"] for line in lines) - reported) > 1:
        return None
    return lines


def parse_nvda_q3_markets(text: str) -> list[dict[str, Any]] | None:
    """新闻稿里的数据中心和边缘计算。单位是 billion，不把电话会约数写进来。"""
    datacenter = re.search(r"Data Center revenue of \$([0-9]+(?:\.[0-9]+)?) billion", text)
    edge = re.search(r"Edge Computing revenue was \$([0-9]+(?:\.[0-9]+)?) billion", text)
    if not datacenter or not edge:
        return None
    return [
        {
            "key": "datacenter",
            "name": "数据中心",
            "value": round(float(datacenter.group(1)) * 1_000_000_000),
            "depth": 0,
            "stack": True,
        },
        {
            "key": "edge",
            "name": "边缘计算",
            "value": round(float(edge.group(1)) * 1_000_000_000),
            "depth": 0,
            "stack": True,
        },
    ]


def parse_composition_table(html: str) -> dict[str, float] | None:
    """10-Q 收入构成表的本季列，单位百万美元。只认 Advertising / Payments and other fees。"""
    plain = re.sub(r"(?is)<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", html)
    plain = re.sub(r"(?i)<br\s*/?>", "\n", plain)
    plain = re.sub(r"<[^>]+>", "\n", plain)
    plain = plain.replace("&nbsp;", " ").replace("&#160;", " ").replace("&#32;", " ").replace("&amp;", "&")
    plain = re.sub(r"[ \t]+", " ", plain)
    match = re.search(
        r"in millions[\s\S]{0,240}?Advertising\s+\$?\s*([\d,]+)[\s\S]{0,500}?Payments and other fees\s+\$?\s*([\d,]+)",
        plain,
        flags=re.I,
    )
    if not match:
        return None
    ads = float(match.group(1).replace(",", "")) * 1_000_000
    other = float(match.group(2).replace(",", "")) * 1_000_000
    if ads <= 0 or other < 0:
        return None
    return {"AdvertisingMember": ads, "PaymentsandOtherFeesMember": other}


def _bucket(days: int) -> str | None:
    if 80 <= days <= 100:
        return "quarter"
    if 260 <= days <= 290:
        return "nine"
    if 350 <= days <= 380:
        return "year"
    return None


def _prefer(facts: list[dict[str, Any]]) -> dict[tuple, dict[str, Any]]:
    """同一份文件内，同一键留收入标签更具体的一条。"""
    rank = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": 3,
        "Revenues": 2,
        "SalesRevenueNet": 1,
    }
    out: dict[tuple, dict[str, Any]] = {}
    for fact in facts:
        bucket = _bucket(fact["days"])
        if bucket is None or not fact["members"]:
            continue
        key = (fact["end"], bucket, fact["members"])
        prev = out.get(key)
        if prev is None or rank.get(fact["tag"], 0) >= rank.get(prev["tag"], 0):
            out[key] = fact
    return out


def lines_for(members: dict[tuple[str, ...], float]) -> list[dict[str, Any]]:
    """把成员金额收成不重复的行。stack 为真的行进入占比柱。"""
    indexed = {frozenset(key): value for key, value in members.items()}
    if _FOA in indexed and _RL in indexed:
        rows = [
            {"key": "foa", "name": "应用家族", "value": indexed[_FOA], "depth": 0, "stack": False},
        ]
        if _ADS_FOA in indexed:
            rows.append({"key": "ads", "name": "广告", "value": indexed[_ADS_FOA], "depth": 1, "stack": True})
        if _OTH_FOA in indexed:
            rows.append({"key": "other", "name": "其他收入", "value": indexed[_OTH_FOA], "depth": 1, "stack": True})
        rows.append({"key": "rl", "name": "Reality Labs", "value": indexed[_RL], "depth": 0, "stack": True})
        stacked = [row for row in rows if row["stack"]]
        foa_children = [row["value"] for row in stacked if row["key"] != "rl"]
        if foa_children and abs(sum(foa_children) - indexed[_FOA]) / max(indexed[_FOA], 1) > 0.02:
            rows = [
                {"key": "foa", "name": "应用家族", "value": indexed[_FOA], "depth": 0, "stack": True},
                {"key": "rl", "name": "Reality Labs", "value": indexed[_RL], "depth": 0, "stack": True},
            ]
        return rows
    rows = []
    if _ADS in indexed:
        rows.append({"key": "ads", "name": "广告", "value": indexed[_ADS], "depth": 0, "stack": True})
    if _PAY in indexed:
        rows.append({"key": "other", "name": "支付及其他费用", "value": indexed[_PAY], "depth": 0, "stack": True})
    elif _OTH in indexed:
        rows.append({"key": "other", "name": "其他收入", "value": indexed[_OTH], "depth": 0, "stack": True})
    if rows:
        return rows
    singles = {key[0]: value for key, value in members.items() if len(key) == 1 and value > 0}
    seen: set[str] = set()
    market_rows = []
    for name, key, label in _MARKET_ORDER:
        if name not in singles or key in seen:
            continue
        seen.add(key)
        market_rows.append({"key": key, "name": label, "value": singles[name], "depth": 0, "stack": True})
    if len(market_rows) >= 2:
        return market_rows
    return []


def _index_filing(facts: list[dict[str, Any]]) -> dict[tuple, float]:
    picked = _prefer(facts)
    return {key: fact["value"] for key, fact in picked.items()}


def assemble_periods(
    filings: list[dict[str, Any]],
    fiscal_year_end_month: int = 12,
) -> dict[str, list[dict[str, Any]]]:
    """filings: {filed, form, accession, report, facts}，facts 来自 parse_instance。

    额外的构成表用 members 字典放在 composition 字段，report 是该季结束日。
    """
    direct: dict[tuple, dict[str, Any]] = {}
    residual: dict[tuple, dict[str, Any]] = {}
    annual: dict[str, dict[str, Any]] = {}

    for filing in filings:
        indexed = _index_filing(filing["facts"])
        by_end_bucket: dict[tuple[str, str], dict[tuple[str, ...], float]] = {}
        for (end, bucket, members), value in indexed.items():
            by_end_bucket.setdefault((end, bucket), {})[members] = value
        for (end, bucket), member_values in by_end_bucket.items():
            if bucket == "quarter":
                key = (end, "quarter")
                prev = direct.get(key)
                if prev is None or filing["filed"] >= prev["filed"]:
                    direct[key] = {
                        "end": end,
                        "lines": lines_for(member_values),
                        "filed": filing["filed"],
                        "form": filing["form"],
                        "accession": filing["accession"],
                        "method": "xbrl",
                    }
            if bucket == "year":
                # 全年行只收财年 Q4。结束月不是 fiscal_year_end_month 的跨年区间不写入。
                year_key = fiscal_quarter_key(end, fiscal_year_end_month)
                if year_key and year_key[1] == 4:
                    year = str(year_key[0])
                    prev = annual.get(year)
                    if prev is None or filing["filed"] >= prev["filed"]:
                        annual[year] = {
                            "end": end,
                            "lines": lines_for(member_values),
                            "filed": filing["filed"],
                            "form": filing["form"],
                            "accession": filing["accession"],
                            "method": "xbrl",
                        }
        # 同一份文件里的财年全年 − 前九个月。结束月来自 fiscal_year_end_month。
        nine_by_fy, year_ends = _nine_and_year_ends(by_end_bucket, fiscal_year_end_month)
        for fy, end in year_ends:
            annual_members = by_end_bucket.get((end, "year")) or {}
            nine = nine_by_fy.get(fy) or {}
            if not annual_members or not nine:
                continue
            shared = set(annual_members) & set(nine)
            q4_members = {}
            for member in shared:
                gap = annual_members[member] - nine[member]
                if gap > 0:
                    q4_members[member] = gap
            lines = lines_for(q4_members)
            if not lines:
                continue
            prev = residual.get(end)
            if prev is None or filing["filed"] >= prev["filed"]:
                residual[end] = {
                    "end": end,
                    "lines": lines,
                    "filed": filing["filed"],
                    "form": filing["form"],
                    "accession": filing["accession"],
                    "method": "residual",
                }

    for filing in filings:
        composition = filing.get("composition")
        report = filing.get("report")
        if not composition or not report:
            continue
        member_values = {tuple(sorted([name])): value for name, value in composition.items()}
        lines = lines_for(member_values)
        if not lines:
            continue
        key = (report, "quarter")
        if key in direct:
            continue
        direct[key] = {
            "end": report,
            "lines": lines,
            "filed": filing["filed"],
            "form": filing["form"],
            "accession": filing["accession"],
            "method": "table",
        }

    quarters = []
    for end, row in direct.items():
        if row["lines"]:
            quarters.append(row)
    for end, row in residual.items():
        if (end, "quarter") not in direct and row["lines"]:
            quarters.append(row)
    quarters.sort(key=lambda row: row["end"])
    years = [row for row in annual.values() if row["lines"]]
    years.sort(key=lambda row: row["end"])
    quarters = _fill_q4_from_three_quarters(quarters, years, fiscal_year_end_month)
    quarters = _drop_incomplete_residuals(quarters)
    return {"quarters": quarters, "years": years}


def _drop_incomplete_residuals(quarters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """相减后少了上一季有的分部时，不把剩余部分当成 100% 占比。"""
    kept: list[dict[str, Any]] = []
    previous: set[str] | None = None
    for row in quarters:
        stack = {line["key"] for line in row["lines"] if line["stack"]}
        if row.get("method") == "residual" and previous and not previous <= stack:
            continue
        kept.append(row)
        previous = stack
    return kept


def _fill_q4_from_three_quarters(
    quarters: list[dict[str, Any]],
    years: list[dict[str, Any]],
    fiscal_year_end_month: int = 12,
) -> list[dict[str, Any]]:
    """全年和前三季都有同一分部、但没有单季第四季时，用差额。差额不是正数就跳过。

    第四季是财年结束月，不写死 12 月。前三季按财季对齐；缺任何一季就留空。
    """
    by_end = {row["end"]: row for row in quarters}
    by_key: dict[tuple[int, int], dict[str, Any]] = {}
    for row in quarters:
        key = fiscal_quarter_key(row["end"], fiscal_year_end_month)
        if key:
            by_key[key] = row
    extra: list[dict[str, Any]] = []
    for year_row in years:
        end = year_row["end"]
        key = fiscal_quarter_key(end, fiscal_year_end_month)
        if not key or key[1] != 4 or end in by_end or key in by_key:
            continue
        fiscal_year = key[0]
        prior = [by_key.get((fiscal_year, quarter)) for quarter in (1, 2, 3)]
        if any(row is None for row in prior):
            continue
        year_lines = {row["key"]: row for row in year_row["lines"]}
        sums: dict[str, float] = {}
        names: dict[str, str] = {}
        depths: dict[str, int] = {}
        stacks: dict[str, bool] = {}
        for row in prior:
            for line in row["lines"]:
                sums[line["key"]] = sums.get(line["key"], 0.0) + line["value"]
                names[line["key"]] = line["name"]
                depths[line["key"]] = line["depth"]
                stacks[line["key"]] = line["stack"]
        q4_lines = []
        for line_key, annual in year_lines.items():
            if line_key not in sums:
                continue
            gap = annual["value"] - sums[line_key]
            if gap <= 0:
                continue
            q4_lines.append(
                {
                    "key": line_key,
                    "name": names.get(line_key, annual["name"]),
                    "value": gap,
                    "depth": depths.get(line_key, annual["depth"]),
                    "stack": stacks.get(line_key, annual["stack"]),
                }
            )
        if not q4_lines:
            continue
        extra.append(
            {
                "end": end,
                "lines": q4_lines,
                "filed": year_row["filed"],
                "form": year_row["form"],
                "accession": year_row["accession"],
                "method": "residual",
            }
        )
    merged = quarters + extra
    merged.sort(key=lambda row: row["end"])
    return merged
