"""Stage3 soft-delete: next-day reaction and T+N revisions are no-ops."""

from __future__ import annotations

from pipeline.commands.run_cmd import refresh_recent_stage3, run_stage3
from builder.build import _is_stage3_warning, _price_view, _revision_rows


def test_refresh_recent_stage3_is_noop():
    out = refresh_recent_stage3()
    assert out["discontinued"] is True
    assert out["refreshed"] == []


def test_run_stage3_returns_existing_doc_without_writing(tmp_path, monkeypatch):
    import pipeline.commands.run_cmd as run_cmd

    doc = {
        "meta": {"fiscal_period": "FY2026Q1", "release_at_utc": "2026-01-01T00:00:00Z"},
        "status": {"stage": "stage2_done", "warnings": []},
        "price_reaction": {"next_day_pct": None},
        "guidance": {},
    }
    saved = []

    monkeypatch.setattr(run_cmd, "load_period_json", lambda t, p: dict(doc))
    monkeypatch.setattr(run_cmd, "save_period_json", lambda t, p, d: saved.append((t, p, d)))
    monkeypatch.setattr(run_cmd, "mark_stage", lambda *a, **k: saved.append(("mark", a, k)))

    out = run_stage3("META", "FY2026Q1")
    assert out["status"]["stage"] == "stage2_done"
    assert saved == []


def test_stage3_page_helpers_are_blank():
    assert _price_view({"price_reaction": {"next_day_pct": 0.05}})["text"] == "—"
    assert _revision_rows({"guidance": {"analyst_revisions": {"t_plus_1": {"eps": 1}}}})["has"] is False
    assert _is_stage3_warning("交易日收盘价不足，暂无法计算次日涨跌")
    assert not _is_stage3_warning("问答摘要条数不足")
