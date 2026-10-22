"""Stage 2 完成后，去掉已经被这次运行推翻的 Stage 1 提示。"""

from __future__ import annotations

from typing import Any


def _stale_llm_warning(text: str) -> bool:
    return (
        "没有调用模型" in text
        or "OPENROUTER_API_KEY" in text
        or "仍待 Stage 2" in text
        or "仍待 Stage2" in text
    )


def _stale_alpha_vantage_warning(text: str) -> bool:
    """只去掉「没调用 / 没 key」这一类。方法论里提到 Alpha Vantage 的句子留下。"""
    if "Alpha Vantage" not in text and "ALPHAVANTAGE" not in text:
        return False
    return "没有调用" in text or "没有 API key" in text or "没有 key" in text


def alpha_vantage_warning(record: dict[str, Any] | None) -> str | None:
    """按这一次有没有真的调用 Alpha Vantage 来写提示。调用成功就不留这条。"""
    if not record:
        return None
    called = bool(record.get("called"))
    status = str(record.get("status") or "")
    reason = str(record.get("reason") or "").strip()
    if called and status == "ok":
        return None
    if called and status == "error":
        return f"Alpha Vantage EARNINGS_ESTIMATES 已调用但失败：{reason or '未知错误'}。"
    if not called:
        return f"Alpha Vantage EARNINGS_ESTIMATES 没有调用：{reason or '没有 API key'}。"
    return None


def reconcile_stage2_warnings(
    warnings: list[Any] | None,
    *,
    qa_model_called: bool,
    alpha_vantage: dict[str, Any] | None = None,
) -> list[str]:
    """问答模型已经跑过，就去掉「没调用模型 / 没有 OPENROUTER」；
    传入了本轮 Alpha Vantage 结果，就换成跟这次调用一致的那一句。
    """
    kept: list[str] = []
    seen: set[str] = set()
    for item in warnings or []:
        text = str(item).strip()
        if not text or text in seen:
            continue
        if qa_model_called and _stale_llm_warning(text):
            continue
        if alpha_vantage is not None and _stale_alpha_vantage_warning(text):
            continue
        seen.add(text)
        kept.append(text)
    if alpha_vantage is not None:
        note = alpha_vantage_warning(alpha_vantage)
        if note and note not in seen:
            kept.append(note)
    return kept
