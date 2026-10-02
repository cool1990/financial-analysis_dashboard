from __future__ import annotations

import json
import re
from typing import Any

from pipeline.compute.topics import compute_topic_stats
from pipeline.config import load_topics
from pipeline.llm import LLMClient, LLMError
from pipeline.schemas import QAItem
from pipeline.sources.transcripts import split_prepared_and_qa


_FIRM_RE = re.compile(r"\b(?:from|with)\s+([A-Z][A-Za-z0-9&.'\-]*(?:[ ,]+[A-Z&][A-Za-z0-9&.'\-]*)*)")


def clean_firm(text: str | None) -> str | None:
    """从主持人引出语中取机构名：「...the line of Atif Malik from Citi.」→「Citi」。"""
    if not text:
        return None
    head = text.split("\n", 2)
    for line in head[:2]:
        # 「from 分析师 with/from 机构」：取最后一个匹配
        found = _FIRM_RE.findall(line)
        if found:
            firm = re.split(r"\.\s+(?:Please|Your|Go|You|The|Our)\b", found[-1])[0]
            return firm.strip(" ,.")[:80] or None
    return None


def _split_exchanges(qa_text: str) -> list[dict[str, Any]]:
    """粗拆 exchanges：以 Operator 引出下一位分析师为界。"""
    parts = re.split(r"\n(?=Operator:)", qa_text)
    exchanges = []
    for i, part in enumerate(parts):
        if not part.strip():
            continue
        analyst = None
        firm = None
        firm = clean_firm(part)
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
        # 结构化失败不代表回避：不能填 partial，否则会被计入「部分回答 / 回避」
        "directness": None,
        "evasion_note": "",
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
