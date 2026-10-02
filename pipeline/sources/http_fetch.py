from __future__ import annotations

import time
from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from pipeline.config import sec_user_agent


class FetchError(RuntimeError):
    pass


_DEFAULT_UA = (
    "Mozilla/5.0 (compatible; financial-analysis-dashboard/1.0; "
    "+https://github.com/cool1990/financial-analysis_dashboard)"
)


def build_session(*, user_agent: str | None = None) -> requests.Session:
    session = requests.Session()
    ua = user_agent or sec_user_agent()
    # SEC_USER_AGENT 形如 "Name email"；对公开网页再用兼容 UA，避免部分站点拒非浏览器 UA
    if not ua or "example.com" in ua or "@" in ua and "Mozilla" not in ua:
        ua = _DEFAULT_UA
    session.headers.update(
        {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/pdf,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


@retry(
    retry=retry_if_exception_type((FetchError, requests.RequestException)),
    wait=wait_exponential(multiplier=1, min=1, max=20),
    stop=stop_after_attempt(3),
    reraise=True,
)
def http_get(
    url: str,
    *,
    session: requests.Session | None = None,
    timeout: float = 45.0,
    min_interval: float = 0.4,
) -> requests.Response:
    sess = session or build_session()
    time.sleep(min_interval)
    resp = sess.get(url, timeout=timeout, allow_redirects=True)
    if resp.status_code in {403, 429} or resp.status_code >= 500:
        raise FetchError(f"HTTP {resp.status_code} for {url}")
    if resp.status_code == 404:
        return resp
    resp.raise_for_status()
    return resp


def http_get_text(url: str, **kwargs: Any) -> tuple[int, str, str]:
    """返回 (status, final_url, text)。"""
    resp = http_get(url, **kwargs)
    return resp.status_code, str(resp.url), resp.text


def http_get_bytes(url: str, **kwargs: Any) -> tuple[int, str, bytes, str]:
    """返回 (status, final_url, content, content_type)。"""
    resp = http_get(url, **kwargs)
    return resp.status_code, str(resp.url), resp.content, resp.headers.get("Content-Type", "")
