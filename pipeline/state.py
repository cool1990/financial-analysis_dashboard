from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.config import data_dir


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


def add_processed(ticker: str, accession: str) -> None:
    path = data_dir(ticker) / "processed.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"accessions": sorted(load_processed(ticker) | {accession})}
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_period_json(ticker: str, fiscal_period: str) -> dict[str, Any] | None:
    path = data_dir(ticker) / f"{fiscal_period}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_period_json(ticker: str, fiscal_period: str, data: dict[str, Any]) -> Path:
    path = data_dir(ticker) / f"{fiscal_period}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def empty_period_doc(ticker: str, fiscal_period: str, **meta: Any) -> dict[str, Any]:
    return {
        "meta": {
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
