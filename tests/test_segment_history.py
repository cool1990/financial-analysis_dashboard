"""分部和地区收入只保留申报里真有的拆分。"""

from datetime import date

from pipeline.sources.segment_history import (
    _drop_incomplete_residuals,
    _fill_q4_from_three_quarters,
    assemble_periods,
    fiscal_quarter_key,
    geo_lines,
    lines_for,
    parse_composition_table,
)


def test_geo_lines_prefer_four_regions_over_the_older_us_split():
    lines = geo_lines(
        {
            "US": 100,
            "USCanadaMember": 120,
            "EuropeMember": 40,
            "AsiaPacificMember": 30,
            "RestOfWorldMember": 10,
        }
    )
    assert [line["key"] for line in lines] == ["us_ca", "europe", "apac", "row"]


def test_geo_lines_keep_us_and_rest_when_four_regions_are_missing():
    lines = geo_lines({"US": 2509, "RestOfWorldMember": 2873})
    assert [(line["key"], line["name"]) for line in lines] == [("us", "美国"), ("row_broad", "美国以外")]


def test_geo_lines_skip_a_partial_region_set():
    assert geo_lines({"USCanadaMember": 10, "EuropeMember": 4}) == []


def _members(**pairs: float) -> dict[tuple[str, ...], float]:
    return {tuple(name.split("|")): value for name, value in pairs.items()}


def test_lines_for_keeps_children_when_they_reconcile_to_the_parent():
    lines = lines_for(
        _members(
            **{
                "FamilyOfAppsMember": 100,
                "AdvertisingMember|FamilyOfAppsMember": 80,
                "FamilyOfAppsMember|ServiceOtherMember": 20,
                "RealityLabsMember": 5,
            }
        )
    )
    assert [(line["key"], line["stack"], line["depth"]) for line in lines] == [
        ("foa", False, 0),
        ("ads", True, 1),
        ("other", True, 1),
        ("rl", True, 0),
    ]


def test_lines_for_drops_children_when_parent_does_not_reconcile():
    """子项加总离母项超过 2% 时，不把对不上的子项当成占比。"""
    lines = lines_for(
        _members(
            **{
                "FamilyOfAppsMember": 100,
                "AdvertisingMember|FamilyOfAppsMember": 50,
                "RealityLabsMember": 5,
            }
        )
    )
    assert [(line["key"], line["stack"]) for line in lines] == [("foa", True), ("rl", True)]


def test_lines_for_payments_without_a_parent():
    lines = lines_for(_members(AdvertisingMember=10, PaymentsandOtherFeesMember=1))
    assert [line["key"] for line in lines] == ["ads", "other"]
    assert all(line["stack"] for line in lines)


def _fact(start: str, end: str, member: str, value: float) -> dict:
    d0 = date.fromisoformat(start)
    d1 = date.fromisoformat(end)
    return {
        "tag": "Revenues",
        "start": start,
        "end": end,
        "days": (d1 - d0).days,
        "members": (member,),
        "value": value,
    }


def test_assemble_periods_uses_table_xbrl_and_same_filing_residual():
    filings = [
        {
            "filed": "2016-04-28",
            "form": "10-Q",
            "accession": "table-1",
            "report": "2016-03-31",
            "facts": [],
            "composition": {"AdvertisingMember": 100.0, "PaymentsandOtherFeesMember": 10.0},
        },
        {
            "filed": "2016-07-28",
            "form": "10-Q",
            "accession": "xbrl-1",
            "report": "2016-06-30",
            "facts": [
                _fact("2016-04-01", "2016-06-30", "AdvertisingMember", 200),
                _fact("2016-04-01", "2016-06-30", "PaymentsandOtherFeesMember", 20),
            ],
        },
        {
            "filed": "2017-02-01",
            "form": "10-K",
            "accession": "k-1",
            "report": "2016-12-31",
            "facts": [
                _fact("2016-01-01", "2016-09-30", "AdvertisingMember", 700),
                _fact("2016-01-01", "2016-09-30", "PaymentsandOtherFeesMember", 70),
                _fact("2016-01-01", "2016-12-31", "AdvertisingMember", 1000),
                _fact("2016-01-01", "2016-12-31", "PaymentsandOtherFeesMember", 100),
            ],
        },
    ]
    out = assemble_periods(filings, 12)
    by_end = {row["end"]: row for row in out["quarters"]}
    assert by_end["2016-03-31"]["method"] == "table"
    assert by_end["2016-03-31"]["lines"][0]["value"] == 100
    assert by_end["2016-06-30"]["method"] == "xbrl"
    assert by_end["2016-12-31"]["method"] == "residual"
    assert [line["value"] for line in by_end["2016-12-31"]["lines"]] == [300, 30]
    assert "2016-09-30" not in by_end


