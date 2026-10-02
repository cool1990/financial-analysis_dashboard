from __future__ import annotations

import json
import re
from typing import Any

from pipeline.compute.topics import compute_topic_stats
from pipeline.config import load_topics
from pipeline.llm import LLMClient, LLMError
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


def _fallback_item(ex: dict[str, Any], reason: str) -> dict[str, Any]:
    excerpt = re.sub(r"\s+", " ", (ex.get("text") or ""))[:220]
    return {
        "exchange_id": ex["exchange_id"],
        "analyst": ex.get("analyst"),
        "firm": ex.get("firm"),
        "topic": "其他",
        "question_summary": f"本轮问答未能自动结构化（{reason}）。",
        "answer_summary": excerpt or "原文片段不足，请重跑 Stage2 或查看文字稿。",
        "new_numbers": [],
        "directness": "partial",
        "evasion_note": "结构化失败，内容仅供参考，不代表回避回答。",
        "tone": "neutral",
        "answer_quote": "",
        "parse_failed": True,
    }


def _structure_batch(llm: LLMClient, prompt_tmpl: str, batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    user = prompt_tmpl + "\n\n" + json.dumps(batch, ensure_ascii=False)
    try:
        models = llm.complete_json_list("structure_qa.md", user, QAItem)
        return [m.model_dump() for m in models]
    except Exception as e:
        # 不再逐条重试：单次调用可达数分钟，逐条会把 Stage2 拖到十几分钟并重复烧额度。
        # 批次失败则整批用占位，由人工或 --force 重跑补齐。
        reason = "模型返回空或非 JSON" if "Expecting value" in str(e) else "解析失败"
        if isinstance(e, LLMError):
            reason = "模型调用失败" if ("HTTP" in str(e) or "超时" in str(e) or "timeout" in str(e).lower()) else reason
        return [_fallback_item(ex, reason) for ex in batch]


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
    # 更大批次 → 更少次 OpenRouter 往返（原先 batch=2 + 失败逐条重试会放大耗时）
    batch_size = 5
    for i in range(0, len(exchanges), batch_size):
        batch = exchanges[i : i + batch_size]
        print(f"[stage2/qa] batch {i // batch_size + 1}/{(len(exchanges) + batch_size - 1) // batch_size} size={len(batch)}", flush=True)
        items.extend(_structure_batch(llm, prompt_tmpl, batch))
    stats = compute_topic_stats(items)
    return {
        "prepared_remarks": prepared,
        "qa_text": qa,
        "items": items,
        **stats,
    }
