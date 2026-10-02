from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


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
    # parsed fields
    low: Optional[float] = None
    mid: Optional[float] = None
    high: Optional[float] = None
    call_variant: Optional[dict[str, Any]] = None
    change_vs_prior: Optional[str] = None


class DriverItem(BaseModel):
    type: str
    direction: Optional[str] = None
    is_primary: bool = False
    description: Optional[str] = None
    evidence_quote: Optional[str] = None
    source: Optional[str] = None
    is_inference: bool = False
    reasoning: Optional[str] = None
    quote_unverified: bool = False


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


class ThesisCheck(BaseModel):
    index: int
    status: Literal["strengthened", "unchanged", "weakened"] = "unchanged"
    evidence: str = ""
    quote: Optional[str] = None
    source: Optional[str] = None
    falsified: bool = False


class TriggerCheck(BaseModel):
    name: str
    state: Literal["triggered", "not_triggered", "unknown"] = "unknown"
    reason: str = ""


class QuestionAnswer(BaseModel):
    index: int
    answered: bool = False
    answer: str = ""
    quote: Optional[str] = None
    source: Optional[str] = None


class NewConcern(BaseModel):
    concern: str
    raised_by: Optional[str] = None
    why: Optional[str] = None


class PositionReviewResult(BaseModel):
    theses: list[ThesisCheck] = Field(default_factory=list)
    triggers: list[TriggerCheck] = Field(default_factory=list)
    answers: list[QuestionAnswer] = Field(default_factory=list)
    new_concerns: list[NewConcern] = Field(default_factory=list)


# --- Period document schema (schema_version = 1) ---

Verdict = Literal["beat", "miss", "inline", "unknown", ""]


class Benchmark(BaseModel):
    """scorecard 与 financials 共用的基准结构。"""

    model_config = ConfigDict(extra="forbid")

    value: Optional[float] = None
    source: str = "none"  # consensus / prior_guidance_mid / derived_from_guidance / none
    diff: Optional[float] = None
    diff_pct: Optional[float] = None  # 金额类
    diff_pp: Optional[float] = None  # 比率类（小数形式，如 0.011 = 1.1pp）


class MetricBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    value: Optional[float] = None
    yoy_pct: Optional[float] = None
    qoq_pct: Optional[float] = None
    yoy_pp: Optional[float] = None
    qoq_pp: Optional[float] = None
    benchmark: Benchmark = Field(default_factory=Benchmark)
    source_quote: Optional[str] = None


class ScorecardItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str
    actual: Optional[float] = None
    benchmark: Benchmark = Field(default_factory=Benchmark)
    verdict: Verdict | str = ""
    basis: Optional[str] = None


class PeriodMeta(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: Literal[1] = 1
    ticker: str
    fiscal_period: str
    calendar_quarter: Optional[str] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    release_at_utc: Optional[str] = ""
    accession: Optional[str] = ""
    press_release_url: Optional[str] = ""
    transcript_source: Optional[str] = None
    eps_basis: Optional[str] = None


class PeriodStatus(BaseModel):
    model_config = ConfigDict(extra="allow")

    stage: str
    needs_review: bool = False
    warnings: list[str] = Field(default_factory=list)


class PriceReaction(BaseModel):
    model_config = ConfigDict(extra="allow")

    next_day_pct: Optional[float] = None
    close_before: Optional[float] = None
    close_after: Optional[float] = None
    note: Optional[str] = None


class Financials(BaseModel):
    model_config = ConfigDict(extra="allow")

    revenue: Optional[MetricBlock] = None
    gross_margin_gaap: Optional[MetricBlock] = None
    gross_margin_nongaap: Optional[MetricBlock] = None
    operating_margin_gaap: Optional[MetricBlock] = None
    operating_margin_nongaap: Optional[MetricBlock] = None
    eps_gaap: Optional[MetricBlock] = None
    eps_nongaap: Optional[MetricBlock] = None
    operating_cash_flow: Optional[MetricBlock] = None
    capex: Optional[MetricBlock] = None
    fcf: Optional[MetricBlock] = None
    kpis: dict[str, Any] = Field(default_factory=dict)


class DriversBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    stage: int = 0
    metrics: list[MetricDrivers] = Field(default_factory=list)
    status: Optional[str] = None


class AnalystRevisions(BaseModel):
    model_config = ConfigDict(extra="allow")

    t_minus_1: dict[str, Any] = Field(default_factory=dict)
    t_plus_1: dict[str, Any] = Field(default_factory=dict)
    t_plus_3: dict[str, Any] = Field(default_factory=dict)
    t_plus_7: dict[str, Any] = Field(default_factory=dict)
    gap_closure: Optional[Any] = None


class GuidanceBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    items: list[dict[str, Any]] = Field(default_factory=list)
    prior_guidance_review: list[Any] = Field(default_factory=list)
    vs_consensus: list[Any] = Field(default_factory=list)
    analyst_revisions: AnalystRevisions = Field(default_factory=AnalystRevisions)


class QABlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    items: list[dict[str, Any]] = Field(default_factory=list)
    topic_stats: dict[str, Any] = Field(default_factory=dict)
    new_topics: list[str] = Field(default_factory=list)
    dropped_topics: list[str] = Field(default_factory=list)
    hot_topics: list[str] = Field(default_factory=list)
    evasive_list: list[Any] = Field(default_factory=list)


class SummaryBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    headline: Optional[str] = ""
    key_findings: list[Any] = Field(default_factory=list)
    prior_watchlist_review: list[Any] = Field(default_factory=list)
    next_watchlist: list[Any] = Field(default_factory=list)


class PeriodDoc(BaseModel):
    """单季 data/{TICKER}/{fiscal_period}.json 的完整 schema。"""

    model_config = ConfigDict(extra="forbid")

    meta: PeriodMeta
    status: PeriodStatus
    scorecard: list[ScorecardItem] = Field(default_factory=list)
    price_reaction: PriceReaction = Field(default_factory=PriceReaction)
    financials: Financials = Field(default_factory=Financials)
    drivers: DriversBlock = Field(default_factory=DriversBlock)
    guidance: GuidanceBlock = Field(default_factory=GuidanceBlock)
    qa: QABlock = Field(default_factory=QABlock)
    summary: SummaryBlock = Field(default_factory=SummaryBlock)
    # 仓位评估（config/theses/{TICKER}.yaml 存在时才有）：theses / triggers / answers / new_concerns / fingerprint
    thesis_review: dict[str, Any] = Field(default_factory=dict)


def validate_period_doc(data: dict[str, Any]) -> PeriodDoc:
    """校验并规范化单季 JSON；失败抛出 ValidationError。"""
    meta = dict(data.get("meta") or {})
    meta.setdefault("schema_version", 1)
    payload = {**data, "meta": meta}
    return PeriodDoc.model_validate(payload)
