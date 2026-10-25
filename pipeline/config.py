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


STAGES = {
    "观察": ("watch", "观察仓"), "观察仓": ("watch", "观察仓"),
    "等待": ("wait", "等待仓"), "等待仓": ("wait", "等待仓"),
    "持仓": ("hold", "持仓"), "持有": ("hold", "持仓"),
}


def _plain(value: Any) -> str:
    """配置里的展示字段。空值、占位「请填写」不当作已经填好的数。"""
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    if isinstance(value, float) and value.is_integer():
        text = str(int(value))
    elif isinstance(value, float):
        text = f"{value:g}"
    else:
        text = str(value).strip()
    if not text or "请填写" in text or text in {"—", "-", "null", "None"}:
        return ""
    return text


def load_position(ticker: str) -> dict[str, Any] | None:
    """config/theses/{TICKER}.yaml：仓位档案（阶段、论点、买卖条件、想搞清楚的问题）。

    文件用中文键方便手写，这里统一转成内部结构；没有文件时返回 None。
    """
    path = repo_path("config", "theses", f"{ticker.upper()}.yaml")
    if not path.exists():
        return None
    raw = load_yaml(path) or {}
    stage, label = STAGES.get(str(raw.get("阶段") or "观察").strip(), ("watch", "观察仓"))
    theses = [
        {"bull": str(t.get("看好") or ""), "bear": str(t.get("担心") or ""), "falsify": str(t.get("证伪") or "")}
        for t in raw.get("论点") or []
        if isinstance(t, dict)
    ]
    triggers = [{"name": str(k), "text": str(v)} for k, v in (raw.get("条件") or {}).items() if v]
    questions = [str(q) for q in raw.get("问题") or [] if q]
    text = path.read_text(encoding="utf-8")
    return {
        "stage": stage,
        "stage_label": label,
        "updated": str(raw.get("更新") or ""),
        "theses": theses,
        "triggers": triggers,
        "questions": questions,
        "group": _plain(raw.get("分组") or raw.get("group")),
        "kind": _plain(raw.get("类型") or raw.get("type") or raw.get("category")),
        "target_value": _plain(raw.get("目标估值") if "目标估值" in raw else raw.get("target_valuation")),
        # 文件里还有「草稿」「【请填写】」字样时，页面提示尚未定稿
        "draft": "草稿" in text.split("\n", 1)[0] or "【请填写】" in text,
    }


def list_tickers() -> list[str]:
    folder = repo_path("config", "tickers")
    return sorted(p.stem for p in folder.glob("*.yaml"))


def is_hk_listing(cfg: dict[str, Any] | None) -> bool:
    """港股走手工导入，不调用 SecEdgar。`0700.HK.yaml` 的 stem 仍是 `0700.HK`。"""
    if not cfg:
        return False
    return str(cfg.get("listing") or "").strip().lower() == "hk"


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


def alphavantage_api_key() -> str | None:
    from pipeline.sources.alpha_vantage import alphavantage_api_key as _key

    return _key()


@lru_cache(maxsize=1)
def load_pe_targets() -> dict[str, float]:
    """Hermes 股票池 pe_target → {TICKER: float}。空单元格不收录。

    权威文件：config/stock_pool.csv（从 /workspace/stock_pool/master_stock_pool.csv 同步，勿手填）。
    """
    import csv

    path = repo_path("config", "stock_pool.csv")
    if not path.exists():
        return {}
    out: dict[str, float] = {}
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            ticker = str(row.get("ticker") or "").strip().upper()
            raw = str(row.get("pe_target") or "").strip()
            if not ticker or not raw:
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            out[ticker] = value
    return out


def pe_target_for(ticker: str) -> float | None:
    """单票目标 Forward PE；没有配置返回 None。"""
    return load_pe_targets().get(ticker.upper())

