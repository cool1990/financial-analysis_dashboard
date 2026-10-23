"""ADR 新闻稿常同时给 NT$ 与 US$。reporting_currency=USD 时不能把台币当成美元。

稀释 EPS 若写成 NT$x.xx (US$y.yy per ADR)，采用官方 ADR 美元，不用汇率把普通股 EPS 换算。
利润表若只有 NT$、没有该科目的美元对照，也不写入 USD 字段，不用汇率硬换。
晶圆收入的制程占比不是金额。
"""

from __future__ import annotations

import re
from typing import Any

from pipeline.extract.numbers import ParsedAmount, parse_number


_FOREIGN = re.compile(r"NT\$|\bNTD\b|\bTWD\b|新台币", re.IGNORECASE)
_USD = re.compile(r"US\$|\bUSD\b|U\.S\.\s*dollars|in US dollars", re.IGNORECASE)
# 台积电等 ADR 新闻稿首段：diluted earnings per share of NT$19.50 (US$3.14 per ADR unit)
_DILUTED_ADR_EPS = re.compile(
    r"diluted earnings per share of NT\$\s*[\d,]+(?:\.\d+)?"
    r"\s*\(\s*(?P<phrase>US\$\s*(?P<usd>[\d,]+\.\d+)\s+per\s+ADR(?:\s+unit)?)\s*\)"
    r"(?:\s+for\s+the\s+[^.]{0,90})?",
    re.IGNORECASE,
)
_ANY_ADR_EPS = re.compile(
    r"(?P<phrase>US\$\s*(?P<usd>[\d,]+\.\d+)\s+per\s+ADR(?:\s+unit)?)",
    re.IGNORECASE,
)
_NT_EPS = re.compile(r"NT\$\s*([\d,]+(?:\.\d+)?)")
_PER_ADR = re.compile(r"per\s+ADR", re.IGNORECASE)

_AMOUNT_KEYS = (
    "revenue",
    "net_income_gaap",
    "diluted_shares",
    "cost_of_revenue",
    "gross_profit_gaap",
    "gross_profit_nongaap",
    "rd_expense",
    "sga_expense",
    "operating_income_gaap",
    "operating_income_nongaap",
    "eps_gaap_diluted",
    "eps_nongaap_diluted",
    "operating_cash_flow",
    "capex_gross",
    "capex_net",
)


def currency_prompt_note(reporting_currency: str | None) -> str:
    currency = (reporting_currency or "USD").upper()
    if currency == "USD":
        return (
            "\n\nreporting_currency=USD。"
            "若新闻稿同时出现 NT$ 与 US$ / in US dollars，raw 只填美元金额，不要填 NT$。\n"
            "稀释 EPS 若写成 NT$x.xx (US$y.yy per ADR)，eps_gaap_diluted.raw 填 y.yy，"
            "source_quote 必须包含 US$y.yy per ADR。不要用汇率把 NT$ 换算成美元。\n"
            "利润表若单位是 NT$ million，该行不是美元。"
            "某个科目只有 NT$、没有 In US dollars / US$ 对照时，raw 填 null，"
            "不要把百万或十亿新台币写进美元字段，也不要用文中的汇率假设换算。\n"
            "x-nanometer accounted for n% of wafer revenue 是占比，不是金额，不要写入美元 KPI。\n"
        )
    return f"\n\nreporting_currency={currency}。raw 只填该币种金额。\n"


