"""ADR 6-K 过滤器与配置。不联网。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline.earnings_config import resolve_earnings_spec
from pipeline.extract.currency_guard import apply_currency_guard
from pipeline.sources.earnings_6k import (
    classify_6k,
    exhibit_descriptions_from_html,
    merge_exhibits,
    select_press_and_presentation,
)


FILTERS = {
    "require_exhibit_desc_any": [r"(?i)press\s*release", r"(?i)earnings"],
    "reject_exhibit_desc_any": [
        r"(?i)consolidated\s+financial\s+statements",
        r"(?i)monthly\s+revenue",
        r"(?i)dividend",
    ],
    "reject_primary_doc_any": [
        r"(?i)monthend",
        r"(?i)revenue20",
        r"(?i)dividend",
        r"(?i)board",
        r"(?i)agm",
        r"(?i)fsx",
    ],
    "prefer_exhibit_desc": r"(?i)press\s*release",
}

EARNINGS_COVER = """
<html><body>
<a href="q-release.htm">Press release </a><a href="q-release.htm">issued by Example</a>
<a href="q-slides.htm">Third Quarter Earnings Conference Presentation</a>
</body></html>
"""

FS_COVER = """
<html><body>
<a href="statements.htm">Consolidated Financial Statements for the quarter</a>
</body></html>
"""


def test_domestic_defaults_stay_8k_item_202():
    spec = resolve_earnings_spec({"cik": "0000000001", "fiscal_year_end_month": 12})
    assert spec.listing == "domestic"
    assert spec.earnings_forms == ["8-K"]
    assert spec.earnings_items == ["2.02"]
    assert spec.earnings_6k is None
    assert spec.reporting_currency == "USD"


def test_adr_schema_uses_6k_filters_not_item_202():
    spec = resolve_earnings_spec(
        {
            "listing": "adr",
            "reporting_currency": "usd",
            "accounting_basis": "taiwan_ifrs",
            "sec": {"earnings_forms": ["6-K"], "earnings_items": []},
        }
    )
    assert spec.listing == "adr"
    assert spec.reporting_currency == "USD"
    assert spec.accounting_basis == "taiwan_ifrs"
    assert spec.earnings_forms == ["6-K"]
    assert spec.earnings_items == []
    assert spec.earnings_6k is not None
    assert any("press" in pattern for pattern in spec.earnings_6k.require_exhibit_desc_any)


def test_listing_must_be_domestic_or_adr():
    with pytest.raises(ValueError):
        resolve_earnings_spec({"listing": "twse"})


def test_settings_profile_overrides_same_name(monkeypatch):
    monkeypatch.setattr(
        "pipeline.earnings_config.load_settings",
        lambda: {
            "sec": {
                "earnings_6k_profiles": {
                    "adr": {"prefer_exhibit_desc": "(?i)earnings\\s+release"},
                }
            }
        },
    )
    spec = resolve_earnings_spec({"listing": "adr", "sec": {"earnings_6k": {"prefer_exhibit_desc": "(?i)press\\s*release"}}})
    assert spec.earnings_6k is not None
    assert spec.earnings_6k.prefer_exhibit_desc == r"(?i)earnings\s+release"


def test_cover_press_release_hits_and_fs_is_skipped():
    cover = exhibit_descriptions_from_html(EARNINGS_COVER)
    exhibits = merge_exhibits(
        [
            {"name": "q-release.htm", "description": "", "index_description": "EX-99.1"},
            {"name": "q-slides.htm", "description": "", "index_description": "EX-99.2"},
            {"name": "cover.htm", "description": "", "index_description": "6-K"},
        ],
        cover,
    )
    assert classify_6k("cover.htm", exhibits, FILTERS) == ("hit", "exhibit_desc")
    press, presentation = select_press_and_presentation(exhibits, FILTERS, primary_document="cover.htm")
    assert press is not None and press["name"] == "q-release.htm"
    assert "Press release" in press["description"]
    assert presentation is not None and presentation["name"] == "q-slides.htm"

    fs = merge_exhibits(
        [{"name": "statements.htm", "description": "", "index_description": "EX-99.1"}],
        exhibit_descriptions_from_html(FS_COVER),
    )
    assert classify_6k("report.htm", fs, FILTERS) == ("skip", "reject_exhibit")


def test_primary_doc_noise_skipped_without_being_a_hit():
    for name in (
        "monthend-6k.htm",
        "revenue20261008.htm",
        "fsx-package.htm",
        "dividend-adjustment.htm",
        "board-meeting.htm",
        "agm-notice.htm",
    ):
        assert classify_6k(name, [], FILTERS)[0] == "skip"
        assert classify_6k(name, [], FILTERS)[1] == "primary_doc"


def test_press_release_preferred_over_ex991_filename():
    exhibits = [
        {
            "name": "consolidated.htm",
            "description": "EX-99.1",
            "index_description": "EX-99.1",
        },
        {
            "name": "guidance-final.htm",
            "description": "Press release issued today",
            "index_description": "",
        },
    ]
    # 没有 Press release 的合并报表描述若同时像 reject，会在分类阶段丢掉。
    # 这里描述只是 EX-99.1，选文件时仍应优先 Press release，而不是文件名里的 99.1。
    press, _presentation = select_press_and_presentation(exhibits, FILTERS, primary_document="cover.htm")
    assert press is not None and press["name"] == "guidance-final.htm"


def test_currency_guard_drops_ntd_only_amounts():
    extracted = {
        "revenue": {"raw": "NT$1,494.24 billion", "source_quote": "revenue of NT$1,494.24 billion"},
        "eps_gaap_diluted": {"raw": "4.60", "source_quote": "US$4.60 per ADR unit"},
    }
    warnings = apply_currency_guard(extracted, "NT$29.43 (US$4.60 per ADR)", "USD")
    assert extracted["revenue"]["raw"] is None
    assert extracted["eps_gaap_diluted"]["raw"] == "4.60"
    assert any("NT$" in warning for warning in warnings)


def test_detect_new_filings_adr_uses_6k_not_8k(monkeypatch, tmp_path):
    from pipeline.commands import run_cmd
    from pipeline import state

    now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
    filings = [
        {
            "accessionNumber": "new-6k",
            "acceptanceDateTime": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "form": "6-K",
            "items": "",
            "primaryDocument": "cover.htm",
            "press_release_file": "release.htm",
        }
    ]
    skips = [
        {
            "accessionNumber": "monthend-1",
            "reason": "primary_doc",
            "acceptanceDateTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "primaryDocument": "monthend.htm",
        }
    ]
    scan = type("Scan", (), {"hits": filings, "skips": skips})()

    monkeypatch.setattr(
        run_cmd,
        "load_ticker_config",
        lambda ticker: {
            "cik": "0001046179",
            "listing": "adr",
            "reporting_currency": "USD",
            "accounting_basis": "taiwan_ifrs",
            "sec": {"earnings_forms": ["6-K"], "earnings_items": []},
        },
    )
    monkeypatch.setattr(run_cmd.SecEdgarClient, "__init__", lambda self, *args, **kwargs: None)
    monkeypatch.setattr(run_cmd.SecEdgarClient, "find_earnings_8k", lambda self, cik: (_ for _ in ()).throw(AssertionError("ADR 不应走 8-K")))
    monkeypatch.setattr(run_cmd.SecEdgarClient, "scan_earnings_filings", lambda self, cik, cfg, **kwargs: scan)
    monkeypatch.setattr(state, "data_dir", lambda ticker=None: tmp_path)
    monkeypatch.setattr(run_cmd, "load_processed", state.load_processed)
    monkeypatch.setattr(run_cmd, "load_failures", state.load_failures)
    monkeypatch.setattr(run_cmd, "load_ignored", state.load_ignored)
    monkeypatch.setattr(run_cmd, "add_ignored", state.add_ignored)

    out = run_cmd.detect_new_filings("TSM", now=now)
    assert [row["accessionNumber"] for row in out] == ["new-6k"]
    ignored = state.load_ignored("TSM")
    assert "monthend-1" in ignored
    assert "new-6k" not in ignored


def test_download_press_release_adr_writes_only_press_file(tmp_path):
    from pipeline.sources.sec_edgar import SecEdgarClient

    client = SecEdgarClient.__new__(SecEdgarClient)
    saved: list[str] = []

    def _download(cik, accession, filename):
        saved.append(filename)
        return f"<html>{filename}</html>".encode()

    client._cached_filing_bytes = _download  # type: ignore[method-assign]
    dest = tmp_path / "press_release.html"
    presentation = tmp_path / "presentation.html"
    client.download_press_release(
        "0001046179",
        "0001046179-26-000726",
        dest,
        press_release_file="release.htm",
        presentation_file="slides.htm",
        presentation_dest=presentation,
    )
    assert saved == ["release.htm", "slides.htm"]
    assert b"release.htm" in dest.read_bytes()
    assert b"slides.htm" not in dest.read_bytes()
    assert b"slides.htm" in presentation.read_bytes()


def test_tsm_yaml_is_adr_6k():
    from pipeline.config import load_ticker_config

    cfg = load_ticker_config("TSM")
    spec = resolve_earnings_spec(cfg)
    assert cfg["cik"] == "0001046179"
    assert cfg["name"].endswith("(ADR)")
    assert spec.listing == "adr"
    assert spec.reporting_currency == "USD"
    assert spec.accounting_basis == "taiwan_ifrs"
    assert spec.earnings_forms == ["6-K"]
    assert spec.earnings_items == []
    assert spec.earnings_6k is not None
    assert "fsx" in " ".join(spec.earnings_6k.reject_primary_doc_any)


def test_homepage_marks_tsm_as_adr(tmp_path, monkeypatch):
    import builder.build as build

    monkeypatch.setattr(build, "site_dir", lambda: tmp_path)
    build.build_site("TSM")
    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    page = (tmp_path / "stocks" / "TSM" / "index.html").read_text(encoding="utf-8")
    assert 'class="stage-badge s-adr"' in index
    assert ">TSM</a> <span" in index and ">ADR</span>" in index
    assert "不是 8-K Item 2.02" in page
    assert "USD" in page and "taiwan_ifrs" in page
    assert "SEC 6-K" in page or "6-K" in page
