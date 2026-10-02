"""Whisper 转写占位：需要音频 URL 与 faster-whisper 时再启用。"""

from __future__ import annotations


def transcribe_audio(url: str, model_size: str = "small") -> str:
    raise NotImplementedError("whisper 转写未启用；请将文字稿放入 raw/{period}/transcript.txt")
