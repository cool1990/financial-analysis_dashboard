from __future__ import annotations

import json
import re
from typing import Any

from pipeline.analyze.qa_facts import (
    analyst_subquestion_count,
    attach_flags,
    fact_label,
    false_friend_flags,
    missing_answer_facts,
)
from pipeline.compute.topics import compute_topic_stats
from pipeline.config import load_settings, load_topics
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


def _qa_client() -> LLMClient:
    """问答用 settings.llm.qa_model。留空则回退到摘要用的默认模型。

    未配置 qa_reasoning_effort / qa_max_tokens / qa_request_timeout_sec 时，
    沿用摘要模型的思考开关、输出上限和超时。DeepSeek 与摘要相同：关闭思考，4096。
    """
    settings = load_settings().get("llm") or {}
    model = str(settings.get("qa_model") or "").strip() or None
    client = LLMClient(model=model)
    if "qa_reasoning_effort" in settings:
        client.reasoning_effort = settings.get("qa_reasoning_effort")
    if settings.get("qa_max_tokens"):
        client.max_tokens = int(settings["qa_max_tokens"])
    if settings.get("qa_request_timeout_sec"):
        client.request_timeout_sec = float(settings["qa_request_timeout_sec"])
    return client


def _bind_exchange(items: list[dict[str, Any]], ex: dict[str, Any]) -> list[dict[str, Any]]:
    for item in items:
        item["exchange_id"] = str(ex.get("exchange_id") or item.get("exchange_id") or "")
        if not item.get("analyst"):
            item["analyst"] = ex.get("analyst")
        if not item.get("firm"):
            item["firm"] = ex.get("firm")
        item["review_flags"] = [str(flag) for flag in item.get("review_flags") or [] if str(flag).strip()]
    return items


def _usable_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in items if not item.get("parse_failed")]


def _review_exchange(llm: LLMClient, ex: dict[str, Any], items: list[dict[str, Any]], notes: list[str]) -> list[dict[str, Any]]:
    """第二遍对照原文，改归属、误译和漏掉的回答。失败就留下第一遍并打标。"""
    payload = json.dumps(items, ensure_ascii=False)
    user = llm.load_prompt("review_qa.md") + "\n\n原文：\n" + (ex.get("text") or "") + "\n\n已有摘要：\n" + payload
    if notes:
        user += "\n\n规则检查：\n" + "\n".join(notes)
    try:
        reviewed = [model.model_dump() for model in llm.complete_json_list("review_qa.md", user, QAItem)]
    except Exception as exc:
        attach_flags(items[0], [f"审阅未完成：{_failure_reason(exc)}"])
        return items
    reviewed = _bind_exchange([item for item in reviewed if not _is_placeholder_qa(item)], ex)
    if not reviewed:
        attach_flags(items[0], ["审阅没有返回可用摘要，保留第一遍"])
        return items
    text = ex.get("text") or ""
    analyst = ex.get("analyst")
    needed = analyst_subquestion_count(text, analyst)
    if needed > 1 and len(reviewed) < needed and len(items) >= needed:
        attach_flags(items[0], ["审阅把多个问题并回去了，已保留拆开的版本"])
        return items
    before = set(missing_answer_facts(text, [item.get("answer_summary") or "" for item in items], analyst))
    after = set(missing_answer_facts(text, [item.get("answer_summary") or "" for item in reviewed], analyst))
    dropped = sorted(after - before)
    if dropped:
        label = "、".join(fact_label(fact) for fact in dropped)
        attach_flags(items[0], [f"审阅漏掉了原文中的数字或时间（{label}），已保留审阅前的版本"])
        return items
    return reviewed


def _flag_remaining(ex: dict[str, Any], items: list[dict[str, Any]]) -> list[str]:
    """数字、时间和明显误译在审阅之后仍然有问题，就写进 review_flags 并返回警告。"""
    warnings: list[str] = []
    usable = _usable_items(items)
    if not usable:
        return warnings
    text = ex.get("text") or ""
    analyst = ex.get("analyst")
    missing = missing_answer_facts(text, [item.get("answer_summary") or "" for item in usable], analyst)
    if missing:
        label = "、".join(fact_label(fact) for fact in missing)
        warning = f"问答摘要漏了原文中的数字或时间：{label}（exchange {ex.get('exchange_id')}）"
        warnings.append(warning)
        attach_flags(usable[0], [warning])
    needed = analyst_subquestion_count(text, analyst)
    if needed > 1 and len(usable) < needed:
        warning = f"这一轮至少有 {needed} 个问题，摘要只有 {len(usable)} 条（exchange {ex.get('exchange_id')}）"
        warnings.append(warning)
        attach_flags(usable[0], [warning])
    for item in usable:
        flags = false_friend_flags(text, item.get("answer_summary") or "")
        if flags:
            attach_flags(item, flags)
            warnings.extend(f"{flag}（exchange {ex.get('exchange_id')}）" for flag in flags)
    return warnings


