from __future__ import annotations

import json
import re
from typing import Any

from pipeline.compute.topics import compute_topic_stats
from pipeline.config import load_topics
from pipeline.llm import CostLimitExceeded, LLMClient, LLMTruncatedError
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
        "question_summary": "",
        "answer_summary": "",
        "raw_excerpt": excerpt,
        "failure_reason": reason,
        "new_numbers": [],
        # 结构化失败不代表回避：不能填 partial，否则会被计入「部分回答 / 回避」
        "directness": None,
        "evasion_note": "",
        "tone": "neutral",
        "answer_quote": "",
        "parse_failed": True,
    }


def _failure_reason(e: Exception) -> str:
    msg = str(e)
    if isinstance(e, LLMTruncatedError):
        return "模型输出被截断"
    if isinstance(e, CostLimitExceeded):
        return "本轮 LLM 额度已用完"
    if "HTTP" in msg or "超时" in msg or "timeout" in msg.lower():
        return "模型调用失败"
    if "Expecting value" in msg:
        return "模型返回空内容"
    return "解析失败"


def _structure_batch(
    llm: LLMClient,
    prompt_tmpl: str,
    batch: list[dict[str, Any]],
    *,
    split: bool = True,
) -> list[dict[str, Any]]:
    user = prompt_tmpl + "\n\n" + json.dumps(batch, ensure_ascii=False)
    try:
        models = llm.complete_json_list("structure_qa.md", user, QAItem)
        return [m.model_dump() for m in models]
    except Exception as e:
        ids = ",".join(str(ex.get("exchange_id")) for ex in batch)
        # 失败多因一批输出太长被截断 / 返回空：拆成两半重试。截断说明输出确实装不下，
        # 继续对半拆到单轮为止；其它错误只拆一层。总调用次数不超过 2×批大小。熔断后不再尝试。
        truncated = isinstance(e, LLMTruncatedError)
        if (split or truncated) and len(batch) > 1 and not isinstance(e, CostLimitExceeded):
            mid = (len(batch) + 1) // 2
            print(f"[stage2/qa] 第 {ids} 轮失败（{_failure_reason(e)}），拆成 {mid}+{len(batch) - mid} 重试", flush=True)
            return _structure_batch(llm, prompt_tmpl, batch[:mid], split=False) + _structure_batch(
                llm, prompt_tmpl, batch[mid:], split=False
            )
        print(f"[stage2/qa] 第 {ids} 轮放弃（{_failure_reason(e)}）：{str(e)[:160]}", flush=True)
        return [_fallback_item(ex, _failure_reason(e)) for ex in batch]


def _prompt(llm: LLMClient, press_release_numbers: dict[str, Any] | None) -> str:
    topics = load_topics()
    topic_list = topics.get("approved", []) + topics.get("pending_review", [])
    return llm.load_prompt(
        "structure_qa.md",
        topics=json.dumps(topic_list, ensure_ascii=False),
        press_release_numbers=json.dumps(press_release_numbers or {}, ensure_ascii=False),
    )


def repair_failed_items(
    transcript: str,
    items: list[dict[str, Any]],
    press_release_numbers: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """只重跑结构化失败的那几轮问答，其余结果原样保留。返回 (新列表, 修复轮数)。"""
    failed_ids = {str(i.get("exchange_id")) for i in items if i.get("parse_failed")}
    if not failed_ids:
        return items, 0
    _, qa = split_prepared_and_qa(transcript)
    exchanges = [ex for ex in (_split_exchanges(qa) if qa else []) if ex["exchange_id"] in failed_ids]
    if not exchanges:
        return items, 0
    llm = LLMClient()
    fresh = _structure_batch(llm, _prompt(llm, press_release_numbers), exchanges)
    by_id: dict[str, list[dict[str, Any]]] = {}
    for f in fresh:
        by_id.setdefault(str(f.get("exchange_id")), []).append(f)
    out: list[dict[str, Any]] = []
    repaired = 0
    for item in items:
        eid = str(item.get("exchange_id"))
        if item.get("parse_failed") and eid in by_id:
            new_items = by_id.pop(eid)
            if not any(n.get("parse_failed") for n in new_items):
                repaired += 1
            out.extend(new_items)
        else:
            out.append(item)
    return out, repaired


def structure_qa(
    transcript: str,
    press_release_numbers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prepared, qa = split_prepared_and_qa(transcript)
    exchanges = _split_exchanges(qa) if qa else []
    llm = LLMClient()
    prompt_tmpl = _prompt(llm, press_release_numbers)
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
