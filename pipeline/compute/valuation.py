"""市盈率。股价和 EPS 币种不同时，先把股价换到 EPS 的币种再相除。

港股：股价是港币，EPS 是人民币。HKDCNY 表示 1 港币折合的人民币。
人民币股价 = 港币股价 × HKDCNY，市盈率 = 人民币股价 / 人民币 EPS。
没有当天汇率时不计算，避免把两种货币直接相除。
"""

from __future__ import annotations

from typing import Any

PENDING = "待核对"


def price_currency(cfg: dict[str, Any] | None) -> str:
    listing = str((cfg or {}).get("listing") or "").strip().lower()
    if listing == "hk":
        return "HKD"
    return "USD"


def eps_currency(cfg: dict[str, Any] | None) -> str:
    text = str((cfg or {}).get("reporting_currency") or "USD").strip().upper()
    return text or "USD"


def pe_multiple(
    price: float | None,
    eps: float | None,
    *,
    price_ccy: str,
    eps_ccy: str,
    fx: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """返回 pe、展示文字和写在 P/E 旁边的汇率。算不出时 pe 为 None。"""
    if price is None or eps in (None, 0):
        return {"pe": None, "text": "—", "fx": "", "source": ""}
    price_ccy = (price_ccy or "USD").upper()
    eps_ccy = (eps_ccy or "USD").upper()
    if price_ccy == eps_ccy:
        pe = price / eps
        return {"pe": pe, "text": f"{pe:.1f}×", "fx": "", "source": ""}
    if {price_ccy, eps_ccy} != {"HKD", "CNY"}:
        return {"pe": None, "text": PENDING, "fx": "", "source": ""}
    rate = None
    day = ""
    source = ""
    pair = ""
    if isinstance(fx, dict):
        rate = fx.get("rate")
        day = str(fx.get("date") or "")
        source = str(fx.get("source") or "")
        pair = str(fx.get("pair") or "HKDCNY").upper()
    try:
        rate_f = float(rate) if rate is not None else 0.0
    except (TypeError, ValueError):
        rate_f = 0.0
    if pair != "HKDCNY" or rate_f <= 0 or not day:
        return {"pe": None, "text": PENDING, "fx": "", "source": ""}
    if price_ccy == "HKD":
        pe = price * rate_f / eps
    else:
        pe = (price / rate_f) / eps
    return {
        "pe": pe,
        "text": f"{pe:.1f}×",
        "fx": f"HKDCNY {rate_f:.4f}（{day}）",
        "source": source,
    }
