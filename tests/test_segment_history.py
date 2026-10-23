"""分部和地区收入只保留申报里真有的拆分。"""

from pipeline.sources.segment_history import geo_lines


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
