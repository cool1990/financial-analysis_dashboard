from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class RawNumber(BaseModel):
    raw: Optional[str] = None
    unit: Optional[str] = None
    source_quote: Optional[str] = None
    period_type: Optional[Literal["3m", "ytd"]] = None
    months: Optional[int] = None
    label: Optional[str] = None
    qualitative: Optional[str] = None


class ExtractFinancials(BaseModel):
    period_label: Optional[str] = None
    period_end_date: Optional[str] = None
    revenue: Optional[RawNumber] = None
    net_income_gaap: Optional[RawNumber] = None
    diluted_shares: Optional[RawNumber] = None
    cost_of_revenue: Optional[RawNumber] = None
    gross_profit_gaap: Optional[RawNumber] = None
    gross_profit_nongaap: Optional[RawNumber] = None
    rd_expense: Optional[RawNumber] = None
    sga_expense: Optional[RawNumber] = None
    operating_income_gaap: Optional[RawNumber] = None
    operating_income_nongaap: Optional[RawNumber] = None
    eps_gaap_diluted: Optional[RawNumber] = None
    eps_nongaap_diluted: Optional[RawNumber] = None
    nongaap_variants: list[Any] = Field(default_factory=list)
    has_nongaap_eps: Optional[bool] = None
    operating_cash_flow: Optional[RawNumber] = None
    capex_gross: Optional[RawNumber] = None
    capex_net: Optional[RawNumber] = None
    kpis: dict[str, Any] = Field(default_factory=dict)
    prior_year_comparables: dict[str, Any] = Field(default_factory=dict)


class GuidanceItem(BaseModel):
    metric_key: str
    metric_label: Optional[str] = None
    period: Optional[str] = None
    type: Literal["numeric", "directional", "qualitative"] = "numeric"
    low_raw: Optional[str] = None
    high_raw: Optional[str] = None
    point_raw: Optional[str] = None
    plus_minus_raw: Optional[str] = None
    basis: Optional[str] = None
    direction: Optional[str] = None
    statement: Optional[str] = None
    source: Optional[str] = None
    source_quote: Optional[str] = None


class DriverItem(BaseModel):
    type: str
    direction: Optional[str] = None
    is_primary: bool = False
    description: Optional[str] = None
    evidence_quote: Optional[str] = None
    source: Optional[str] = None
    is_inference: bool = False
    reasoning: Optional[str] = None


class MetricDrivers(BaseModel):
    metric: str
    summary: Optional[str] = None
    drivers: list[DriverItem] = Field(default_factory=list)
    conflicts: list[Any] = Field(default_factory=list)


class DriversResult(BaseModel):
    metrics: list[MetricDrivers] = Field(default_factory=list)


class QAItem(BaseModel):
    exchange_id: Optional[str] = None
    analyst: Optional[str] = None
    firm: Optional[str] = None
    topic: Optional[str] = None
    is_new_topic: bool = False
    question_summary: Optional[str] = None
    answer_summary: Optional[str] = None
    new_numbers: list[Any] = Field(default_factory=list)
    directness: Optional[str] = None
    evasion_note: Optional[str] = None
    tone: Optional[str] = None
    answer_quote: Optional[str] = None


class SummaryResult(BaseModel):
    headline: Optional[str] = None
    key_findings: list[Any] = Field(default_factory=list)
    prior_watchlist_review: list[Any] = Field(default_factory=list)
    next_watchlist: list[Any] = Field(default_factory=list)
