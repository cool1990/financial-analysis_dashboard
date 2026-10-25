"""首页今日修正 / RSI信号：对齐 Hermes 盈利跟踪简要总结。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from pipeline.compute import earnings_brief as eb


FIXTURE_NOTE = """---
data_date: 2026-10-24
---

# 美港股盈利指标跟踪 2026-10-24

## 简要总结
1、今日修正：MU：少数上修——强上修（当前季EPS 38.28，30日+7.3%，上/下修30日 26/2，覆盖30）。
2、RSI信号：低于30 SNDK；高于70 无。
3、估值触发：MU（5.03<6）。

|ticker|收盘价|
|---|---|
|MU|1|
"""


def test_hermes_revision_signal_matches_locked_order():
    assert eb.hermes_revision_signal(None, 10, 0.1, 5, 0) == "无数据"
    assert eb.hermes_revision_signal(0.05, 10, 0.2, 5, 0) == "eps低基数"
    assert eb.hermes_revision_signal(1.0, 2, 0.2, 5, 0) == "样本不足"
    assert eb.hermes_revision_signal(1.0, 10, 0.2, 0, 0) == "样本不足"
    assert eb.hermes_revision_signal(1.0, 10, 0.07, 3, 0) == "强上修"  # net/cov=0.3
    assert eb.hermes_revision_signal(1.0, 10, -0.07, 0, 3) == "强下修"
    # 少数：|chg|≥5% 且 |net|<2（Hermes；站点表列用 ≥2，这里必须跟 Hermes）
    assert eb.hermes_revision_signal(1.0, 10, 0.06, 1, 0) == "少数上修"
    assert eb.hermes_revision_signal(1.0, 10, -0.06, 0, 1) == "少数下修"
    assert eb.hermes_revision_signal(1.0, 10, 0.01, 5, 0) == "中性"


def test_parse_brief_from_note(tmp_path: Path):
    path = tmp_path / "盈利跟踪_2026-10-24.md"
    path.write_text(FIXTURE_NOTE, encoding="utf-8")
    brief = eb.parse_brief_from_note(path)
    assert brief is not None
    assert brief["data_date"] == "2026-10-24"
    assert brief["revision_empty"] is False
    assert "MU：少数上修——强上修" in brief["revision_text"]
    assert brief["rsi_low"] == ["SNDK"]
    assert brief["rsi_high"] == []
    assert "低于30 SNDK" in brief["rsi_text"]


def test_parse_brief_empty_revision(tmp_path: Path):
    path = tmp_path / "盈利跟踪_2026-10-23.md"
    path.write_text(
        "## 简要总结\n1、今日修正：无。\n2、RSI信号：低于30 无；高于70 无。\n",
        encoding="utf-8",
    )
    brief = eb.parse_brief_from_note(path)
    assert brief is not None
    assert brief["revision_empty"] is True
    assert brief["revision_text"] == "今日修正：无。"


def test_find_latest_note(tmp_path: Path):
    (tmp_path / "盈利跟踪_2026-10-23.md").write_text("## 简要总结\n1、今日修正：无。\n2、RSI信号：低于30 无；高于70 无。\n", encoding="utf-8")
    newer = tmp_path / "盈利跟踪_2026-10-24.md"
    newer.write_text(FIXTURE_NOTE, encoding="utf-8")
    assert eb.find_latest_note([tmp_path]) == newer


def test_load_market_brief_prefers_fresh_note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "盈利跟踪_2026-10-24.md").write_text(FIXTURE_NOTE, encoding="utf-8")
    out = tmp_path / "market_brief.json"
    real_repo_path = eb.repo_path

    def fake_repo_path(*parts: str):
        if parts and parts[-1] == "market_brief.json":
            return out
        return real_repo_path(*parts)

    monkeypatch.setattr(eb, "note_search_roots", lambda: [notes])
    monkeypatch.setattr(eb, "repo_path", fake_repo_path)
    brief = eb.load_market_brief(tickers=["MU"], today=date(2026, 10, 24), persist=True)
    assert brief["source"] == "note"
    assert "MU：少数上修——强上修" in brief["revision_text"]
    assert out.exists()


def test_load_market_brief_falls_back_when_note_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    notes = tmp_path / "notes"
    notes.mkdir()
    stale = FIXTURE_NOTE.replace("2026-10-24", "2026-09-01")
    (notes / "盈利跟踪_2026-09-01.md").write_text(stale, encoding="utf-8")
    monkeypatch.setattr(eb, "note_search_roots", lambda: [notes])
    monkeypatch.setattr(
        eb,
        "compute_brief_from_pool",
        lambda *a, **k: {
            "data_date": "2026-10-24",
            "source": "compute",
            "revision_body": "无",
            "rsi_body": "低于30 无；高于70 无",
            "revision_text": "今日修正：无。",
            "rsi_text": "RSI信号：低于30 无；高于70 无。",
            "revision_empty": True,
            "rsi_low": [],
            "rsi_high": [],
            "signals": {"MU": "中性"},
        },
    )
    brief = eb.load_market_brief(tickers=["MU"], today=date(2026, 10, 24), persist=False)
    assert brief["source"] == "compute"


def test_homepage_includes_signal_blocks(tmp_path: Path):
    import re
    from builder.build import build_site

    pages = build_site(out_dir=tmp_path)
    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "今日修正" in index and "RSI 信号" in index
    assert "pool-signals" in index
    assert "今日修正：" in index and "RSI信号：" in index
    row = re.search(
        r'<tr data-status="(\w+)"[^>]*>\s*<td[^>]*>\s*<a[^>]*>闪迪 \(SNDK\)',
        index,
    )
    assert row is not None and row.group(1) == "watch"
    assert any(p.name == "index.html" for p in pages)
