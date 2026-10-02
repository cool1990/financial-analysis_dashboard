"""派生指标与规则检查。不调用 LLM、不联网。

增长质量检查与论点里的「可自动核对条件」共用这里的指标和比较逻辑：
配置里写 {key, op, value}，这里负责取数、比较、生成一句可读的说明。
"""

from __future__ import annotations

from typing import Any, Callable

# key → (中文名, 格式)。格式：pct = 百分比变化，ratio = 占比 / 利润率，pp = 百分点差，money = 金额
METRICS: dict[str, tuple[str, str]] = {
    "revenue": ("营收", "money"),
    "revenue_yoy": ("营收同比", "pct"),
    "revenue_qoq": ("营收环比", "pct"),
    "gross_margin": ("毛利率", "ratio"),
    "gross_margin_gaap": ("毛利率（GAAP）", "ratio"),
    "gross_margin_nongaap": ("毛利率（Non-GAAP）", "ratio"),
    "operating_margin": ("营业利润率", "ratio"),
    "eps": ("每股收益", "eps"),
    "capex_qoq": ("资本开支环比", "pct"),
    "capex_to_revenue": ("资本开支 / 营收", "ratio"),
    "fcf_margin": ("自由现金流 / 营收", "ratio"),
    "cash_conversion": ("自由现金流 / 净利润", "ratio"),
    "gaap_gap": ("Non-GAAP 比 GAAP EPS 高出", "ratio"),
    "capex_minus_revenue_growth": ("资本开支环比 − 营收环比", "pp"),
    "gm_guide_delta": ("下季毛利率指引 − 本季毛利率", "pp"),
}

OPS: dict[str, tuple[Callable[[float, float], bool], str]] = {
    ">=": (lambda a, b: a >= b, "不低于"),
    ">": (lambda a, b: a > b, "高于"),
    "<=": (lambda a, b: a <= b, "不高于"),
    "<": (lambda a, b: a < b, "低于"),
}


def _num(x: Any) -> float | None:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def _ratio(a: float | None, b: float | None) -> float | None:
    return a / b if a is not None and b else None


def derived_metrics(doc: dict[str, Any]) -> dict[str, float | None]:
    """从单季 PeriodDoc 算出可供规则使用的指标。缺数据的指标为 None。"""
    fin = doc.get("financials") or {}
    basis = (doc.get("meta") or {}).get("eps_basis") or "non_gaap"
    sfx = "gaap" if basis == "gaap" else "nongaap"

    def v(key: str, field: str = "value") -> float | None:
        return _num((fin.get(key) or {}).get(field))

    rev, capex, fcf, ni = v("revenue"), v("capex"), v("fcf"), v("net_income_gaap")
    g, ng = v("eps_gaap"), v("eps_nongaap")
    gm = v(f"gross_margin_{sfx}")
    if gm is None:
        gm = v("gross_margin_gaap")
    om = v(f"operating_margin_{sfx}")
    if om is None:
        om = v("operating_margin_gaap")
    rev_q, capex_q = v("revenue", "qoq_pct"), v("capex", "qoq_pct")

    # 下季毛利率指引（同一口径，新闻稿优先）；指引常以百分数存，如 86.25
    guide = None
    for gi in (doc.get("guidance") or {}).get("items") or []:
        if gi.get("metric_key") == f"gross_margin_{sfx}" and _num(gi.get("mid")) is not None:
            if guide is None or gi.get("source") == "press_release":
                guide = _num(gi.get("mid"))
    if guide is not None and guide > 1.5:
        guide /= 100

    return {
        "revenue": rev,
        "revenue_yoy": v("revenue", "yoy_pct"),
        "revenue_qoq": rev_q,
        "gross_margin": gm,
        "gross_margin_gaap": v("gross_margin_gaap"),
        "gross_margin_nongaap": v("gross_margin_nongaap"),
        "operating_margin": om,
        "eps": ng if basis != "gaap" and ng is not None else g,
        "capex_qoq": capex_q,
        "capex_to_revenue": _ratio(abs(capex) if capex is not None else None, rev),
        "fcf_margin": _ratio(fcf, rev),
        "cash_conversion": _ratio(fcf, ni) if ni and ni > 0 else None,
        "gaap_gap": (ng - g) / abs(g) if g and ng is not None else None,
        "capex_minus_revenue_growth": capex_q - rev_q if capex_q is not None and rev_q is not None else None,
        "gm_guide_delta": guide - gm if guide is not None and gm is not None else None,
    }


def fmt_metric(key: str, value: float | None) -> str:
    if value is None:
        return "—"
    kind = METRICS.get(key, (key, "ratio"))[1]
    if value == 0 and kind in {"pct", "pp", "ratio"}:
        return "0"
    if kind == "money":
        return f"${value / 1e9:.2f}B" if abs(value) >= 1e9 else f"${value / 1e6:.1f}M"
    if kind == "eps":
        return f"${value:.2f}"
    if kind == "pct":
        return f"{value * 100:+.1f}%"
    if kind == "pp":
        return f"{value * 100:+.1f} 个百分点"
    return f"{value * 100:.1f}%"


def evaluate(spec: dict[str, Any], metrics: dict[str, float | None]) -> dict[str, Any]:
    """按 {key, op, value} 比较，返回 {status: ok / warn / na, label, actual, text}。

    spec 可带 label（覆盖中文名）、note（附在结论后的一句解释）。
    """
    key = spec.get("key") or ""
    label = spec.get("label") or METRICS.get(key, (key, ""))[0]
    op = spec.get("op") or ">="
    thr = _num(spec.get("value"))
    actual = metrics.get(key)
    cmp = OPS.get(op)
    if cmp is None or thr is None:
        return {"status": "na", "label": label, "actual": "—", "text": f"规则写法有误：{spec}"}
    rule = f"{cmp[1]} {fmt_metric(key, thr)}"
    if actual is None:
        return {"status": "na", "label": label, "actual": "—", "text": f"缺数据，规则为{rule}"}
    ok = cmp[0](actual, thr)
    note = spec.get("note") or ""
    text = f"{fmt_metric(key, actual)}，{'达到' if ok else '未达到'}「{rule}」" + (f"；{note}" if note else "")
    return {"status": "ok" if ok else "warn", "label": label, "actual": fmt_metric(key, actual), "text": text}


def merge_checks(defaults: list[dict[str, Any]], company: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """通用层 + 公司层。公司层写了同一个 key 时覆盖通用层的阈值。"""
    own = {c.get("key") for c in company}
    out = [{**c, "layer": "通用"} for c in defaults if c.get("key") not in own]
    out += [{**c, "layer": "本公司"} for c in company]
    return out
