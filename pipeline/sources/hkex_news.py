"""港交所披露易标题搜索（试跑）。

入口（2026-10-23 对 00700 / 腾讯核对过）：

- 代号：``GET https://www1.hkexnews.hk/search/prefix.do``
  JSONP。``name=00700&market=SEHK`` 返回 ``stockId``。代码不写死 stockId。
- 公告：``GET https://www1.hkexnews.hk/search/titleSearchServlet.do``
  与标题搜索页 ``titlesearch.xhtml?lang=zh`` 相同的查询。
  ``t1code=10000``（公告及通告）、``t2Gcode=3``（財務資料）。
  响应是 JSON，``result`` 本身又是一段 JSON 字符串。
- PDF：``https://www1.hkexnews.hk`` + ``FILE_LINK``。

类别目录：``/ncms/script/eds/tierone_c.json``、``tiertwo_c.json``。
財務資料里还有董事会日期和股息表格，所以拉下来之后再按标题分类。

本模块只负责查询和解析。不写单季 JSON，不接进每天的 poll。
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
from datetime import date, datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests

HKEX_USER_AGENT = (
    "financial-analysis-dashboard/hkex-poll "
    "(+https://github.com/cool1990/financial-analysis_dashboard; research prototype)"
)
PREFIX_URL = "https://www1.hkexnews.hk/search/prefix.do"
SEARCH_URL = "https://www1.hkexnews.hk/search/titleSearchServlet.do"
PDF_ORIGIN = "https://www1.hkexnews.hk"
TITLE_SEARCH_PAGE = "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh"
ALLOWED_HOSTS = frozenset({"www1.hkexnews.hk", "www.hkexnews.hk"})
HK_TZ = ZoneInfo("Asia/Hong_Kong")

# 公告及通告 / 財務資料。季度、中期、末期业绩都在这一组，董事会日期和股息表格也在。
T1_ANNOUNCEMENTS = "10000"
T2G_FINANCIALS = "3"
PAGE_SIZE = 100
MAX_PAGES = 3
MAX_PDF_BYTES = 25 * 1024 * 1024
RETRYABLE_STATUS = frozenset({403, 429, 500, 502, 503, 504})

DOC_LABELS = {
    "quarterly_results": "季度業績",
    "interim_results": "中期業績",
    "annual_results": "末期業績",
}

_HARD_EXCLUDE = (
    "董事會召開日期",
    "翌日披露",
    "月報表",
    "股份購回",
    "年報",
    "半年度報告",
    "中期報告",
    "季度報告",
    "延遲發表",
    "附屬公司的業績",
    "修訂已刊發",
    "修改已刊發",
    "發行債務證券",
    "債券及結構性產品",
    "營運業績最新情況",
)


class HkexError(RuntimeError):
    """披露易查询失败。``to_dict`` 给 CLI 打成可诊断的 JSON。"""

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        status: int | None = None,
        content_type: str | None = None,
        snippet: str | None = None,
        attempts: int | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.status = status
        self.content_type = content_type
        self.snippet = snippet
        self.attempts = attempts
        self.hint = hint

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": False, "error": str(self)}
        for key in ("url", "status", "content_type", "snippet", "attempts", "hint"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


def _with_context(exc: HkexError, **kwargs: Any) -> HkexError:
    for key, value in kwargs.items():
        if getattr(exc, key) is None and value is not None:
            setattr(exc, key, value)
    return exc


def snippet(body: bytes | str, limit: int = 280) -> str:
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def plain(value: str | None) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_stock_code(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if not digits or len(digits) > 5:
        raise HkexError(f"无法识别港股代号: {value}")
    return digits.zfill(5)


def stock_codes(raw: str | None) -> set[str]:
    found: set[str] = set()
    for part in re.split(r"[\s,/]+", plain(raw)):
        digits = re.sub(r"\D", "", part)
        if not digits:
            continue
        found.add(digits.zfill(5) if len(digits) <= 5 else digits)
    return found


def source_info() -> dict[str, Any]:
    return {
        "prefix_url": PREFIX_URL,
        "search_url": SEARCH_URL,
        "search_page": TITLE_SEARCH_PAGE,
        "pdf_origin": PDF_ORIGIN,
        "market": "SEHK",
        "lang": "zh",
        "t1code": T1_ANNOUNCEMENTS,
        "t2Gcode": T2G_FINANCIALS,
        "t2code": "-2",
        "category": "公告及通告 / 財務資料（t1code=10000, t2Gcode=3）",
        "kept": "季度業績、中期業績、末期業績（标题为业绩公布或全年业绩公布）",
        "user_agent": HKEX_USER_AGENT,
    }


def _with_query(url: str, params: dict[str, str] | None) -> str:
    if not params:
        return url
    return url + "?" + urllib.parse.urlencode(params)


def check_hkex_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in ALLOWED_HOSTS:
        raise HkexError(
            f"拒绝非披露易链接: {url}",
            url=url,
            hint="只接受 https://www1.hkexnews.hk 或 https://www.hkexnews.hk 。",
        )


def absolute_pdf_url(link: str | None) -> str:
    text = html.unescape(str(link or "")).strip()
    if not text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    elif text.startswith("/"):
        text = PDF_ORIGIN + text
    parsed = urllib.parse.urlparse(text)
    cleaned = urllib.parse.urlunparse(parsed._replace(fragment=""))
    check_hkex_url(cleaned)
    return cleaned


def parse_hkex_datetime(value: str | None) -> str:
    text = plain(value)
    matched = re.search(r"(\d{2})/(\d{2})/(\d{4})(?:\s+(\d{2}):(\d{2}))?", text)
    if not matched:
        raise HkexError(f"无法解析披露易时间: {value}")
    day, month, year, hour, minute = matched.groups()
    try:
        parsed = datetime(
            int(year),
            int(month),
            int(day),
            int(hour or 0),
            int(minute or 0),
            tzinfo=HK_TZ,
        )
    except ValueError as exc:
        raise HkexError(f"无法解析披露易时间: {value}") from exc
    return parsed.isoformat(timespec="seconds")


def _match_kind(text: str) -> str | None:
    lowered = text.lower()
    if "季度業績" in text or "quarterly results" in lowered:
        return "quarterly_results"
    if "中期業績" in text or "interim results" in lowered:
        return "interim_results"
    if any(token in text for token in ("末期業績", "全年業績")) or any(
        token in lowered for token in ("final results", "annual results")
    ):
        return "annual_results"
    return None


def _looks_like_results_title(title: str) -> bool:
    if any(token in title for token in ("業績公佈", "業績公布", "全年業績", "中期業績", "季度業績")):
        return True
    if "results announcement" in title.lower():
        return True
    return bool(
        re.search(r"\bresults\b", title, re.I)
        and re.search(
            r"\b(three|six|nine|twelve)\s+months\b|\bannual\b|\binterim\b|\byear ended\b",
            title,
            re.I,
        )
    )


def classify_results(headline: str, title: str) -> str | None:
    """业绩公布返回 doc_type，其余公告返回 None。

    末期业绩的标题类别经常写成「末期業績 / 股息或分派 / …」。
    同一天另有一份「股息或分派（公告表格）」，标题是末期股息，不能当成业绩公布。
    """
    headline_text = plain(headline)
    title_text = plain(title)
    blob = f"{headline_text} {title_text}"
    if any(token in blob for token in _HARD_EXCLUDE):
        return None
    if "公告表格" in headline_text and "業績" not in title_text:
        return None
    if "股息或分派" in headline_text and not any(
        token in headline_text for token in ("季度業績", "中期業績", "末期業績")
    ):
        return None
    if "末期股息" in title_text and "業績" not in title_text:
        return None
    kind = _match_kind(headline_text) or _match_kind(title_text)
    if kind is None or not _looks_like_results_title(title_text):
        return None
    return kind


def parse_prefix_body(text: str, *, stock_code: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise HkexError("prefix.do 没有返回 JSONP", snippet=snippet(text))
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise HkexError("prefix.do 的 JSONP 无法解析", snippet=snippet(text)) from exc
    info = data.get("stockInfo") or []
    wanted = normalize_stock_code(stock_code)
    exact = [
        row
        for row in info
        if isinstance(row, dict) and normalize_stock_code(str(row.get("code") or "0")) == wanted
    ]
    if not exact:
        shown = [str(row.get("code")) for row in info[:8] if isinstance(row, dict)]
        raise HkexError(
            f"披露易找不到股份代号 {wanted}",
            snippet=snippet(text),
            hint=f"prefix.do 返回 {shown or '空列表'}。0700.HK 对应 00700，不是人民币柜台 80700。",
        )
    row = exact[0]
    if row.get("stockId") is None:
        raise HkexError(f"prefix.do 没有给 {wanted} 的 stockId", snippet=snippet(text))
    return {
        "stock_id": int(row["stockId"]),
        "stock_code": wanted,
        "stock_name": plain(str(row.get("name") or "")),
    }


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


def parse_title_search_payload(payload: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(payload, dict):
        raise HkexError("titleSearchServlet 的 JSON 不是对象")
    raw = payload.get("result")
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            rows: Any = []
        else:
            try:
                rows = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise HkexError(
                    "titleSearchServlet 的 result 字段不是 JSON 数组",
                    snippet=snippet(raw),
                ) from exc
    elif isinstance(raw, list):
        rows = raw
    elif raw is None:
        raise HkexError(
            "titleSearchServlet 响应缺少 result",
            snippet=snippet(json.dumps(payload, ensure_ascii=False)),
        )
    else:
        raise HkexError(f"titleSearchServlet 的 result 类型无法识别: {type(raw).__name__}")
    if not isinstance(rows, list):
        raise HkexError("titleSearchServlet 的 result 不是数组")
    meta = {
        "has_next": _as_bool(payload.get("hasNextRow")),
        "loaded": payload.get("loadedRecord"),
        "record_count": payload.get("recordCnt"),
        "row_range": payload.get("rowRange"),
        "from_html": False,
    }
    return rows, meta


def parse_title_search_html(page: str) -> list[dict[str, Any]]:
    """解析标题搜索结果表。标记和披露易 ``titlesearch.js`` 写出来的行一致。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(page, "lxml")
    rows: list[dict[str, Any]] = []
    for tr in soup.select("table tbody tr"):
        date_cell = tr.select_one("td.release-time")
        link = tr.select_one(".doc-link a[href]")
        if date_cell is None or link is None:
            continue
        headline = tr.select_one(".headline")
        code_cell = tr.select_one("td.stock-short-code")
        date_text = plain(date_cell.get_text(" ", strip=True))
        matched = re.search(r"\d{2}/\d{2}/\d{4}(?:\s+\d{2}:\d{2})?", date_text)
        href = str(link.get("href") or "")
        rows.append(
            {
                "DATE_TIME": matched.group(0) if matched else date_text,
                "TITLE": link.get_text(" ", strip=True),
                "FILE_LINK": href,
                "LONG_TEXT": headline.get_text(" ", strip=True) if headline else "",
                "STOCK_CODE": code_cell.get_text(" ", strip=True) if code_cell else "",
                "FILE_TYPE": "PDF" if href.lower().split("?", 1)[0].endswith(".pdf") else "",
                "NEWS_ID": str(link.get("data-news-id") or ""),
            }
        )
    return rows


