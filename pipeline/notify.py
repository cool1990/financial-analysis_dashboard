from __future__ import annotations

import os
from typing import Any

import httpx


def maybe_notify(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with httpx.Client(timeout=30) as client:
        client.post(url, json={"chat_id": chat_id, "text": text[:3500]})


def format_scorecard_text(ticker: str, period: str, scorecard: list[dict[str, Any]], url: str | None = None) -> str:
    lines = [f"{ticker} {period} Stage 完成"]
    for row in scorecard:
        lines.append(
            f"- {row.get('metric')}: actual={row.get('actual')} vs {row.get('benchmark')} → {row.get('verdict')}"
        )
    if url:
        lines.append(url)
    return "\n".join(lines)
