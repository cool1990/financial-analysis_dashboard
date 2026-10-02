from __future__ import annotations

import json
import re
from typing import Any

from pipeline.compute.topics import compute_topic_stats
from pipeline.config import load_topics
from pipeline.llm import LLMClient
from pipeline.schemas import QAItem
from pipeline.sources.transcripts import split_prepared_and_qa


def _split_exchanges(qa_text: str) -> list[dict[str, Any]]:
    """粗拆 exchanges：以 Operator 引出下一位分析师为界。"""
    parts = re.split(r"\n(?=Operator:)", qa_text)
    exchanges = []
    for i, part in enumerate(parts):
        if not part.strip():
            continue
        analyst = None
        firm = None
        m = re.search(r"(?:from|with)\s+([A-Za-z0-9&.,\-\s]+)", part)
        if m:
            firm = m.group(1).strip()[:80]
        name_m = re.search(r"\n([A-Z][a-z]+(?:\s[A-Z][a-z]+)+):", part)
        if name_m:
            analyst = name_m.group(1)
        exchanges.append({"exchange_id": str(i + 1), "analyst": analyst, "firm": firm, "text": part.strip()})
    return exchanges


def structure_qa(
    transcript: str,
    press_release_numbers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prepared, qa = split_prepared_and_qa(transcript)
    exchanges = _split_exchanges(qa) if qa else []
    topics = load_topics()
    topic_list = topics.get("approved", []) + topics.get("pending_review", [])
    llm = LLMClient()
    prompt_tmpl = llm.load_prompt(
        "structure_qa.md",
        topics=json.dumps(topic_list, ensure_ascii=False),
        press_release_numbers=json.dumps(press_release_numbers or {}, ensure_ascii=False),
    )
    items: list[dict[str, Any]] = []
    # batch ~ few exchanges
    batch_size = 3
    for i in range(0, len(exchanges), batch_size):
        batch = exchanges[i : i + batch_size]
        user = prompt_tmpl + "\n\n" + json.dumps(batch, ensure_ascii=False)
        try:
            models = llm.complete_json_list("structure_qa.md", user, QAItem)
            items.extend([m.model_dump() for m in models])
        except Exception as e:
            for ex in batch:
                items.append(
                    {
                        "exchange_id": ex["exchange_id"],
                        "analyst": ex.get("analyst"),
                        "firm": ex.get("firm"),
                        "topic": "其他",
                        "question_summary": f"（解析失败）{e}",
                        "answer_summary": "",
                        "new_numbers": [],
                        "directness": "partial",
                        "evasion_note": str(e),
                        "tone": "neutral",
                        "answer_quote": "",
                    }
                )
    stats = compute_topic_stats(items)
    return {
        "prepared_remarks": prepared,
        "qa_text": qa,
        "items": items,
        **stats,
    }