def announcements_from_rows(
    rows: list[dict[str, Any]],
    *,
    stock_code: str,
) -> tuple[list[dict[str, Any]], int, list[str]]:
    wanted = normalize_stock_code(stock_code)
    items: list[dict[str, Any]] = []
    skipped = 0
    warnings: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            skipped += 1
            continue
        codes = stock_codes(str(row.get("STOCK_CODE") or ""))
        if codes and wanted not in codes:
            skipped += 1
            continue
        title = plain(str(row.get("TITLE") or ""))
        headline = plain(str(row.get("LONG_TEXT") or row.get("SHORT_TEXT") or ""))
        doc_type = classify_results(headline, title)
        if not doc_type:
            skipped += 1
            continue
        try:
            published = parse_hkex_datetime(str(row.get("DATE_TIME") or ""))
        except HkexError as exc:
            skipped += 1
            warnings.append(f"{title[:40] or '未命名公告'}: {exc}")
            continue
        try:
            url = absolute_pdf_url(str(row.get("FILE_LINK") or ""))
        except HkexError as exc:
            skipped += 1
            warnings.append(str(exc))
            continue
        if not url:
            skipped += 1
            warnings.append(f"{title[:40]} 没有文件链接")
            continue
        file_type = plain(str(row.get("FILE_TYPE") or ""))
        if not file_type and url.lower().split("?", 1)[0].endswith(".pdf"):
            file_type = "PDF"
        items.append(
            {
                "title": title,
                "published_at": published,
                "doc_type": doc_type,
                "doc_type_label": DOC_LABELS[doc_type],
                "url": url,
                "headline": headline,
                "news_id": str(row.get("NEWS_ID") or "").strip(),
                "file_type": file_type.upper(),
            }
        )
    items.sort(key=lambda item: item["published_at"], reverse=True)
    return items, skipped, warnings