def _gap_notes(ex: dict[str, Any], items: list[dict[str, Any]]) -> list[str]:
    """这一轮还没拆开、或摘要还漏数字时，写成给模型的核对说明。"""
    usable = _usable_items(items)
    if not usable:
        return []
    text = ex.get("text") or ""
    analyst = ex.get("analyst")
    notes: list[str] = []
    needed = analyst_subquestion_count(text, analyst)
    if needed > 1 and len(usable) < needed:
        notes.append(
            f"这一轮分析师提了至少 {needed} 个互不相关的问题，现在只有 {len(usable)} 条。"
            "必须拆开。每条只放回答该问题的发言，第二问里先开口的那个人的整段回答不能丢。"
        )
    missing = missing_answer_facts(text, [item.get("answer_summary") or "" for item in usable], analyst)
    if missing:
        notes.append(
            "摘要漏了原文回答中的这些数字或时间："
            + "、".join(fact_label(fact) for fact in missing)
            + "。请补进对应那一条，不要改口径，也不要编造。"
        )
    return notes


def _structure_exchange(llm: LLMClient, prompt_tmpl: str, ex: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """先概括。问题被合并或数字被漏掉时重写一次，再对照原文审阅。"""
    items = _bind_exchange(_structure_batch(llm, prompt_tmpl, [ex]), ex)
    if not _usable_items(items):
        return items, []
    notes = _gap_notes(ex, items)
    if notes:
        reminded = prompt_tmpl + "\n\n请按下面的核对重写这一轮，不要把问题并回去：\n" + "\n".join(notes)
        retried = _bind_exchange(_structure_batch(llm, reminded, [ex], split=False), ex)
        if _usable_items(retried):
            items = retried
            # 审阅要看重写之后还缺什么，而不是第一遍的缺口
            notes = _gap_notes(ex, items)
    items = _review_exchange(llm, ex, _usable_items(items) or items, notes)
    return items, _flag_remaining(ex, items)


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
    llm = _qa_client()
    prompt_tmpl = _prompt(llm, press_release_numbers)
    fresh: list[dict[str, Any]] = []
    for ex in exchanges:
        got, _warnings = _structure_exchange(llm, prompt_tmpl, ex)
        fresh.extend(got)
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


def _is_placeholder_qa(item: dict[str, Any]) -> bool:
    """模型按旧提示用「文本不足」占位时丢掉，不在页面上显示空轮次。"""
    question = (item.get("question_summary") or "").strip()
    answer = (item.get("answer_summary") or "").strip()
    return question == "文本不足" or answer == "文本不足"


def _exchange_worth_structuring(ex: dict[str, Any]) -> bool:
    """没有分析师、也没有问号的结尾寒暄不送给模型。"""
    text = ex.get("text") or ""
    if (ex.get("analyst") or "").strip():
        return True
    return "?" in text or "？" in text


def structure_qa(
    transcript: str,
    press_release_numbers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prepared, qa = split_prepared_and_qa(transcript)
    exchanges = [ex for ex in (_split_exchanges(qa) if qa else []) if _exchange_worth_structuring(ex)]
    llm = _qa_client()
    prompt_tmpl = _prompt(llm, press_release_numbers)
    items: list[dict[str, Any]] = []
    fact_warnings: list[str] = []
    # 一轮一批。两轮塞进同一次输出时，模型会把第二个问题并掉或为了长度删回答。
    for index, ex in enumerate(exchanges, start=1):
        print(f"[stage2/qa] exchange {index}/{len(exchanges)} id={ex.get('exchange_id')}", flush=True)
        got, warnings = _structure_exchange(llm, prompt_tmpl, ex)
        items.extend(got)
        fact_warnings.extend(warnings)
    items = [item for item in items if not _is_placeholder_qa(item)]
    stats = compute_topic_stats(items)
    return {
        "prepared_remarks": prepared,
        "qa_text": qa,
        "items": items,
        "fact_warnings": fact_warnings,
        **stats,
    }
