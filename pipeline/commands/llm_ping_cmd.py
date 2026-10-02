from __future__ import annotations

import json
from typing import Any

from pipeline.config import openrouter_api_key
from pipeline.llm import (
    LLMClient,
    LLMError,
    fetch_openrouter_models,
    find_model_info,
    pricing_from_model_info,
    redact_secrets,
)


def llm_ping(model: str | None = None) -> dict[str, Any]:
    """校验模型 slug 存在，并做一次极小 JSON 调用。"""
    key = openrouter_api_key()
    if not key:
        raise LLMError(
            "缺少 OPENROUTER_API_KEY。该命令只能在已配置 GitHub Secret 的 Actions 中运行。"
        )
    client = LLMClient(model=model, use_cache=False, require_key=True)
    models = fetch_openrouter_models(key)
    info = find_model_info(client.model, models)
    if not info:
        raise LLMError(f"OpenRouter 上不存在模型 slug: {client.model}")
    pricing = pricing_from_model_info(info) or {}
    ping = client.ping()
    return {
        "ok": True,
        "model": client.model,
        "context_length": info.get("context_length"),
        "pricing": {
            "prompt_per_token_usd": pricing.get("prompt"),
            "completion_per_token_usd": pricing.get("completion"),
        },
        "ping": ping,
    }


def require_openrouter_key(*, context: str) -> str:
    key = openrouter_api_key()
    if not key:
        raise SystemExit(
            f"ERROR: 缺少 OPENROUTER_API_KEY（{context}）。"
            "请在仓库 Settings → Secrets and variables → Actions 中配置该 Secret。"
        )
    return key