def search_params(*, stock_id: int, since: date, until: date, row_range: int) -> dict[str, str]:
    return {
        "sortDir": "0",
        "sortByOptions": "DateTime",
        "category": "0",
        "market": "SEHK",
        "stockId": str(stock_id),
        "documentType": "-1",
        "fromDate": since.strftime("%Y%m%d"),
        "toDate": until.strftime("%Y%m%d"),
        "title": "",
        "searchType": "1",
        "t1code": T1_ANNOUNCEMENTS,
        "t2Gcode": T2G_FINANCIALS,
        "t2code": "-2",
        "rowRange": str(row_range),
        "lang": "zh",
    }


def _retry_hint(status: int) -> str:
    if status in {403, 429}:
        return (
            "可能遇到限速或 Akamai 拦截（站点会下发 bm_s cookie）。"
            "请保持默认间隔，不要并发，也不要把 User-Agent 改成伪装浏览器后连续重试。"
        )
    return "披露易服务端错误。已按指数退避重试。"


class HkexNewsClient:
    """串行访问披露易。默认每次请求至少间隔 1.2 秒，失败最多 3 次。"""

    def __init__(
        self,
        *,
        hook: Callable[..., tuple[int, dict[str, str], bytes, str]] | None = None,
        session: requests.Session | None = None,
        min_interval: float = 1.2,
        max_attempts: int = 3,
        timeout: float = 45.0,
        max_bytes: int = MAX_PDF_BYTES,
        user_agent: str = HKEX_USER_AGENT,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._hook = hook
        self._session = session
        self.min_interval = min_interval
        self.max_attempts = max_attempts
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.user_agent = user_agent
        self._sleep = sleep
        self._monotonic = monotonic
        self._last: float | None = None

    def build_headers(self, *, accept: str | None = None) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": accept or "application/json, text/javascript, text/html;q=0.8, */*;q=0.1",
            "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
            "Referer": TITLE_SEARCH_PAGE,
        }

    def _session_get(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
        return self._session

    def _pace(self) -> None:
        now = self._monotonic()
        if self._last is not None and self.min_interval > 0:
            wait = self.min_interval - (now - self._last)
            if wait > 0:
                self._sleep(wait)
                now = self._monotonic()
        self._last = now

    def _once(
        self,
        url: str,
        params: dict[str, str] | None,
    ) -> tuple[int, dict[str, str], bytes, str]:
        accept = None
        if params is None and url.lower().split("?", 1)[0].endswith(".pdf"):
            accept = "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8"
        headers = self.build_headers(accept=accept)
        if self._hook is not None:
            return self._hook(url, params, headers)
        resp = self._session_get().get(
            url,
            params=params,
            headers=headers,
            timeout=self.timeout,
            allow_redirects=True,
            stream=True,
        )
        try:
            buf = bytearray()
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                if len(buf) + len(chunk) > self.max_bytes:
                    raise HkexError(
                        f"响应超过 {self.max_bytes} 字节，已中止",
                        url=str(resp.url),
                        status=resp.status_code,
                        content_type=resp.headers.get("Content-Type", ""),
                        hint="业绩公告 PDF 不应超过 25MB。年报整本不在这个命令的下载范围内。",
                    )
                buf.extend(chunk)
            header_map = {key.lower(): value for key, value in resp.headers.items()}
            return resp.status_code, header_map, bytes(buf), str(resp.url)
        finally:
            resp.close()

    def _send(
        self,
        url: str,
        params: dict[str, str] | None,
    ) -> tuple[int, dict[str, str], bytes, str]:
        full = _with_query(url, params)
        last: HkexError | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._pace()
            try:
                status, headers, body, final = self._once(url, params)
            except HkexError:
                raise
            except (requests.RequestException, OSError) as exc:
                last = HkexError(
                    f"连接披露易失败: {exc.__class__.__name__}",
                    url=full,
                    attempts=attempt,
                    hint="请检查网络后重试。本命令串行请求，不要并发跑多只股票。",
                )
                if attempt == self.max_attempts:
                    break
                self._sleep(min(8.0, float(2 ** (attempt - 1))))
                continue
            ctype = headers.get("content-type", "")
            if status < 400 and len(body) > self.max_bytes:
                raise HkexError(
                    f"响应超过 {self.max_bytes} 字节，已丢弃",
                    url=final or full,
                    status=status,
                    content_type=ctype,
                )
            if status in RETRYABLE_STATUS:
                last = HkexError(
                    f"披露易返回 HTTP {status}",
                    url=final or full,
                    status=status,
                    content_type=ctype,
                    snippet=snippet(body),
                    attempts=attempt,
                    hint=_retry_hint(status),
                )
                if attempt == self.max_attempts:
                    break
                self._sleep(min(8.0, float(2 ** (attempt - 1))))
                continue
            if status >= 400:
                raise HkexError(
                    f"披露易返回 HTTP {status}",
                    url=final or full,
                    status=status,
                    content_type=ctype,
                    snippet=snippet(body),
                    attempts=attempt,
                    hint="若是 404，标题搜索入口可能已变更。",
                )
            return status, headers, body, final or full
        assert last is not None
        raise last

    def _decode_prefix(self, status: int, headers: dict[str, str], body: bytes, url: str, *, stock_code: str) -> dict[str, Any]:
        ctype = headers.get("content-type", "")
        text = body.decode("utf-8", "replace")
        if text.lstrip().startswith("<"):
            raise HkexError(
                "prefix.do 返回了 HTML 而不是 JSONP",
                url=url,
                status=status,
                content_type=ctype,
                snippet=snippet(text),
                hint="正常应是 callback({...})。HTML 多半是拦截页或入口已变更。",
            )
        try:
            return parse_prefix_body(text, stock_code=stock_code)
        except HkexError as exc:
            raise _with_context(exc, url=url, status=status, content_type=ctype) from exc

    def _decode_search(
        self,
        status: int,
        headers: dict[str, str],
        body: bytes,
        url: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        ctype = headers.get("content-type", "")
        text = body.decode("utf-8", "replace")
        stripped = text.lstrip()
        looks_json = "json" in ctype or stripped.startswith("{")
        if looks_json:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise HkexError(
                    "titleSearchServlet 返回的内容不是合法 JSON",
                    url=url,
                    status=status,
                    content_type=ctype,
                    snippet=snippet(text),
                    hint='正常响应是 {"result": "[...]", "hasNextRow": ...}。入口可能已变更。',
                ) from exc
            try:
                return parse_title_search_payload(payload)
            except HkexError as exc:
                raise _with_context(exc, url=url, status=status, content_type=ctype) from exc
        rows = parse_title_search_html(text)
        if rows:
            return rows, {
                "has_next": False,
                "loaded": len(rows),
                "record_count": len(rows),
                "row_range": None,
                "from_html": True,
            }
        raise HkexError(
            "titleSearchServlet 没有返回 JSON，页面里也没有公告表格",
            url=url,
            status=status,
            content_type=ctype,
            snippet=snippet(text),
            hint=(
                "正常入口是 GET https://www1.hkexnews.hk/search/titleSearchServlet.do 。"
                "若 snippet 含 Access Denied、bm-verify 或 relocated，是反爬或 URL 失效，不是这只股票没有业绩。"
            ),
        )

    def poll(self, *, stock_code: str, since: date, until: date) -> dict[str, Any]:
        code = normalize_stock_code(stock_code)
        prefix_params = {
            "callback": "callback",
            "lang": "ZH",
            "type": "A",
            "name": code,
            "market": "SEHK",
        }
        status, headers, body, final = self._send(PREFIX_URL, prefix_params)
        stock = self._decode_prefix(status, headers, body, final, stock_code=code)
        request_log: list[dict[str, Any]] = [{"name": "prefix", "url": final, "status": status}]
        rows: list[dict[str, Any]] = []
        meta: dict[str, Any] = {"has_next": False}
        for page in range(MAX_PAGES):
            params = search_params(
                stock_id=int(stock["stock_id"]),
                since=since,
                until=until,
                row_range=PAGE_SIZE * (page + 1),
            )
            status, headers, body, final = self._send(SEARCH_URL, params)
            rows, meta = self._decode_search(status, headers, body, final)
            request_log.append(
                {
                    "name": "titleSearch",
                    "url": _with_query(SEARCH_URL, params),
                    "status": status,
                    "record_count": meta.get("record_count"),
                    "loaded": meta.get("loaded"),
                    "has_next": meta.get("has_next"),
                    "from_html": bool(meta.get("from_html")),
                }
            )
            if not meta.get("has_next"):
                break
        truncated = bool(meta.get("has_next"))
        items, skipped, warnings = announcements_from_rows(rows, stock_code=code)
        kept: list[dict[str, Any]] = []
        for item in items:
            day = date.fromisoformat(item["published_at"][:10])
            if since <= day <= until:
                kept.append(item)
            else:
                skipped += 1
        if not rows:
            warnings.append("这个日期窗口内，財務資料分类下没有公告。不一定是反爬；可能是窗口里确实没有披露。")
        if truncated:
            warnings.append(
                f"披露易仍有下一页（最多取 {MAX_PAGES * PAGE_SIZE} 条）。结果可能不完整；请缩小日期窗口。"
            )
        info = source_info()
        info["min_interval_seconds"] = self.min_interval
        return {
            "stock_id": stock["stock_id"],
            "stock_code": stock["stock_code"],
            "stock_name": stock["stock_name"],
            "items": kept,
            "skipped": skipped,
            "warnings": warnings,
            "truncated": truncated,
            "fetched": len(rows),
            "requests": request_log,
            "source": info,
        }

    def download_pdf(self, url: str) -> bytes:
        check_hkex_url(url)
        status, headers, body, final = self._send(url, None)
        check_hkex_url(final)
        if not body.startswith(b"%PDF"):
            raise HkexError(
                "下载内容不是 PDF",
                url=final,
                status=status,
                content_type=headers.get("content-type", ""),
                snippet=snippet(body),
                hint="披露易有时会返回拦截 HTML。未把该响应写入 data/。",
            )
        return body
