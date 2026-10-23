"""ADR 6-K 过滤器与配置。不联网。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline.earnings_config import resolve_earnings_spec
from pipeline.extract.currency_guard import apply_currency_guard, currency_prompt_note
from pipeline.extract.numbers import ParsedAmount
from pipeline.validate import validate_extraction
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


_Q4_LEAD = (
    "TSMC today announced consolidated revenue of NT$1,046.09 billion, net income of NT$505.74 billion, "
    "and diluted earnings per share of NT$19.50 (US$3.14 per ADR unit) for the fourth quarter ended December 31, 2025. "
    "And, based on the exchange rate assumption of 1 US dollar to 31.6 NT dollars, "
    "gross profit margin is expected to be between 63% and 65%."
)


def test_currency_prompt_tells_model_to_prefer_adr_eps():
    note = currency_prompt_note("USD")
    assert "US$y.yy per ADR" in note
    assert "不要用汇率" in note
    assert "NT$ million" in note
    assert "没有 In US dollars" in note
    assert "wafer revenue" in note


def test_currency_guard_prefers_official_adr_usd_over_ntd_eps():
    extracted = {
        "eps_gaap_diluted": {
            "raw": "19.50",
            "source_quote": "diluted earnings per share of NT$19.50",
        }
    }
    warnings = apply_currency_guard(extracted, _Q4_LEAD, "USD")
    node = extracted["eps_gaap_diluted"]
    assert node["raw"] == "3.14"
    assert "US$3.14 per ADR" in node["source_quote"]
    assert "NT$19.50" in node["source_quote"]
    assert ParsedAmount.from_dict(node, is_eps=True).value == 3.14
    assert not any("已清空" in warning for warning in warnings)
    assert any("US$3.14 per ADR" in warning and "不做汇率换算" in warning for warning in warnings)
    # 31.6 的汇率假设不能拿来把 19.50 换成 0.62 或约 3.09
    assert node["raw"] not in {"0.62", "3.09", "3.08"}


def test_currency_guard_fills_eps_when_quote_was_truncated_to_ntd():
    extracted = {
        "eps_gaap_diluted": {
            "raw": None,
            "unit": None,
            "source_quote": "diluted earnings per share of NT$19.50",
        }
    }
    apply_currency_guard(extracted, _Q4_LEAD, "USD")
    node = extracted["eps_gaap_diluted"]
    assert node["raw"] == "3.14"
    assert node["source_quote"].startswith("diluted earnings per share of NT$19.50 (US$3.14 per ADR")


def test_currency_guard_does_not_fx_convert_without_adr_dollar():
    extracted = {
        "eps_gaap_diluted": {
            "raw": "19.50",
            "source_quote": "diluted earnings per share of NT$19.50",
        }
    }
    text = (
        "diluted earnings per share of NT$19.50. "
        "exchange rate assumption of 1 US dollar to 31.6 NT dollars"
    )
    warnings = apply_currency_guard(extracted, text, "USD")
    assert extracted["eps_gaap_diluted"]["raw"] is None
    assert any("已清空" in warning for warning in warnings)
    assert not any("3.14" in warning or "汇率换算" in warning for warning in warnings)


def test_currency_guard_uses_diluted_sentence_not_a_later_adr_figure():
    text = (
        "diluted earnings per share of NT$19.50 (US$3.14 per ADR unit) for the fourth quarter. "
        "Illustration only US$9.99 per ADR unit"
    )
    extracted = {"eps_gaap_diluted": {"raw": None, "source_quote": "NT$19.50"}}
    apply_currency_guard(extracted, text, "USD")
    assert extracted["eps_gaap_diluted"]["raw"] == "3.14"
    assert "9.99" not in extracted["eps_gaap_diluted"]["source_quote"]


def test_v2_compares_share_count_to_ntd_eps_when_value_is_adr_usd():
    quote = "diluted earnings per share of NT$19.50 (US$3.14 per ADR unit) for the fourth quarter ended December 31, 2025"
    extracted = {
        "net_income_gaap": {"raw": "505744", "unit": "millions", "source_quote": "Net income 505,744"},
        "diluted_shares": {"raw": "25931", "unit": "millions", "source_quote": "25,931 million weighted average outstanding shares"},
        "eps_gaap_diluted": {"raw": "3.14", "source_quote": quote},
    }
    checked = validate_extraction(extracted, f"{quote} Net income 505,744 25,931 million")
    assert not any(item.startswith("V2:") for item in checked["warnings"])


def test_v2_still_flags_adr_usd_when_quote_has_no_ordinary_eps():
    extracted = {
        "net_income_gaap": {"raw": "505744", "unit": "millions", "source_quote": "Net income 505,744"},
        "diluted_shares": {"raw": "25931", "unit": "millions", "source_quote": "25,931 million"},
        "eps_gaap_diluted": {"raw": "3.14", "source_quote": "US$3.14 per ADR unit"},
    }
    checked = validate_extraction(extracted, "Net income 505,744 US$3.14 per ADR unit 25,931 million")
    assert any(item.startswith("V2:") and "GAAP EPS=" in item for item in checked["warnings"])


_Q3_RELEASE = (
    "TSMC today announced consolidated revenue of NT$1,494.24 billion, net income of NT$763.06 billion, "
    "and diluted earnings per share of NT$29.43 (US$4.60 per ADR unit) for the third quarter ended September 30, 2026. "
    "Year-over-year, third quarter revenue increased 50.9%, while net income and diluted EPS both increased 68.7%. "
    "Compared to second quarter 2026, third quarter results represented a 17.6% increase in revenue and an 8.0% increase in net income. "
    "In US dollars, third quarter revenue was $46.72 billion, which increased 41.2% year-over-year. "
    "Gross margin for the quarter was 66.4%, operating margin was 58.7%, and net profit margin was 51.1%. "
    "In the third quarter, shipments of 2-nanometer accounted for 8% of total wafer revenue; "
    "3-nanometer accounted for 33%; 5-nanometer accounted for 28%; and 7-nanometer accounted for 10%. "
    "Advanced technologies, defined as 7-nanometer and more advanced technologies, accounted for 79% of total wafer revenue. "
    "Revenue is expected to be between US$52.3 billion and US$54.3 billion. "
    "And, based on the exchange rate assumption of 1 US dollar to 31.8 NT dollars, "
    "(Unit: NT$ million, except for EPS) Net income 763,064 Gross profit 992,379 Income from operations 876,445"
)


def test_currency_guard_drops_ntd_table_net_income_without_fx():
    from pipeline.extract.currency_guard import stated_margin_ratios, stated_net_income_changes
    from pipeline.extract.numbers import ParsedAmount

    extracted = {
        "revenue": {
            "raw": "1,494,243",
            "unit": "millions",
            "source_quote": "Net sales 1,494,243",
        },
        "net_income_gaap": {
            "raw": "763,064",
            "unit": "millions",
            "source_quote": "Net income 763,064",
        },
        "gross_profit_gaap": {
            "raw": "992,379",
            "unit": "millions",
            "source_quote": "Gross profit 992,379",
        },
        "operating_income_gaap": {
            "raw": "876,445",
            "unit": "millions",
            "source_quote": "Income from operations 876,445",
        },
        "diluted_shares": {
            "raw": "25,932",
            "unit": "millions",
            "source_quote": "Based on 25,932 million weighted average outstanding shares",
        },
        "eps_gaap_diluted": {"raw": "29.43", "source_quote": "diluted earnings per share of NT$29.43"},
        "kpis": {
            "advanced_node_revenue": {
                "raw": "79",
                "unit": "percent",
                "source_quote": "accounted for 79% of total wafer revenue",
            },
            "hpc_revenue": {"raw": None, "unit": None, "source_quote": ""},
        },
    }
    warnings = apply_currency_guard(extracted, _Q3_RELEASE, "USD")
    revenue = ParsedAmount.from_dict(extracted["revenue"])
    assert revenue.value == 46_720_000_000
    assert "In US dollars" in extracted["revenue"]["source_quote"] or "$46.72" in extracted["revenue"]["source_quote"]
    assert extracted["net_income_gaap"]["raw"] is None
    assert "NT$763.06 billion" in extracted["net_income_gaap"]["source_quote"]
    assert extracted["gross_profit_gaap"]["raw"] is None
    assert extracted["operating_income_gaap"]["raw"] is None
    assert extracted["diluted_shares"]["raw"] == "25,932"
    assert extracted["eps_gaap_diluted"]["raw"] == "4.60"
    # 31.8 的汇率不能把 763.06 billion 换成约 240 亿美元
    assert all("24.0" not in warning and "239" not in warning for warning in warnings)
    assert "advanced_node_revenue" not in extracted["kpis"]
    assert "hpc_revenue" not in extracted["kpis"]
    lines = {line["key"]: line["value"] for line in extracted["kpis"]["wafer_revenue_mix"]["lines"]}
    assert lines["2nm"] == 0.08
    assert lines["3nm"] == 0.33
    assert lines["advanced"] == 0.79
    stated = stated_margin_ratios(_Q3_RELEASE)
    assert stated["gross"] == pytest.approx(0.664)
    assert stated["operating"] == pytest.approx(0.587)
    changes = stated_net_income_changes(_Q3_RELEASE)
    assert changes["yoy"] == 0.687
    assert changes["qoq"] == 0.08
    # 指引里的 US$52.3 billion 不能覆盖本季营收
    assert revenue.value != 52_300_000_000


def test_currency_guard_adopts_explicit_usd_net_income():
    text = _Q3_RELEASE + " In US dollars, net income was $24.01 billion."
    extracted = {
        "net_income_gaap": {
            "raw": "763,064",
            "unit": "millions",
            "source_quote": "Net income 763,064",
        }
    }
    apply_currency_guard(extracted, text, "USD")
    node = extracted["net_income_gaap"]
    assert node["raw"] == "24.01"
    assert node["unit"] == "billions"
    assert "24.01" in node["source_quote"]
    assert ParsedAmount.from_dict(node).value == 24_010_000_000


def test_fin_rows_show_wafer_mix_not_blank_kpi_amount():
    from builder.build import _fin_rows

    doc = {
        "meta": {"ticker": "", "fiscal_period": "FY2026Q3"},
        "financials": {
            "revenue": {"value": 46.72e9, "yoy_pct": None, "qoq_pct": None, "benchmark": {}},
            "kpis": {
                "wafer_revenue_mix": {
                    "lines": [
                        {"key": "2nm", "name": "2纳米晶圆收入占比", "value": 0.08},
                        {"key": "advanced", "name": "先进制程（7纳米及更先进）", "value": 0.79, "yoy_pp": 0.02},
                    ]
                }
            },
            "net_income_gaap": {
                "value": None,
                "yoy_pct": 0.687,
                "qoq_pct": 0.08,
                "source_quote": "net income of NT$763.06 billion",
            },
        },
        "drivers": {
            "metrics": [
                {
                    "metric": "kpis.advanced_node_revenue",
                    "summary": "先进制程收入占比79%，其中2纳米占8%。",
                    "drivers": [],
                },
                {
                    "metric": "net_income_gaap",
                    "summary": "新闻稿未披露美元净利，仅有新台币口径，故不记入美元金额。净利同比增长 68.7%。",
                    "drivers": [],
                },
            ]
        },
    }
    rows = {row["key"]: row for row in _fin_rows(doc)}
    assert "kpis.advanced_node_revenue" not in rows
    assert rows["wafer_revenue_mix.2nm"]["value"] == "8.0%"
    assert rows["wafer_revenue_mix.advanced"]["value"] == "79.0%"
    assert rows["wafer_revenue_mix.advanced"]["yoy"]["text"] == "+2.0pp"
    assert "79%" in rows["wafer_revenue_mix.advanced"]["summary"]
    assert rows["net_income_gaap"]["value"] == "—"
    assert rows["net_income_gaap"]["label"] == "净利润（GAAP）"
    assert "新台币" in rows["net_income_gaap"]["summary"]
    assert "$" not in rows["net_income_gaap"]["value"]


def test_tsm_press_releases_resolve_to_official_adr_eps():
    from pipeline.extract.press_release import load_press_release

    expected = {
        "FY2024Q4": "2.24",
        "FY2025Q1": "2.12",
        "FY2025Q2": "2.47",
        "FY2025Q3": "2.92",
        "FY2025Q4": "3.14",
        "FY2026Q1": "3.49",
        "FY2026Q2": "4.31",
        "FY2026Q3": "4.60",
    }
    from pipeline import ROOT

    root = ROOT / "data" / "TSM" / "raw"
    for period, usd in expected.items():
        parsed = load_press_release(root / period / "press_release.html")
        extracted = {"eps_gaap_diluted": {"raw": "19.50", "source_quote": "diluted earnings per share of NT$19.50"}}
        apply_currency_guard(extracted, parsed["combined"], "USD")
        node = extracted["eps_gaap_diluted"]
        assert node["raw"] == usd, period
        assert f"US${usd} per ADR" in node["source_quote"]


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
    # 首页 ADR 徽章，以及有财季后的业绩页页眉，都写明不是 8-K Item 2.02。
    assert "不是 8-K Item 2.02" in index
    assert "不是 8-K Item 2.02" in page
    assert 'class="stage-badge s-adr"' in page
    assert "USD" in page and "taiwan_ifrs" in page
    assert "SEC 6-K" in page or "6-K" in page
