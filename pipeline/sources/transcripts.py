from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.sources.sec_edgar import SecEdgarClient


def load_manual_transcript(raw_dir: Path) -> str | None:
    path = raw_dir / "transcript.txt"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def fetch_transcript(
    ticker_cfg: dict[str, Any],
    raw_dir: Path,
    *,
    sources: list[str] | None = None,
) -> tuple[str | None, str | None]:
    """按配置顺序尝试获取文字稿，返回 (text, source_name)。"""
    order = sources or ticker_cfg.get("transcript_sources") or ["manual"]
    for src in order:
        if src == "manual":
            text = load_manual_transcript(raw_dir)
            if text:
                return text, "manual"
        elif src == "ir_page":
            # IR 抓取依赖站点结构，失败时跳过
            continue
        elif src == "motley_fool":
            continue
        elif src == "whisper":
            continue
    return None, None


def split_prepared_and_qa(transcript: str) -> tuple[str, str]:
    """启发式拆分 prepared remarks 与 Q&A。"""
    markers = [
        "Question-and-Answer Session",
        "QUESTION AND ANSWER",
        "Questions and Answers",
        "Q&A Session",
        "Operator:",
    ]
    lower = transcript
    cut = None
    for m in markers:
        idx = lower.find(m)
        if idx != -1:
            cut = idx if cut is None else min(cut, idx)
    # Prefer first analyst intro after "operator"
    if cut is None:
        return transcript, ""
    return transcript[:cut], transcript[cut:]
