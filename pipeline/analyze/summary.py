from __future__ import annotations

import json
from typing import Any

from pipeline.llm import LLMClient
from pipeline.schemas import SummaryResult


def summarize_period(
    period_doc: dict[str, Any],
    prior_watchlist: list[Any] | None = None,
) -> dict[str, Any]:
    llm = LLMClient()
    prompt = llm.load_prompt("summarize.md")
    payload = {
        "scorecard": period_doc.get("scorecard"),
        "financials": period_doc.get("financials"),
        "guidance": period_doc.get("guidance"),
        "qa": {
            "items": period_doc.get("qa", {}).get("items", []),
            "hot_topics": period_doc.get("qa", {}).get("hot_topics", []),
            "evasive_list": period_doc.get("qa", {}).get("evasive_list", []),
        },
        "prior_watchlist": prior_watchlist or [],
        "drivers": period_doc.get("drivers"),
    }
    user = prompt + "\n\n" + json.dumps(payload, ensure_ascii=False)[:60000]
    result = llm.complete_json("summarize.md", user, SummaryResult)
    return result.model_dump()
