from __future__ import annotations

import json
from typing import Any

from pipeline.config import load_settings
from pipeline.llm import LLMClient, LLMTruncatedError
from pipeline.schemas import DriversResult
from pipeline.validate import fuzzy_quote_ok

DRIVER_QUOTE_WARNING_PREFIX = "driver quote 未匹配:"
# 全局 max_tokens 是 4096。整张指标表的中文原因在长电话会上会写穿这个上限。
_DRIVERS_MAX_TOKENS = 16384
_DRIVERS_TIMEOUT_SEC = 240


def _drivers_budget() -> tuple[int, float]:
    settings = load_settings().get("llm") or {}
    tokens = int(settings.get("drivers_max_tokens") or _DRIVERS_MAX_TOKENS)
    timeout = float(settings.get("drivers_request_timeout_sec") or _DRIVERS_TIMEOUT_SEC)
    return max(tokens, 1), max(timeout, 1.0)


def _drivers_user(
    prompt: str,
    metrics_table: dict[str, Any],
    press_text: str,
    prepared_remarks: str,
    qa_text: str,
) -> str:
    return (
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


def _split_metric_table(table: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    keys = list(table.keys())
    if len(keys) < 2:
        return None
    mid = (len(keys) + 1) // 2
    return {k: table[k] for k in keys[:mid]}, {k: table[k] for k in keys[mid:]}


def _complete_drivers(
    llm: LLMClient,
    prompt: str,
    metrics_table: dict[str, Any],
    press_text: str,
    prepared_remarks: str,
    qa_text: str,
    *,
    nested: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """一次要完整 JSON。被截断就按指标对半拆开再合并，不把半截结果写入。"""
    tokens, timeout = _drivers_budget()
    user = _drivers_user(prompt, metrics_table, press_text, prepared_remarks, qa_text)
    try:
        result = llm.complete_json(
            "analyze_drivers.md",
            user,
            DriversResult,
            max_tokens=tokens,
            timeout_sec=timeout,
        )
        return result.model_dump(), []
    except LLMTruncatedError:
        parts = _split_metric_table(metrics_table)
        if parts is None:
            raise
        merged: list[dict[str, Any]] = []
        warnings: list[str] = []
        for part in parts:
            data, extra = _complete_drivers(
                llm,
                prompt,
                part,
                press_text,
                prepared_remarks,
                qa_text,
                nested=True,
            )
            merged.extend(data.get("metrics") or [])
            warnings.extend(extra)
        if not nested:
            warnings.insert(0, "变动原因单次输出被截断，已按指标拆成多次完整 JSON 后合并")
        return {"metrics": merged}, warnings


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
    data, warnings = _complete_drivers(llm, prompt, metrics_table, press_text, prepared_remarks, qa_text)
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
    if unmatched:
        detail = "、".join(f"{k}×{v}" if v > 1 else k for k, v in unmatched.items())
        warnings.append(
            f"{DRIVER_QUOTE_WARNING_PREFIX} {sum(unmatched.values())} 条引文未在原文中找到，已标为推测（{detail}）"
        )
    return {"stage": stage, "metrics": data.get("metrics", []), "warnings": warnings}