def _stack(ads: float, other: float) -> list[dict]:
    return [
        {"key": "ads", "name": "广告", "value": ads, "depth": 0, "stack": True},
        {"key": "other", "name": "支付及其他费用", "value": other, "depth": 0, "stack": True},
    ]


def _qrow(end: str, lines: list[dict], method: str = "xbrl") -> dict:
    return {
        "end": end,
        "lines": lines,
        "filed": "2025-06-01",
        "form": "10-Q",
        "accession": "acc",
        "method": method,
    }


def test_fill_q4_uses_fiscal_year_end_month_not_december():
    quarters = [
        _qrow("2024-08-31", _stack(10, 1)),
        _qrow("2024-11-30", _stack(20, 2)),
        _qrow("2025-02-28", _stack(30, 3)),
    ]
    years = [_qrow("2025-05-31", _stack(100, 10), "xbrl")]
    years[0]["form"] = "10-K"
    filled = _fill_q4_from_three_quarters(quarters, years, 5)
    q4 = next(row for row in filled if row["end"] == "2025-05-31")
    assert q4["method"] == "residual"
    assert [line["value"] for line in q4["lines"]] == [40, 4]

    # 12 月财年不把 5 月结束的全年当成第四季
    assert _fill_q4_from_three_quarters(quarters, years, 12) == quarters


def test_fill_q4_leaves_the_quarter_blank_when_one_quarter_or_the_gap_is_missing():
    quarters = [
        _qrow("2024-03-31", _stack(10, 1)),
        _qrow("2024-09-30", _stack(30, 3)),
    ]
    years = [_qrow("2024-12-31", _stack(100, 10))]
    assert _fill_q4_from_three_quarters(quarters, years, 12) == quarters

    complete = quarters + [_qrow("2024-06-30", _stack(80, 20))]
    # 10+80+30=120 > 100，广告差额不是正数；支付 1+20+3=24 > 10，同样跳过
    filled = _fill_q4_from_three_quarters(complete, years, 12)
    assert [row["end"] for row in filled] == ["2024-03-31", "2024-06-30", "2024-09-30"]
    assert all(row["method"] == "xbrl" for row in filled)


def test_fiscal_quarter_key_matches_the_period_calendar():
    from pipeline.compute.periods import fiscal_quarter_end

    for fiscal_year_end_month in (1, 5, 6, 8, 11, 12):
        for year in (2016, 2024, 2026):
            for quarter in (1, 2, 3, 4):
                end = fiscal_quarter_end(year, quarter, fiscal_year_end_month)
                assert fiscal_quarter_key(end.isoformat(), fiscal_year_end_month) == (year, quarter)


def test_fill_q4_december_fiscal_year_still_subtracts_the_first_three_quarters():
    quarters = [
        _qrow("2024-03-31", _stack(10, 1)),
        _qrow("2024-06-30", _stack(20, 2)),
        _qrow("2024-09-30", _stack(30, 3)),
    ]
    years = [_qrow("2024-12-31", _stack(100, 10))]
    filled = _fill_q4_from_three_quarters(quarters, years, 12)
    q4 = next(row for row in filled if row["end"] == "2024-12-31")
    assert [line["value"] for line in q4["lines"]] == [40, 4]
    assert fiscal_quarter_key("2024-12-31", 12) == (2024, 4)
    assert fiscal_quarter_key("2025-05-31", 5) == (2025, 4)
    # 11 月财年的 52/53 周季末，离名义月末大约四周，仍然是该季。
    assert fiscal_quarter_key("2017-01-29", 11) == (2017, 1)
    assert fiscal_quarter_key("2025-11-02", 11) == (2025, 4)
    assert fiscal_quarter_key("2026-08-02", 11) == (2026, 3)
    assert fiscal_quarter_key("2017-04-05", 11) is None


def test_drop_incomplete_residuals_keeps_xbrl_and_complete_residuals():
    quarters = [
        _qrow("2024-09-30", _stack(10, 1)),
        _qrow("2024-12-31", _stack(4, 1)[:1], "residual"),
        _qrow("2025-03-31", _stack(11, 1)),
        _qrow("2025-06-30", [_stack(1, 1)[0]], "xbrl"),
    ]
    kept = _drop_incomplete_residuals(quarters)
    assert [row["end"] for row in kept] == ["2024-09-30", "2025-03-31", "2025-06-30"]


def test_parse_composition_table_reads_the_10q_advertising_column():
    html = """
    <p>in millions</p>
    <table><tr><td>Advertising</td><td>$ 5,201</td></tr>
    <tr><td>Payments and other fees</td><td>$ 181</td></tr></table>
    """
    assert parse_composition_table(html) == {
        "AdvertisingMember": 5_201_000_000,
        "PaymentsandOtherFeesMember": 181_000_000,
    }
    assert parse_composition_table("<p>no table</p>") is None
