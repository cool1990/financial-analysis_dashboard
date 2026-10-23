"""披露易 hkex-poll：用截下来的 JSON / HTML 解析，不访问港交所。"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import date
from pathlib import Path

import pytest
import requests

from pipeline import ROOT
from pipeline.commands.hkex_poll_cmd import (
    hkex_poll,
    resolve_window,
    stock_code_from_config,
    store_pdf,
)
from pipeline.config import load_ticker_config
from pipeline.sources.hkex_news import (
    HkexError,
    HkexNewsClient,
    announcements_from_rows,
    classify_results,
    parse_hkex_datetime,
    parse_prefix_body,
    parse_title_search_html,
    parse_title_search_payload,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hkex"
PREFIX = (FIXTURES / "prefix_00700.js").read_bytes()
SEARCH = (FIXTURES / "title_search_0700.json").read_bytes()
HTML = (FIXTURES / "title_search_0700.html").read_bytes()
BLOCKED = (FIXTURES / "blocked.html").read_bytes()
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
Q1_URL = "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0513/2026051300335_c.pdf"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _client(hook, **kwargs) -> HkexNewsClient:
    kwargs.setdefault("min_interval", 0)
    kwargs.setdefault("sleep", lambda _seconds: None)
    return HkexNewsClient(hook=hook, **kwargs)


def _ok_hook(url, params, headers):
    assert "financial-analysis-dashboard/hkex-poll" in headers["User-Agent"]
    assert "github.com/cool1990/financial-analysis_dashboard" in headers["User-Agent"]
    assert headers["Referer"].startswith("https://www1.hkexnews.hk/search/titlesearch.xhtml")
    if url.endswith("/search/prefix.do"):
        return 200, {"content-type": "application/javascript;charset=utf-8"}, PREFIX, url
    if "titleSearchServlet.do" in url:
        return 200, {"content-type": "application/json;charset=utf-8"}, SEARCH, url
    if url.endswith(".pdf"):
        return 200, {"content-type": "application/pdf"}, PDF, url
    raise AssertionError(url)


def test_prefix_and_datetime_use_hong_kong_day_first():
    stock = parse_prefix_body(PREFIX.decode(), stock_code="00700")
    assert stock == {"stock_id": 7609, "stock_code": "00700", "stock_name": "騰訊控股"}
    assert parse_hkex_datetime("13/05/2026 16:31") == "2026-05-13T16:31:00+08:00"
    with pytest.raises(HkexError, match="无法解析"):
        parse_hkex_datetime("32/13/2026")


def test_json_fixture_keeps_results_and_drops_noise():
    payload = json.loads(SEARCH)
    rows, meta = parse_title_search_payload(payload)
    assert meta["has_next"] is False
    assert len(rows) == 8
    items, skipped, warnings = announcements_from_rows(rows, stock_code="00700")
    assert warnings == []
    assert skipped == 4
    assert [item["doc_type"] for item in items] == [
        "interim_results",
        "quarterly_results",
        "annual_results",
        "quarterly_results",
    ]
    quarterly = items[1]
    assert quarterly["published_at"] == "2026-05-13T16:31:00+08:00"
    assert quarterly["url"] == Q1_URL
    assert quarterly["news_id"] == "12157227"
    assert quarterly["title"].startswith("截至二零二六年三月三十一日")
    annual = items[2]
    assert annual["url"].endswith("/2026031800389_c.pdf")
    assert "&#" not in annual["headline"]
    assert "末期業績 / 股息或分派" in annual["headline"]
    assert all("末期股息" not in item["title"] for item in items)
    assert all("中期報告" not in item["title"] for item in items)
    assert all("翌日披露" not in item["title"] for item in items)


def test_html_fixture_parses_the_same_results_table():
    rows = parse_title_search_html(HTML.decode())
    items, skipped, warnings = announcements_from_rows(rows, stock_code="00700")
    assert warnings == []
    assert skipped == 2
    assert [(item["doc_type"], item["news_id"]) for item in items] == [
        ("interim_results", "12280991"),
        ("quarterly_results", "12157227"),
        ("annual_results", "12056833"),
    ]
    assert items[1]["url"] == Q1_URL
    assert items[1]["published_at"] == "2026-05-13T16:31:00+08:00"


def test_classify_rejects_revisions_delays_and_accepts_english_quarterly():
    assert classify_results("公告及通告 - [延遲發表業績公告]", "延遲發表二零二六年第一季業績公佈") is None
    assert classify_results("公告及通告 - [修訂已刊發初步業績的資料]", "修訂業績公佈") is None
    assert classify_results("公告及通告 - [其他-營運業績最新情況]", "營運業績最新情況") is None
    assert (
        classify_results(
            "Announcements and Notices - [Quarterly Results]",
            "Results Announcement for the Three Months Ended 31 March 2026",
        )
        == "quarterly_results"
    )


def test_window_stock_code_and_allowlist(monkeypatch):
    since, until = resolve_window(None, None, today=date(2026, 10, 23))
    assert (since, until) == (date(2025, 4, 23), date(2026, 10, 23))
    with pytest.raises(HkexError, match="晚于"):
        resolve_window("2026-10-01", "2026-01-01", today=date(2026, 10, 23))
    with pytest.raises(HkexError, match="3 年"):
        resolve_window("2020-01-01", "2026-10-23", today=date(2026, 10, 23))
    with pytest.raises(HkexError, match="YYYY-MM-DD"):
        resolve_window("13/05/2026", None, today=date(2026, 10, 23))
    cfg = load_ticker_config("0700.HK")
    assert stock_code_from_config(cfg, "0700.HK") == "00700"
    assert stock_code_from_config({"listing": "hk"}, "0700.HK") == "00700"
    with pytest.raises(HkexError, match="listing=hk"):
        hkex_poll("MU")
    with pytest.raises(HkexError, match="缺少股票配置"):
        hkex_poll("0388.HK")

    def boom(*_args, **_kwargs):
        raise AssertionError("不支持的代号不应访问披露易")

    monkeypatch.setattr(
        "pipeline.commands.hkex_poll_cmd.load_ticker_config",
        lambda _ticker: {"listing": "hk", "hkex": {"stock_code": "00388"}},
    )
    with pytest.raises(HkexError, match="只支持 0700.HK"):
        hkex_poll("0388.HK", client=HkexNewsClient(hook=boom, min_interval=0))


def test_dry_run_lists_tencent_results_without_writing(tmp_path):
    client = _client(_ok_hook)
    out = hkex_poll(
        "0700.hk",
        since="2025-01-01",
        until="2026-10-23",
        client=client,
        dest_dir=tmp_path,
    )
    assert out["ok"] is True
    assert out["ticker"] == "0700.HK"
    assert out["dry_run"] is True
    assert out["stock_id"] == 7609
    assert out["saved"] == 0
    assert out["matched"] == 4
    assert out["fetched"] == 8
    assert out["items"][1]["url"] == Q1_URL
    assert "sha256" not in out["items"][0]
    assert "path" not in out["items"][0]
    assert not any(tmp_path.iterdir())
    assert out["source"]["search_url"] == "https://www1.hkexnews.hk/search/titleSearchServlet.do"
    assert out["source"]["t1code"] == "10000"
    assert out["source"]["t2Gcode"] == "3"
    search = out["requests"][1]
    assert search["status"] == 200
    assert "stockId=7609" in search["url"]
    assert "fromDate=20250101" in search["url"]
    assert "toDate=20261023" in search["url"]
    assert "t1code=10000" in search["url"]
    assert "import-period" in out["import_hint"]
    narrowed = hkex_poll(
        "0700.HK",
        since="2026-05-01",
        until="2026-05-31",
        limit=20,
        client=client,
        dest_dir=tmp_path,
    )
    assert [item["news_id"] for item in narrowed["items"]] == ["12157227"]
    limited = hkex_poll(
        "0700.HK",
        since="2025-01-01",
        until="2026-10-23",
        limit=2,
        client=client,
        dest_dir=tmp_path,
    )
    assert limited["limit_applied"] is True
    assert [item["doc_type"] for item in limited["items"]] == ["interim_results", "quarterly_results"]


def test_pagination_replaces_pages_and_paces():
    clock = Clock()
    seen: list[str] = []

    def hook(url, params, headers):
        if url.endswith("/search/prefix.do"):
            return 200, {"content-type": "application/javascript"}, PREFIX, url
        seen.append(params["rowRange"])
        payload = json.loads(SEARCH)
        if params["rowRange"] == "100":
            rows = json.loads(payload["result"])[:1]
            payload["result"] = json.dumps(rows, ensure_ascii=False)
            payload["hasNextRow"] = True
            payload["loadedRecord"] = 1
            payload["recordCnt"] = 8
            return 200, {"content-type": "application/json"}, json.dumps(payload).encode(), url
        payload["hasNextRow"] = False
        return 200, {"content-type": "application/json"}, json.dumps(payload).encode(), url

    client = HkexNewsClient(hook=hook, min_interval=1.2, sleep=clock.sleep, monotonic=clock.monotonic)
    out = hkex_poll(
        "0700.HK",
        since="2025-01-01",
        until="2026-10-23",
        client=client,
        today=date(2026, 10, 23),
    )
    assert seen == ["100", "200"]
    assert clock.slept == [1.2, 1.2]
    assert out["truncated"] is False
    assert out["matched"] == 4
    assert len({item["url"] for item in out["items"]}) == 4


def test_blocked_html_and_http_errors_are_diagnostic():
    def blocked(url, params, headers):
        if url.endswith("/search/prefix.do"):
            return 200, {"content-type": "application/javascript"}, PREFIX, url
        return 200, {"content-type": "text/html"}, BLOCKED, url

    client = _client(blocked)
    with pytest.raises(HkexError, match="没有返回 JSON") as caught:
        client.poll(stock_code="00700", since=date(2026, 1, 1), until=date(2026, 10, 23))
    payload = caught.value.to_dict()
    assert payload["ok"] is False
    assert payload["status"] == 200
    assert "Access Denied" in payload["snippet"]
    assert "反爬" in payload["hint"]
    assert "bm_s=" not in json.dumps(payload)

    clock = Clock()
    calls = {"n": 0}

    def denied(url, _params, _headers):
        calls["n"] += 1
        return 403, {"content-type": "text/html"}, BLOCKED, url

    client = HkexNewsClient(
        hook=denied,
        min_interval=0,
        max_attempts=2,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    with pytest.raises(HkexError, match="HTTP 403") as caught:
        client.poll(stock_code="00700", since=date(2026, 1, 1), until=date(2026, 10, 23))
    assert calls["n"] == 2
    assert clock.slept == [1.0]
    assert caught.value.attempts == 2
    assert "Akamai" in (caught.value.hint or "")
    assert "没有业绩" not in str(caught.value)

    def missing(url, _params, _headers):
        calls["missing"] = calls.get("missing", 0) + 1
        return 404, {"content-type": "text/html"}, b"missing", url

    client = _client(missing, max_attempts=3)
    with pytest.raises(HkexError, match="HTTP 404"):
        client.poll(stock_code="00700", since=date(2026, 1, 1), until=date(2026, 10, 23))
    assert calls["missing"] == 1


def test_download_writes_sha_and_skips_existing(tmp_path):
    hits = {"pdf": 0}

    def hook(url, params, headers):
        if url.endswith(".pdf"):
            hits["pdf"] += 1
            return 200, {"content-type": "application/pdf"}, PDF, url
        return _ok_hook(url, params, headers)

    client = _client(hook)
    out = hkex_poll(
        "0700.HK",
        since="2026-05-13",
        until="2026-05-13",
        download=True,
        client=client,
        dest_dir=tmp_path,
    )
    assert out["ok"] is True
    assert out["dry_run"] is False
    assert out["saved"] == 1
    item = out["items"][0]
    assert item["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert item["download"] == "saved"
    path = tmp_path / "2026-05-13_quarterly_results_12157227.pdf"
    assert path.read_bytes() == PDF
    assert item["path"].endswith(path.name)
    assert hits["pdf"] == 1

    again = hkex_poll(
        "0700.HK",
        since="2026-05-13",
        until="2026-05-13",
        download=True,
        client=client,
        dest_dir=tmp_path,
    )
    assert again["saved"] == 0
    assert again["items"][0]["download"] == "skipped_existing"
    assert again["items"][0]["sha256"] == item["sha256"]
    assert hits["pdf"] == 1


def test_download_rejects_html_redirect_and_oversize(tmp_path):
    def html_body(url, _params, _headers):
        if url.endswith(".pdf"):
            return 200, {"content-type": "text/html"}, b"<html>nope</html>", url
        return _ok_hook(url, None, _headers)

    client = _client(html_body)
    out = hkex_poll(
        "0700.HK",
        since="2026-05-13",
        until="2026-05-13",
        download=True,
        client=client,
        dest_dir=tmp_path,
    )
    assert out["ok"] is False
    assert "download_error" in out["items"][0]
    assert out["items"][0]["download_error"]["snippet"].startswith("<html>nope</html>")
    assert not list(tmp_path.glob("*.pdf"))

    def redirected(url, _params, _headers):
        if url.endswith(".pdf"):
            return 200, {"content-type": "application/pdf"}, PDF, "https://evil.example/x.pdf"
        return _ok_hook(url, None, _headers)

    out = hkex_poll(
        "0700.HK",
        since="2026-05-13",
        until="2026-05-13",
        download=True,
        client=_client(redirected),
        dest_dir=tmp_path,
    )
    assert out["ok"] is False
    assert "拒绝非披露易链接" in out["items"][0]["download_error"]["error"]
    assert not list(tmp_path.glob("*.pdf"))

    calls = {"n": 0}

    def huge(url, _params, _headers):
        calls["n"] += 1
        return 200, {"content-type": "application/pdf"}, b"%PDF" + b"x" * 40, url

    client = _client(huge, max_bytes=16)
    with pytest.raises(HkexError, match="超过"):
        client.download_pdf(Q1_URL)
    assert calls["n"] == 1

    with pytest.raises(HkexError, match="不是 PDF"):
        store_pdf(
            _client(_ok_hook),
            {
                "title": "网页",
                "published_at": "2026-05-13T16:31:00+08:00",
                "doc_type": "quarterly_results",
                "url": "https://www1.hkexnews.hk/listedco/a.htm",
                "file_type": "HTM",
                "news_id": "1",
            },
            tmp_path,
        )


def test_cli_dry_run_and_error_json(monkeypatch, capsys):
    captured = {}

    def fake(ticker, **kwargs):
        captured["ticker"] = ticker
        captured.update(kwargs)
        return {"ok": True, "dry_run": True, "items": []}

    import pipeline.commands.hkex_poll_cmd as mod
    from pipeline.__main__ import main

    monkeypatch.setattr(mod, "hkex_poll", fake)
    assert main(["hkex-poll", "--ticker", "0700.HK"]) == 0
    assert captured["ticker"] == "0700.HK"
    assert captured["download"] is False
    assert captured["limit"] == 20
    assert json.loads(capsys.readouterr().out)["dry_run"] is True

    def broken(*_args, **_kwargs):
        raise HkexError(
            "披露易返回 HTTP 403",
            url="https://www1.hkexnews.hk/search/titleSearchServlet.do",
            status=403,
            snippet="Access Denied",
        )

    monkeypatch.setattr(mod, "hkex_poll", broken)
    assert main(["hkex-poll", "--ticker", "0700.HK", "--download"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["status"] == 403
    assert payload["snippet"] == "Access Denied"


def test_poll_workflow_and_gitignore_leave_existing_pdfs():
    workflow = (ROOT / ".github" / "workflows" / "poll.yml").read_text(encoding="utf-8")
    assert "hkex-poll" not in workflow
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "data/0700.HK/raw/hkex/2026-05-13_quarterly_results_12157227.pdf"],
        cwd=ROOT,
        check=False,
    )
    assert ignored.returncode == 0
    kept = subprocess.run(
        ["git", "check-ignore", "-q", "data/0700.HK/raw/FY2026Q1/press_release.pdf"],
        cwd=ROOT,
        check=False,
    )
    assert kept.returncode == 1


def test_connection_error_retries_then_succeeds():
    clock = Clock()
    state = {"n": 0}

    def hook(url, _params, _headers):
        if url.endswith("/search/prefix.do"):
            return 200, {"content-type": "application/javascript"}, PREFIX, url
        state["n"] += 1
        if state["n"] < 3:
            raise requests.ConnectionError("timed out")
        return 200, {"content-type": "application/json"}, SEARCH, url

    client = HkexNewsClient(
        hook=hook,
        min_interval=0,
        max_attempts=3,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    found = client.poll(stock_code="00700", since=date(2026, 1, 1), until=date(2026, 10, 23))
    assert state["n"] == 3
    assert clock.slept == [1.0, 2.0]
    assert [item["news_id"] for item in found["items"]] == ["12280991", "12157227", "12056833"]