def _clip_quote(quote: str, phrase: str, limit: int = 200) -> str:
    text = re.sub(r"\s+", " ", quote).strip()
    needle = re.sub(r"\s+", " ", phrase).strip()
    if len(text) <= limit and needle.lower() in text.lower():
        return text
    idx = text.lower().find(needle.lower())
    if idx < 0 or len(needle) >= limit:
        return needle[:limit]
    spare = limit - len(needle)
    left = min(idx, spare // 2)
    start = idx - left
    end = min(len(text), start + limit)
    start = max(0, end - limit)
    return text[start:end].strip()


def find_adr_diluted_eps(full_text: str) -> tuple[str, str] | None:
    """官方 ADR 美元稀释 EPS。返回 (raw 数字, 含 US$x.xx per ADR 的引文)。

    优先「diluted earnings per share of NT$… (US$… per ADR)」；没有这句时才用文中第一处 US$ per ADR。
    不根据汇率或 ADR 股数换算。
    """
    if not full_text:
        return None
    match = _DILUTED_ADR_EPS.search(full_text)
    if match is not None:
        usd = match.group("usd").replace(",", "")
        return usd, _clip_quote(match.group(0), match.group("phrase"))
    match = _ANY_ADR_EPS.search(full_text)
    if match is None:
        return None
    usd = match.group("usd").replace(",", "")
    start = max(0, match.start() - 90)
    end = min(len(full_text), match.end() + 60)
    return usd, _clip_quote(full_text[start:end], match.group("phrase"))


def _same_amount(raw: Any, usd: str) -> bool:
    left = parse_number(None if raw is None else str(raw))
    right = parse_number(usd)
    return left is not None and right is not None and abs(left - right) < 1e-6


def _quote_has_adr_usd(quote: str, usd: str) -> bool:
    return bool(
        re.search(rf"US\$\s*{re.escape(usd)}\s+per\s+ADR", quote or "", re.IGNORECASE)
    )


def ordinary_share_eps(node: dict[str, Any] | None) -> float | None:
    """引文同时有 NT$ 普通股 EPS 与 US$ per ADR 时，返回 NT$ 数字。

    净利润和股数通常是台币口径，不能拿去和 ADR 美元 EPS 比。
    """
    if not isinstance(node, dict):
        return None
    quote = str(node.get("source_quote") or "")
    if not _PER_ADR.search(quote):
        return None
    match = _NT_EPS.search(quote)
    if not match:
        return None
    return parse_number(match.group(1))


def _prefer_adr_gaap_eps(extracted: dict[str, Any], full_text: str, warnings: list[str]) -> bool:
    found = find_adr_diluted_eps(full_text)
    if found is None:
        return False
    usd, quote = found
    node = extracted.get("eps_gaap_diluted")
    if not isinstance(node, dict):
        node = {}
        extracted["eps_gaap_diluted"] = node
    already = _same_amount(node.get("raw"), usd) and _quote_has_adr_usd(str(node.get("source_quote") or ""), usd)
    node["raw"] = usd
    node["unit"] = None
    if not _quote_has_adr_usd(str(node.get("source_quote") or ""), usd):
        node["source_quote"] = quote
    if not already:
        warnings.append(
            f"eps_gaap_diluted 采用新闻稿 US${usd} per ADR，优先于 NT$ 普通股 EPS，不做汇率换算"
        )
    return True


# 这些科目可以有新闻稿里的美元句子。股数和 EPS 不走这条。
_USD_LINE_LABELS: dict[str, str] = {
    "revenue": r"revenue",
    "net_income_gaap": r"net income",
    "cost_of_revenue": r"cost of (?:revenue|sales)",
    "gross_profit_gaap": r"gross profit",
    "gross_profit_nongaap": r"non[-\s]?gaap gross profit",
    "operating_income_gaap": r"income from operations|operating income",
    "operating_income_nongaap": r"non[-\s]?gaap operating income",
    "rd_expense": r"research and development",
    "sga_expense": r"selling, general|sg&a",
    "operating_cash_flow": r"cash (?:flows? )?from operating|operating cash flow",
    "capex_gross": r"capital expenditure",
    "capex_net": r"capital expenditure",
}
_NT_TABLE = re.compile(r"\(\s*Unit\s*:\s*NT\$\s*millions?", re.IGNORECASE)
_NT_NARRATIVE = re.compile(
    r"(?P<label>net income|gross profit|income from operations|operating income|revenue)"
    r"\s+of\s+NT\$\s*(?P<num>[\d,]+(?:\.\d+)?)\s*(?P<unit>billions?|millions?)",
    re.IGNORECASE,
)
_TABLE_LABELS: dict[str, re.Pattern[str]] = {
    "revenue": re.compile(r"\b(?:net sales|revenue)\b", re.IGNORECASE),
    "net_income_gaap": re.compile(r"\bnet income\b", re.IGNORECASE),
    "gross_profit_gaap": re.compile(r"\bgross profit\b", re.IGNORECASE),
    "operating_income_gaap": re.compile(r"\b(?:income from operations|operating income)\b", re.IGNORECASE),
    "cost_of_revenue": re.compile(r"\bcost of (?:revenue|sales)\b", re.IGNORECASE),
}
_NARRATIVE_KEYS = {
    "revenue": "revenue",
    "net income": "net_income_gaap",
    "gross profit": "gross_profit_gaap",
    "income from operations": "operating_income_gaap",
    "operating income": "operating_income_gaap",
}
_STATED_MARGINS = re.compile(
    r"Gross margin for the quarter was\s*(?P<gross>\d+(?:\.\d+)?)%\s*,\s*"
    r"operating margin was\s*(?P<operating>\d+(?:\.\d+)?)%\s*,\s*"
    r"and net profit margin was\s*(?P<net>\d+(?:\.\d+)?)%",
    re.IGNORECASE,
)
_NI_YOY = re.compile(
    r"net income and diluted EPS (?:both )?(?P<dir>increased|decreased)\s+(?P<pct>\d+(?:\.\d+)?)%",
    re.IGNORECASE,
)
_NI_QOQ = re.compile(
    r"(?:an|a)\s+(?P<pct>\d+(?:\.\d+)?)%\s+(?P<dir>increase|decrease)\s+in net income",
    re.IGNORECASE,
)
_NODE_SHARE = re.compile(
    r"(?P<node>\d+)\s*-\s*nanometer accounted for\s*(?P<pct>\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
_ADVANCED_SHARE = re.compile(
    r"Advanced technologies, defined as 7-nanometer and more advanced technologies, "
    r"accounted for\s*(?P<pct>\d+(?:\.\d+)?)\s*% of total wafer revenue",
    re.IGNORECASE,
)
_SKIP_USD_SENTENCE = re.compile(r"expected|outlook|assumption|\bbetween\b", re.IGNORECASE)
_PLAIN_DOLLAR = re.compile(r"(?<!NT)\$\s*\d", re.IGNORECASE)


def _nt_only(text: str) -> bool:
    return bool(_FOREIGN.search(text or "")) and not _USD.search(text or "")


def _quote_mentions_usd(text: str) -> bool:
    return bool(_USD.search(text or "") or _PLAIN_DOLLAR.search(text or ""))


def _close(left: float | None, right: float | None, tol: float = 0.002) -> bool:
    if left is None or right is None or right == 0:
        return False
    return abs(left - right) / abs(right) <= tol


def _unit_word(unit: str) -> str:
    key = unit.lower().rstrip("s")
    return {"billion": "billions", "million": "millions", "thousand": "thousands"}.get(key, unit.lower())


def _scaled_nt(num: str, unit: str) -> float | None:
    value = parse_number(num)
    if value is None:
        return None
    word = unit.lower()
    if word.startswith("billion"):
        return value * 1_000_000_000
    if word.startswith("million"):
        return value * 1_000_000
    return value


def _clear_amount(node: dict[str, Any], warnings: list[str], label: str) -> None:
    blob = f"{node.get('raw') or ''} {node.get('source_quote') or ''}"
    if not _nt_only(blob) or node.get("raw") in (None, "", "null"):
        return
    node["raw"] = None
    warnings.append(f"{label} 的引用只有 NT$/TWD、没有美元，已清空以免当成 USD")


def _sentences(full_text: str) -> list[str]:
    """按句号切开，但保留 46.72 这种小数点。"""
    protected = re.sub(r"(\d)\.(\d)", r"\1<DOT>\2", full_text or "")
    parts = re.split(r"\.(?:\s+|$)", protected)
    return [part.replace("<DOT>", ".").strip() for part in parts if part.strip()]


def _usd_windows(full_text: str) -> list[str]:
    """从 In US dollars / US$ 起看，不把同一句前面的汇率假设算进这个窗口。"""
    windows: list[str] = []
    for sentence in _sentences(full_text):
        lower = sentence.lower()
        start = lower.find("in us dollars")
        if start >= 0:
            windows.append(sentence[start:])
            continue
        if re.search(r"US\$", sentence):
            windows.append(sentence)
    return windows


def find_official_usd_amount(full_text: str, key: str) -> dict[str, str] | None:
    """新闻稿明确写出的该科目美元金额。不看汇率，也不拿别的科目的美元句子来填。"""
    label = _USD_LINE_LABELS.get(key)
    if not label or not full_text:
        return None
    amount = re.compile(
        rf"(?:{label}).{{0,80}}?(?:US\$|(?<!NT)\$)\s*(?P<num>[\d,]+(?:\.\d+)?)\s*(?P<unit>billions?|millions?|thousands?)",
        re.IGNORECASE,
    )
    for window in _usd_windows(full_text):
        if _SKIP_USD_SENTENCE.search(window):
            continue
        match = amount.search(window)
        if match is None:
            continue
        raw = match.group("num").replace(",", "")
        return {
            "raw": raw,
            "unit": _unit_word(match.group("unit")),
            "quote": _clip_quote(window, match.group(0)),
        }
    return None


def _narrative_nt(full_text: str, key: str) -> tuple[float, str] | None:
    for match in _NT_NARRATIVE.finditer(full_text or ""):
        if _NARRATIVE_KEYS.get(match.group("label").lower()) != key:
            continue
        amount = _scaled_nt(match.group("num"), match.group("unit"))
        if amount is None:
            continue
        return amount, _clip_quote(match.group(0), match.group(0))
    return None


def _matches_nt_amount(node: dict[str, Any], nt_abs: float) -> bool:
    raw_num = parse_number(None if node.get("raw") is None else str(node.get("raw")))
    scaled = ParsedAmount.from_dict(node).value
    for cand in (raw_num, scaled):
        if cand is None:
            continue
        for target in (nt_abs, nt_abs / 1_000_000, nt_abs / 1_000_000_000):
            if _close(abs(cand), abs(target)):
                return True
    return False


def _is_ntd_table_row(node: dict[str, Any], full_text: str, key: str) -> bool:
    if not _NT_TABLE.search(full_text or ""):
        return False
    blob = f"{node.get('raw') or ''} {node.get('source_quote') or ''}"
    if _quote_mentions_usd(blob):
        return False
    label = _TABLE_LABELS.get(key)
    quote = str(node.get("source_quote") or "")
    return bool(label and label.search(quote) and node.get("raw") not in (None, "", "null"))


def _should_clear_as_ntd(node: dict[str, Any], full_text: str, key: str) -> tuple[bool, str | None]:
    if node.get("raw") in (None, "", "null"):
        return False, None
    blob = f"{node.get('raw') or ''} {node.get('source_quote') or ''}"
    narrative = _narrative_nt(full_text, key)
    matches_narrative = narrative is not None and _matches_nt_amount(node, narrative[0])
    if _quote_mentions_usd(blob) and not matches_narrative:
        return False, None
    if _nt_only(blob):
        return True, narrative[1] if narrative else None
    if matches_narrative and narrative is not None:
        return True, narrative[1]
    if _is_ntd_table_row(node, full_text, key):
        return True, narrative[1] if narrative else None
    return False, None


def _adopt_official_usd(node: dict[str, Any], official: dict[str, str], warnings: list[str], label: str) -> None:
    target = ParsedAmount.from_dict({"raw": official["raw"], "unit": official["unit"]}).value
    current = ParsedAmount.from_dict(node).value
    same_value = _same_amount(node.get("raw"), official["raw"]) or _close(current, target, tol=1e-6)
    quote_ok = _quote_mentions_usd(str(node.get("source_quote") or "")) and same_value
    node["raw"] = official["raw"]
    node["unit"] = official["unit"]
    if not quote_ok:
        node["source_quote"] = official["quote"]
    if not (same_value and quote_ok):
        warnings.append(
            f"{label} 采用新闻稿明确给出的美元金额 {official['raw']} {official['unit']}，不用 NT$，也不做汇率换算"
        )


def _guard_usd_line(extracted: dict[str, Any], full_text: str, key: str, warnings: list[str]) -> None:
    official = find_official_usd_amount(full_text, key)
    node = extracted.get(key)
    if official is not None:
        if not isinstance(node, dict):
            node = {}
            extracted[key] = node
        _adopt_official_usd(node, official, warnings, key)
        return
    if not isinstance(node, dict):
        return
    clear, quote = _should_clear_as_ntd(node, full_text, key)
    if not clear:
        _clear_amount(node, warnings, key)
        return
    node["raw"] = None
    node["unit"] = None
    if quote:
        node["source_quote"] = quote
    warnings.append(f"{key} 只有 NT$、没有美元对照，已清空以免当成 USD")


def stated_margin_ratios(full_text: str) -> dict[str, Any]:
    """新闻稿直接写出的毛利率 / 营业利润率 / 净利率。返回小数，例如 66.4% → 0.664。"""
    match = _STATED_MARGINS.search(full_text or "")
    if match is None:
        return {}
    quote = _clip_quote(match.group(0), match.group(0), limit=240)
    return {
        "gross": round(float(match.group("gross")) / 100, 6),
        "operating": round(float(match.group("operating")) / 100, 6),
        "net": round(float(match.group("net")) / 100, 6),
        "quote": quote,
    }


def stated_net_income_changes(full_text: str) -> dict[str, float]:
    """净利同比 / 环比是比率，不是把台币换成美元。没有对应句子就缺省。"""
    out: dict[str, float] = {}
    yoy = _NI_YOY.search(full_text or "")
    if yoy is not None:
        sign = -1 if yoy.group("dir").lower().startswith("dec") else 1
        out["yoy"] = round(sign * float(yoy.group("pct")) / 100, 6)
    qoq = _NI_QOQ.search(full_text or "")
    if qoq is not None:
        sign = -1 if qoq.group("dir").lower().startswith("dec") else 1
        out["qoq"] = round(sign * float(qoq.group("pct")) / 100, 6)
    return out


def find_wafer_revenue_mix(full_text: str) -> dict[str, Any] | None:
    """按制程节点的晶圆收入占比。没有金额，value 用小数。"""
    if not full_text:
        return None
    lines: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _NODE_SHARE.finditer(full_text):
        key = f"{match.group('node')}nm"
        if key in seen:
            continue
        seen.add(key)
        lines.append(
            {
                "key": key,
                "name": f"{match.group('node')}纳米晶圆收入占比",
                "value": round(float(match.group("pct")) / 100, 6),
                "source_quote": _clip_quote(match.group(0), match.group(0)),
            }
        )
    advanced = _ADVANCED_SHARE.search(full_text)
    if advanced is not None:
        lines.append(
            {
                "key": "advanced",
                "name": "先进制程（7纳米及更先进）",
                "value": round(float(advanced.group("pct")) / 100, 6),
                "source_quote": _clip_quote(advanced.group(0), advanced.group(0), limit=240),
            }
        )
    if not lines:
        return None
    return {
        "kind": "share",
        "denominator": "wafer revenue",
        "lines": lines,
        "source_quote": lines[-1]["source_quote"],
    }


def _is_empty_shell(node: dict[str, Any]) -> bool:
    if node.get("raw") not in (None, "", "null"):
        return False
    if str(node.get("qualitative") or "").strip():
        return False
    if node.get("lines"):
        return False
    return True


def _is_percent_not_money(node: dict[str, Any]) -> bool:
    unit = str(node.get("unit") or "").lower()
    blob = f"{node.get('raw') or ''} {node.get('source_quote') or ''}"
    if unit in {"percent", "percentage", "%"}:
        return True
    return "%" in blob and not _quote_mentions_usd(blob)


def _normalize_kpis(extracted: dict[str, Any], full_text: str, warnings: list[str]) -> None:
    mix = find_wafer_revenue_mix(full_text)
    kpis = extracted.get("kpis")
    if not isinstance(kpis, dict):
        if not mix:
            return
        kpis = {}
        extracted["kpis"] = kpis
    had_mix = isinstance(kpis.get("wafer_revenue_mix"), dict) and bool(
        (kpis.get("wafer_revenue_mix") or {}).get("lines")
    )
    if mix:
        kpis["wafer_revenue_mix"] = mix
        if not had_mix:
            warnings.append("晶圆收入占比按新闻稿的制程节点百分比记入，不作为美元金额")
    for key in list(kpis):
        if key == "wafer_revenue_mix":
            continue
        node = kpis.get(key)
        if not isinstance(node, dict):
            continue
        blob = f"{node.get('raw') or ''} {node.get('source_quote') or ''}"
        if _is_empty_shell(node):
            kpis.pop(key, None)
            warnings.append(f"kpis.{key} 空壳已移除")
            continue
        if _is_percent_not_money(node) or _nt_only(blob):
            kpis.pop(key, None)
            warnings.append(f"kpis.{key} 不是美元金额，已移出金额科目")


def margin_or_stated(computed: float | None, stated: float | None) -> float | None:
    """美元利润额被清空后，用新闻稿写明的利润率，不用台币利润除以美元营收。"""
    if computed is not None:
        return computed
    return stated


def net_income_gap_summary(yoy: float | None, qoq: float | None, previous: str | None = None) -> str:
    """美元净利留空时的摘要：不把台币金额写成这一行的数字。"""

    def pct(value: float) -> str:
        return f"{abs(value) * 100:.1f}%"

    bits = ["新闻稿未披露美元净利，仅有新台币口径，故不记入美元金额。"]
    changes: list[str] = []
    if yoy is not None:
        changes.append(("同比下降 " if yoy < 0 else "同比增长 ") + pct(yoy))
    if qoq is not None:
        changes.append(("环比下降 " if qoq < 0 else "环比增长 ") + pct(qoq))
    if changes:
        bits.append("净利" + "，".join(changes) + "。")
    if previous:
        cause = re.search(r"(主要[^。\n]{0,40})", previous)
        if cause and "新台币" not in cause.group(1) and not re.search(r"\d", cause.group(1)):
            bits.append(cause.group(1).rstrip("。") + "。")
    return "".join(bits)


def align_adr_drivers(drivers: dict[str, Any] | None, financials: dict[str, Any] | None) -> None:
    """占比不要再挂在空的美元 KPI 上；净利留空时摘要不写台币金额。"""
    metrics = (drivers or {}).get("metrics") if isinstance(drivers, dict) else None
    if not isinstance(metrics, list):
        return
    ni = ((financials or {}).get("net_income_gaap") or {}) if isinstance(financials, dict) else {}
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        if metric.get("metric") == "kpis.advanced_node_revenue":
            metric["metric"] = "wafer_revenue_mix.advanced"
        if metric.get("metric") == "net_income_gaap" and ni.get("value") is None:
            metric["summary"] = net_income_gap_summary(
                ni.get("yoy_pct"), ni.get("qoq_pct"), metric.get("summary")
            )


def annotate_wafer_mix_changes(kpis: dict[str, Any] | None, yoy_doc: dict[str, Any] | None, qoq_doc: dict[str, Any] | None) -> None:
    if not isinstance(kpis, dict):
        return
    mix = kpis.get("wafer_revenue_mix")
    if not isinstance(mix, dict):
        return

    def lines_of(doc: dict[str, Any] | None) -> dict[str, float]:
        if not doc:
            return {}
        raw = ((doc.get("financials") or {}).get("kpis") or {}).get("wafer_revenue_mix") or {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, float] = {}
        for line in raw.get("lines") or []:
            if isinstance(line, dict) and line.get("key") and line.get("value") is not None:
                out[str(line["key"])] = float(line["value"])
        return out

    yoy = lines_of(yoy_doc)
    qoq = lines_of(qoq_doc)
    for line in mix.get("lines") or []:
        if not isinstance(line, dict) or line.get("value") is None:
            continue
        key = str(line.get("key"))
        if key in yoy:
            line["yoy_pp"] = float(line["value"]) - yoy[key]
        if key in qoq:
            line["qoq_pp"] = float(line["value"]) - qoq[key]


def apply_currency_guard(
    extracted: dict[str, Any],
    full_text: str,
    reporting_currency: str | None,
) -> list[str]:
    """返回应写入 status.warnings 的句子，并清掉只有台币的金额。"""
    currency = (reporting_currency or "USD").upper()
    if currency != "USD":
        return []
    warnings: list[str] = []
    text = full_text or ""
    if _FOREIGN.search(text):
        warnings.append("新闻稿含 NT$/TWD。reporting_currency=USD，未标明美元的金额不能当成 USD。")
    adopted_adr_eps = _prefer_adr_gaap_eps(extracted, text, warnings)
    for key in _AMOUNT_KEYS:
        if key == "eps_gaap_diluted" and adopted_adr_eps:
            continue
        if key in _USD_LINE_LABELS:
            _guard_usd_line(extracted, text, key, warnings)
            continue
        node = extracted.get(key)
        if isinstance(node, dict):
            _clear_amount(node, warnings, key)
    _normalize_kpis(extracted, text, warnings)
    return warnings
