"""股票配置里的上市地与 SEC 业绩发现规则。

国内默认 8-K Item 2.02。ADR 用 6-K（含 6-K/A）加 exhibit 描述过滤，不用 Item 2.02。
港股 listing=hk 与 domestic / adr 一样是合法上市地，但不走 SEC。
第二个 ADR 只改 yaml：写入 listing / reporting_currency / accounting_basis / sec.*。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pipeline.config import load_settings


Listing = Literal["domestic", "adr", "hk"]
AccountingBasis = Literal["us_gaap", "ifrs", "taiwan_ifrs", "hkfrs"]
LISTINGS = frozenset({"domestic", "adr", "hk"})

# 与 ADR 约定草案一致。股票 yaml 写了同名字段就覆盖；未写的键用这里。
DEFAULT_ADR_EARNINGS_6K: dict[str, Any] = {
    "require_exhibit_desc_any": [
        r"(?i)press\s*release",
        r"(?i)earnings",
    ],
    "reject_exhibit_desc_any": [
        r"(?i)consolidated\s+financial\s+statements",
        r"(?i)monthly\s+revenue",
        r"(?i)dividend",
    ],
    "reject_primary_doc_any": [
        r"(?i)monthend",
        r"(?i)revenue20",
        r"(?i)dividend",
        r"(?i)board",
        r"(?i)agm",
        r"(?i)fsx",
    ],
    "prefer_exhibit_desc": r"(?i)press\s*release",
}


def _compile_pattern(pattern: str, field: str) -> None:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"sec.earnings_6k.{field} 正则无效: {pattern!r} ({exc})") from exc


class Earnings6kFilters(BaseModel):
    """6-K 业绩过滤器。描述匹配用正则，不写死某只股票的文件名。"""

    model_config = ConfigDict(extra="forbid")

    require_exhibit_desc_any: list[str] = Field(default_factory=list)
    reject_exhibit_desc_any: list[str] = Field(default_factory=list)
    reject_primary_doc_any: list[str] = Field(default_factory=list)
    prefer_exhibit_desc: str = r"(?i)press\s*release"

    @model_validator(mode="after")
    def _patterns_compile(self) -> "Earnings6kFilters":
        for field in (
            "require_exhibit_desc_any",
            "reject_exhibit_desc_any",
            "reject_primary_doc_any",
        ):
            for pattern in getattr(self, field):
                if not isinstance(pattern, str) or not pattern.strip():
                    raise ValueError(f"sec.earnings_6k.{field} 须为非空正则字符串")
                _compile_pattern(pattern, field)
        _compile_pattern(self.prefer_exhibit_desc, "prefer_exhibit_desc")
        return self


class EarningsSpec(BaseModel):
    """从 ticker yaml（以及 settings 里的同名 profile）解析出的发现规则。"""

    model_config = ConfigDict(extra="forbid")

    listing: Listing
    reporting_currency: str = "USD"
    accounting_basis: Optional[AccountingBasis] = None
    earnings_forms: list[str]
    earnings_items: list[str]
    earnings_6k: Optional[Earnings6kFilters] = None

    @field_validator("reporting_currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        text = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", text):
            raise ValueError("reporting_currency 须为 3 位字母，例如 USD")
        return text

    @field_validator("earnings_forms")
    @classmethod
    def _forms(cls, value: list[str]) -> list[str]:
        return [str(item).strip() for item in value if str(item).strip()]

    @model_validator(mode="after")
    def _forms_required_unless_hk(self) -> "EarningsSpec":
        if self.listing == "hk":
            if self.earnings_forms or self.earnings_items or self.earnings_6k is not None:
                raise ValueError("listing=hk 不使用 SEC earnings_forms / earnings_items / earnings_6k")
            return self
        if not self.earnings_forms:
            raise ValueError("sec.earnings_forms 必须是非空列表")
        return self

    @field_validator("earnings_items")
    @classmethod
    def _items(cls, value: list[str]) -> list[str]:
        return [str(item).strip() for item in value if str(item).strip()]


def _settings_profiles() -> dict[str, Any]:
    sec = load_settings().get("sec") or {}
    profiles = sec.get("earnings_6k_profiles") or {}
    return profiles if isinstance(profiles, dict) else {}


def _apply_profiles(merged: dict[str, Any], sec: dict[str, Any], listing: str) -> None:
    """settings 里的同名 profile 覆盖股票 yaml 已写的键。"""
    profiles = _settings_profiles()
    inline = sec.get("earnings_6k")
    names: list[str] = []
    if isinstance(inline, str) and inline.strip():
        names.append(inline.strip())
    profile = sec.get("earnings_6k_profile")
    if profile is not None and str(profile).strip():
        names.append(str(profile).strip())
    if listing in profiles and listing not in names:
        names.append(listing)
    for name in names:
        block = profiles.get(name)
        if not isinstance(block, dict):
            raise ValueError(f"settings.sec.earnings_6k_profiles 没有名为 {name!r} 的过滤器")
        merged.update(block)


def resolve_earnings_spec(cfg: dict[str, Any] | None) -> EarningsSpec:
    """未写 listing 时等同 domestic + 8-K Item 2.02。listing=hk 不套 SEC 表单。"""
    cfg = cfg or {}
    listing = str(cfg.get("listing") or "domestic").strip().lower()
    if listing not in LISTINGS:
        raise ValueError(f"listing 只能是 domestic、adr 或 hk，收到 {listing!r}")
    sec = cfg.get("sec") or {}
    if not isinstance(sec, dict):
        raise ValueError("sec 必须是映射")

    if listing == "adr":
        forms = list(sec.get("earnings_forms") or ["6-K"])
        items = list(sec["earnings_items"]) if "earnings_items" in sec else []
        merged = dict(DEFAULT_ADR_EARNINGS_6K)
        inline = sec.get("earnings_6k")
        if isinstance(inline, dict):
            merged.update(inline)
        _apply_profiles(merged, sec, listing)
        filters: Earnings6kFilters | None = Earnings6kFilters.model_validate(merged)
    elif listing == "hk":
        forms = []
        items = []
        filters = None
    else:
        forms = list(sec.get("earnings_forms") or ["8-K"])
        items = list(sec["earnings_items"]) if "earnings_items" in sec else ["2.02"]
        filters = None

    basis = cfg.get("accounting_basis")
    if isinstance(basis, str) and not basis.strip():
        basis = None
    return EarningsSpec(
        listing=listing,  # type: ignore[arg-type]
        reporting_currency=str(cfg.get("reporting_currency") or "USD"),
        accounting_basis=basis,
        earnings_forms=forms,
        earnings_items=items,
        earnings_6k=filters,
    )


def earnings_filter_fingerprint(spec: EarningsSpec) -> str:
    """过滤器变了之后，旧的 ignore 记录不再沿用。"""
    payload = spec.model_dump(mode="json")
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
