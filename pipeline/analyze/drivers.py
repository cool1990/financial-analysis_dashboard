from __future__ import annotations

import json
from typing import Any

from pipeline.llm import LLMClient
from pipeline.schemas import DriversResult
from pipeline.validate import fuzzy_quote_ok

DRIVER_QUOTE_WARNING_PREFIX = "driver quote 未匹配:"


def strip_driver_warnings(warnings: list[str]) -> list[str]:
    """重跑 drivers 前去掉上一轮的引文告警，避免 Stage1 的告警残留到 Stage2。"""
    return [w for w in warnings if not str(w).startswith(DRIVER_QUOTE_WARNING_PREFIX)]


def analyze_drivers(
    metrics_table: dict[str, Any],
    press_text: str,
    prepared_remarks: str = "",
    qa_text: str = "",
    *,
    stage: int = 1,
) -> dict[str, Any]:
    llm = LLMClient()
    prompt = llm.load_prompt("analyze_drivers.md")
    user = (
        prompt
        + "\n\n指标表:\n"
        + json.dumps(metrics_table, ensure_ascii=False)
        + "\n\n新闻稿:\n"
        + press_text[:40000]
        + "\n\n管理层发言:\n"
        + (prepared_remarks[:15000] or "（未提供，source 不得填 prepared_remarks）")
        + "\n\nQ&A:\n"
        + (qa_text[:15000] or "（未提供，source 不得填 qa）")
    )
    result = llm.complete_json("analyze_drivers.md", user, DriversResult)
    data = result.model_dump()
    corpus = {
        "press_release": press_text,
        "prepared_remarks": prepared_remarks,
        "qa": qa_text,
    }
    unmatched: dict[str, int] = {}
    for metric in data.get("metrics", []):
        for drv in metric.get("drivers", []):
            quote = drv.get("evidence_quote")
            if not quote:
                continue
            src = drv.get("source") or "press_release"
            if fuzzy_quote_ok(quote, corpus.get(src) or ""):
                continue
            # LLM 常把 source 标错（如 Stage1 只有新闻稿却标 qa），在其它材料里找到则纠正来源
            found = next((k for k, txt in corpus.items() if k != src and fuzzy_quote_ok(quote, txt)), None)
            if found:
                drv["source"] = found
                continue
            drv["is_inference"] = True
            drv["quote_unverified"] = True
            name = metric.get("metric") or "?"
            unmatched[name] = unmatched.get(name, 0) + 1
    warnings: list[str] = []
    if unmatched:
        detail = "、".join(f"{k}×{v}" if v > 1 else k for k, v in unmatched.items())
        warnings.append(
            f"{DRIVER_QUOTE_WARNING_PREFIX} {sum(unmatched.values())} 条引文未在原文中找到，已标为推测（{detail}）"
        )
    return {"stage": stage, "metrics": data.get("metrics", []), "warnings": warnings}
