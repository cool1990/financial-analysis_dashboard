"""Stage2 行业展望。由 GitHub Actions 上的 DeepSeek 阅读电话会正文后写入。

页面只渲染这里的结果。不要在构建脚本里另写一版摘要。
"""

from __future__ import annotations

from typing import Any

from pipeline.llm import LLMClient
from pipeline.schemas import IndustryOutlookResult
from pipeline.sources.transcripts import split_prepared_and_qa

# 全局 max_tokens 是 4096，六题展开的中文会被截断。这一步单独放宽，推理仍关闭。
_OUTLOOK_MAX_TOKENS = 8192
_OUTLOOK_TIMEOUT_SEC = 150

_TOPIC_ORDER = ("外部环境", "需求变化", "供给变化", "竞争变化", "商业模式", "公司战略")


def _topic_key(title: str) -> str | None:
    for name in _TOPIC_ORDER:
        if title == name or title.startswith(name + "（") or title.startswith(name + "("):
            return name
    return None


def _piece(block: dict[str, Any]) -> dict[str, Any] | None:
    kind = str(block.get("type") or "p")
    if kind in {"ul", "ol"}:
        items = [str(item).strip() for item in (block.get("items") or []) if str(item).strip()]
        if items:
            return {"type": kind, "items": items}
        return None
    text = str(block.get("text") or "").strip()
    if text:
        return {"type": "p", "text": text}
    return None


def _blocks_of(item: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = [_piece(block) for block in (item.get("blocks") or []) if isinstance(block, dict)]
    kept = [block for block in blocks if block]
    if kept:
        return kept
    text = str(item.get("text") or "").strip()
    return [{"type": "p", "text": text}] if text else []


def _length(blocks: list[dict[str, Any]]) -> int:
    total = 0
    for block in blocks:
        total += len(block.get("text") or "")
        total += sum(len(item) for item in block.get("items") or [])
    return total


def normalize_industry_outlook(raw: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """只留六个题目，按固定顺序。同一题留更长的一版。空题目丢掉。"""
    best: dict[str, dict[str, Any]] = {}
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        key = _topic_key(title)
        if key is None:
            continue
        blocks = _blocks_of(item)
        if not blocks:
            continue
        current = best.get(key)
        if current is None or _length(blocks) > _length(current["blocks"]):
            best[key] = {"title": title, "blocks": blocks}
    return [best[name] for name in _TOPIC_ORDER if name in best]


def outlook_is_thin(topics: list[dict[str, Any]]) -> bool:
    """薄版：每题一句短话，或整篇短到放不进数字和因果。这种结果不写入页面。"""
    if not topics:
        return True
    lengths = [_length(topic.get("blocks") or []) for topic in topics]
    if not lengths or sum(lengths) < 400:
        return True
    return all(length < 80 for length in lengths)


def extract_industry_outlook(transcript: str, llm: LLMClient | None = None) -> list[dict[str, Any]]:
    """读电话会正文（并附上问答供 CEO 补细节）。调用方用 outlook_is_thin 决定是否写入。"""
    client = llm or LLMClient()
    prepared, qa = split_prepared_and_qa(transcript or "")
    body = (prepared or transcript or "").strip()
    if not body:
        return []
    prompt = client.load_prompt("extract_industry_outlook.md")
    user = prompt + "\n\n----\n电话会正文（prepared remarks）：\n" + body[:50000]
    qa_text = (qa or "").strip()
    if qa_text:
        user += (
            "\n\n----\n问答。只在 CEO 展开行业、需求、供给、竞争、商业模式或战略时使用。"
            "不要用 CFO 的一句财务口径填某个题目：\n" + qa_text[:40000]
        )
    result = client.complete_json(
        "extract_industry_outlook.md",
        user,
        IndustryOutlookResult,
        max_tokens=_OUTLOOK_MAX_TOKENS,
        timeout_sec=_OUTLOOK_TIMEOUT_SEC,
    )
    return normalize_industry_outlook([topic.model_dump() for topic in result.topics])
