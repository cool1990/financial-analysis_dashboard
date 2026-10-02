from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline.sources.ir_transcript import fetch_ir_transcript
from pipeline.sources.motley_fool import fetch_motley_fool_transcript


def load_manual_transcript(raw_dir: Path) -> str | None:
    path = raw_dir / "transcript.txt"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def save_transcript(
    raw_dir: Path,
    text: str,
    *,
    source: str,
    source_url: str | None = None,
) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / "transcript.txt"
    path.write_text(text, encoding="utf-8")
    meta = {
        "source": source,
        "source_url": source_url,
        "chars": len(text),
    }
    (raw_dir / "transcript_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def fetch_transcript(
    ticker_cfg: dict[str, Any],
    raw_dir: Path,
    *,
    sources: list[str] | None = None,
    fiscal_period: str | None = None,
    release_at: str | None = None,
) -> tuple[str | None, str | None]:
    """按配置顺序尝试获取文字稿，返回 (text, source_name)。

    成功时会写入 raw_dir/transcript.txt（gitignore，仅供本机/Actions 作业内使用）。
    """
    order = sources or ticker_cfg.get("transcript_sources") or [
        "motley_fool",
        "ir_page",
        "manual",
    ]
    period = fiscal_period or ""

    for src in order:
        if src == "manual":
            text = load_manual_transcript(raw_dir)
            if text:
                return text, "manual"
        elif src == "motley_fool":
            if not period:
                continue
            try:
                text, url = fetch_motley_fool_transcript(
                    ticker_cfg, fiscal_period=period, release_at=release_at
                )
            except Exception:
                text, url = None, None
            if text:
                save_transcript(raw_dir, text, source="motley_fool", source_url=url)
                return text, "motley_fool"
        elif src in {"ir_page", "ir"}:
            if not period:
                continue
            try:
                text, url = fetch_ir_transcript(ticker_cfg, fiscal_period=period)
            except Exception:
                text, url = None, None
            if text:
                save_transcript(raw_dir, text, source="ir_page", source_url=url)
                return text, "ir_page"
        elif src == "whisper":
            # 音频转写仍未启用
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
    if cut is None:
        return transcript, ""
    return transcript[:cut], transcript[cut:]
