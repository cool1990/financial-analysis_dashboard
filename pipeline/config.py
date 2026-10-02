from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from pipeline import ROOT


def repo_path(*parts: str) -> Path:
    return ROOT.joinpath(*parts)


@lru_cache(maxsize=1)
def load_settings() -> dict[str, Any]:
    path = repo_path("config", "settings.yaml")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def load_ticker_config(ticker: str) -> dict[str, Any]:
    path = repo_path("config", "tickers", f"{ticker.upper()}.yaml")
    if not path.exists():
        raise FileNotFoundError(f"缺少股票配置: {path}")
    return load_yaml(path)


def save_ticker_config(ticker: str, data: dict[str, Any]) -> None:
    path = repo_path("config", "tickers", f"{ticker.upper()}.yaml")
    save_yaml(path, data)


def load_theses(ticker: str) -> dict[str, Any] | None:
    """config/theses/{TICKER}.yaml：投资论点（可选）。没有文件时返回 None。"""
    path = repo_path("config", "theses", f"{ticker.upper()}.yaml")
    if not path.exists():
        return None
    data = load_yaml(path) or {}
    return data if data.get("theses") else None


def list_tickers() -> list[str]:
    folder = repo_path("config", "tickers")
    return sorted(p.stem for p in folder.glob("*.yaml"))


def load_guidance_keys() -> list[str]:
    data = load_yaml(repo_path("config", "guidance_keys.yaml"))
    return list(data.get("keys", []))


def load_topics() -> dict[str, Any]:
    return load_yaml(repo_path("config", "topics.yaml"))


def data_dir(ticker: str | None = None) -> Path:
    base = repo_path(load_settings()["paths"]["data"])
    return base / ticker.upper() if ticker else base


def site_dir() -> Path:
    return repo_path(load_settings()["paths"]["site"])


def cache_dir() -> Path:
    return repo_path(load_settings()["paths"]["cache"])


def logs_dir() -> Path:
    return repo_path(load_settings()["paths"]["logs"])


def prompts_dir() -> Path:
    return repo_path(load_settings()["paths"]["prompts"])


def sec_user_agent() -> str:
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        # 本地开发兜底；Actions 必须配置 Secret
        ua = "stock_investing_dashboard raycao2023@gmail.com"
    return ua


def openrouter_api_key() -> str | None:
    return os.environ.get("OPENROUTER_API_KEY") or None
