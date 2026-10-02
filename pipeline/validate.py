from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any

from rapidfuzz import fuzz

from pipeline.config import load_settings
from pipeline.extract.numbers import ParsedAmount, raw_in_text


def validate_extraction(
    extracted: dict[str, Any],
    press_text: str,
    *,
    provider_reported_eps: float | None = None,
    eps_actual: float | None = None,
    snapshot_at: str | None = None,
    release_at: str | None = None,
    n_analysts: int | None = None,
) -> dict[str, Any]:
    settings = load_settings()
    warnings: list[str] = []
    needs_review = False

    # V1: raw numbers must appear in text
    for key in [
        "revenue",
        "net_income_gaap",
        "diluted_shares",
        "eps_gaap_diluted",
        "eps_nongaap_diluted",
        "gross_profit_gaap",
        "operating_income_gaap",
        "operating_cash_flow",
        "capex_gross",
    ]:
        node = extracted.get(key) or {}
        raw = node.get("raw") if isinstance(node, dict) else None
        if raw and not raw_in_text(raw, press_text):
            warnings.append(f"V1: {key} raw={raw} 未在新闻稿中找到")
            needs_review = True

    # V2: net_income / shares ≈ eps
    ni = ParsedAmount.from_dict(extracted.get("net_income_gaap"))
    shares = ParsedAmount.from_dict(extracted.get("diluted_shares"))
    eps = ParsedAmount.from_dict(extracted.get("eps_gaap_diluted"), is_eps=True)
    if ni.value is not None and shares.value is not None and eps.value is not None and shares.value != 0:
        implied = ni.value / shares.value
        if abs(implied - eps.value) > 0.02:
            warnings.append(f"V2: NI/Shares={implied:.4f} 与 GAAP EPS={eps.value} 差 > 0.02")

    # V3
    tol = settings["validation"]["eps_provider_tolerance"]
    if provider_reported_eps is not None and eps_actual is not None:
        if abs(provider_reported_eps - eps_actual) > tol:
            warnings.append(
                f"V3: eps_actual={eps_actual} 与数据商 Reported EPS={provider_reported_eps} 差 > {tol}，可能需更新 eps_basis"
            )
            needs_review = True

    # V4 snapshot age
    if snapshot_at and release_at:
        try:
            s = datetime.fromisoformat(snapshot_at.replace("Z", "+00:00"))
            r = datetime.fromisoformat(release_at.replace("Z", "+00:00"))
            age_days = (r - s).total_seconds() / 86400
            if age_days > settings["validation"]["snapshot_max_age_days"]:
                warnings.append(f"V4: 财报前快照距发布 {age_days:.1f} 天 > 3 天")
        except Exception:
            pass

    # V5 analysts
    if n_analysts is not None and n_analysts < settings["validation"]["min_analysts"]:
        warnings.append(f"V5: numberOfAnalysts={n_analysts} < 3")

    return {"needs_review": needs_review, "warnings": warnings}


_QUOTE_TRANSLATE = str.maketrans(
    {
        "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u2032": "'",
        "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"',
        "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-",
        "\u00a0": " ", "\u2009": " ", "\u202f": " ",
    }
)
_ELLIPSIS_RE = re.compile(r"\s*(?:\.{3,}|\u2026|\[\.\.\.\])\s*")


def normalize_for_quote_match(text: str) -> str:
    """统一引号/破折号/空白、`$ 1,234` 与 `$1,234`、`90 %` 与 `90%`，并转小写。"""
    text = unicodedata.normalize("NFKC", text).translate(_QUOTE_TRANSLATE).lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\$\s+(?=[\d(.])", "$", text)
    text = re.sub(r"(?<=[\d)])\s+%", "%", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    return text.strip(" \"'")


def fuzzy_quote_ok(quote: str | None, corpus: str, threshold: float = 0.9) -> bool:
    """引文是否出自 corpus。

    先做规范化后的精确子串匹配；不行再用 partial_ratio 容忍少量改字。
    LLM 用省略号拼接的引文，按片段逐段校验（每段都须命中）。
    """
    if not quote or not corpus:
        return False
    hay = normalize_for_quote_match(corpus)
    parts = [normalize_for_quote_match(p) for p in _ELLIPSIS_RE.split(quote)]
    parts = [p for p in parts if len(p) >= 8] or [normalize_for_quote_match(quote)]
    for part in parts:
        if not part:
            return False
        if part in hay:
            continue
        # rapidfuzz ratio is 0-100
        if fuzz.partial_ratio(part, hay) / 100.0 < threshold:
            return False
    return True


def classify_eps_basis(
    *,
    has_nongaap_eps: bool,
    gaap_match_ratio: float | None,
    paired_quarters: int,
    gaap_nongaap_differ: bool = True,
) -> tuple[str, list[str]]:
    """Step 0 判定表。"""
    notes: list[str] = []
    if paired_quarters < 3 or gaap_match_ratio is None:
        return "unknown", ["成功配对少于 3 个季度"]
    if not has_nongaap_eps:
        if gaap_match_ratio >= 0.75:
            return "gaap", ["新闻稿无 Non-GAAP EPS，且数据商与 GAAP 匹配度≥75%"]
        return "unknown", ["新闻稿无 Non-GAAP EPS，但数据商与 GAAP 匹配度<75%，需人工确认"]
    # has nongaap
    if gaap_match_ratio < 0.5:
        return "non_gaap", ["有 Non-GAAP EPS，且数据商与 GAAP 匹配度<50%"]
    if gaap_match_ratio >= 0.75 and gaap_nongaap_differ:
        return "gaap", ["有 Non-GAAP，但数据商仍与 GAAP 高度匹配（少见）"]
    notes.append("有 Non-GAAP EPS，默认 non_gaap，并建议财报后用 V3 复核")
    return "non_gaap", notes
