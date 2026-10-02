from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from pipeline.config import data_dir
from pipeline.schemas import validate_period_doc


def state_path(ticker: str, fiscal_period: str) -> Path:
    return data_dir(ticker) / "state" / f"{fiscal_period}.json"


def load_state(ticker: str, fiscal_period: str) -> dict[str, Any]:
    path = state_path(ticker, fiscal_period)
    if not path.exists():
        return {
            "ticker": ticker.upper(),
            "fiscal_period": fiscal_period,
            "stage": "scheduled",
            "retries": {},
            "errors": [],
            "timestamps": {},
        }
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(ticker: str, fiscal_period: str, state: dict[str, Any]) -> None:
    path = state_path(ticker, fiscal_period)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def mark_stage(ticker: str, fiscal_period: str, stage: str) -> dict[str, Any]:
    state = load_state(ticker, fiscal_period)
    state["stage"] = stage
    state.setdefault("timestamps", {})[stage] = datetime.now(timezone.utc).isoformat()
    save_state(ticker, fiscal_period, state)
    return state


def record_error(ticker: str, fiscal_period: str, stage: str, error: str) -> dict[str, Any]:
    state = load_state(ticker, fiscal_period)
    state.setdefault("errors", []).append(
        {"stage": stage, "error": error, "at": datetime.now(timezone.utc).isoformat()}
    )
    retries = state.setdefault("retries", {})
    retries[stage] = int(retries.get(stage, 0)) + 1
    if retries[stage] >= 5:
        state["stage"] = "failed"
    save_state(ticker, fiscal_period, state)
    return state


def load_processed(ticker: str) -> set[str]:
    path = data_dir(ticker) / "processed.json"
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    return set(data.get("accessions", []))


def _load_processed_file(ticker: str) -> dict[str, Any]:
    path = data_dir(ticker) / "processed.json"
    if not path.exists():
        return {"accessions": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_processed_file(ticker: str, data: dict[str, Any]) -> None:
    path = data_dir(ticker) / "processed.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not data.get("failures"):
        data.pop("failures", None)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def add_processed(ticker: str, accession: str) -> None:
    data = _load_processed_file(ticker)
    data["accessions"] = sorted(set(data.get("accessions", [])) | {accession})
    (data.get("failures") or {}).pop(accession, None)
    _save_processed_file(ticker, data)


def load_failures(ticker: str) -> dict[str, int]:
    """每份 8-K 的 Stage1 失败次数；达到 polling.max_retries 后 poll 不再自动重试。"""
    return {k: int(v) for k, v in (_load_processed_file(ticker).get("failures") or {}).items()}


def record_filing_failure(ticker: str, accession: str) -> int:
    data = _load_processed_file(ticker)
    if accession in set(data.get("accessions", [])):
        return 0
    failures = data.setdefault("failures", {})
    failures[accession] = int(failures.get(accession, 0)) + 1
    _save_processed_file(ticker, data)
    return failures[accession]


def load_period_json(ticker: str, fiscal_period: str) -> dict[str, Any] | None:
    path = data_dir(ticker) / f"{fiscal_period}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_period_json(ticker: str, fiscal_period: str, data: dict[str, Any]) -> Path:
    """写入前必须通过 PeriodDoc 校验。"""
    meta = dict(data.get("meta") or {})
    meta["schema_version"] = 1
    data = {**data, "meta": meta}
    try:
        doc = validate_period_doc(data)
    except ValidationError as e:
        raise ValueError(f"PeriodDoc 校验失败 ({ticker} {fiscal_period}): {e}") from e
    path = data_dir(ticker) / f"{fiscal_period}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = doc.model_dump(mode="json")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def empty_period_doc(ticker: str, fiscal_period: str, **meta: Any) -> dict[str, Any]:
    return {
        "meta": {
            "schema_version": 1,
            "ticker": ticker.upper(),
            "fiscal_period": fiscal_period,
            "calendar_quarter": meta.get("calendar_quarter"),
            "period_start": meta.get("period_start"),
            "period_end": meta.get("period_end"),
            "release_at_utc": meta.get("release_at_utc", ""),
            "accession": meta.get("accession", ""),
            "press_release_url": meta.get("press_release_url", ""),
            "transcript_source": meta.get("transcript_source"),
            "eps_basis": meta.get("eps_basis"),
        },
        "status": {"stage": "detected", "needs_review": False, "warnings": []},
        "scorecard": [],
        "price_reaction": {"next_day_pct": None, "close_before": None, "close_after": None},
        "financials": {},
        "drivers": {"stage": 0, "metrics": []},
        "guidance": {
            "items": [],
            "prior_guidance_review": [],
            "vs_consensus": [],
            "analyst_revisions": {
                "t_minus_1": {},
                "t_plus_1": {},
                "t_plus_3": {},
                "t_plus_7": {},
                "gap_closure": None,
            },
        },
        "qa": {
            "items": [],
            "topic_stats": {},
            "new_topics": [],
            "dropped_topics": [],
            "hot_topics": [],
            "evasive_list": [],
        },
        "summary": {"headline": "", "key_findings": [], "prior_watchlist_review": [], "next_watchlist": []},
    }
