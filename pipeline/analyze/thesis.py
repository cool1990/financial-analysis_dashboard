"""投资论点评估：每季一次小调用，判断每条论点被强化 / 未变 / 削弱，并找出论点没覆盖的新担忧。

输入只用已经结构化的结果（不含文字稿全文），控制 token；
评估结果按「论点文件内容 + 本季数据」做指纹，两者都没变时不再调用。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pipeline.llm import LLMClient
from pipeline.schemas import ThesisReviewResult
from pipeline.validate import fuzzy_quote_ok


def _thesis_payload(theses: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": t.get("id"),
            "name": t.get("name"),
            "bull": t.get("bull"),
            "bear": t.get("bear"),
            "confirm": t.get("confirm") or [],
            "falsify": t.get("falsify") or [],
        }
        for t in theses.get("theses") or []
    ]


def _evidence_payload(doc: dict[str, Any]) -> dict[str, Any]:
    """本季证据：只保留判断需要的字段，原文引用保留用于校验。"""
    fin = doc.get("financials") or {}
    return {
        "scorecard": [
            {k: c.get(k) for k in ("metric", "actual", "verdict")} | {"benchmark": (c.get("benchmark") or {}).get("value")}
            for c in doc.get("scorecard") or []
        ],
        "financials": {
            k: {f: v.get(f) for f in ("value", "yoy_pct", "qoq_pct", "yoy_pp", "qoq_pp")}
            for k, v in fin.items()
            if isinstance(v, dict) and "value" in v
        },
        "guidance": [
            {k: g.get(k) for k in ("metric_key", "metric_label", "period", "mid", "direction", "statement", "source", "source_quote")}
            for g in (doc.get("guidance") or {}).get("items") or []
        ],
        "qa": [
            {k: q.get(k) for k in ("analyst", "firm", "topic", "question_summary", "answer_summary", "directness", "answer_quote")}
            for q in (doc.get("qa") or {}).get("items") or []
            if not q.get("parse_failed")
        ],
        "drivers": [
            {
                "metric": m.get("metric"),
                "summary": m.get("summary"),
                "evidence_quote": [d.get("evidence_quote") for d in m.get("drivers") or [] if d.get("evidence_quote")],
            }
            for m in (doc.get("drivers") or {}).get("metrics") or []
        ],
    }


def review_fingerprint(theses: dict[str, Any], doc: dict[str, Any]) -> str:
    h = hashlib.sha256()
    h.update(json.dumps(_thesis_payload(theses), ensure_ascii=False, sort_keys=True).encode())
    h.update(json.dumps(_evidence_payload(doc), ensure_ascii=False, sort_keys=True).encode())
    return h.hexdigest()[:16]


def _quote_corpus(evidence: dict[str, Any]) -> str:
    parts: list[str] = []
    for g in evidence["guidance"]:
        parts += [g.get("source_quote") or "", g.get("statement") or ""]
    for q in evidence["qa"]:
        parts += [q.get("answer_quote") or "", q.get("answer_summary") or ""]
    for d in evidence["drivers"]:
        parts += list(d.get("evidence_quote") or [])
    return "\n".join(p for p in parts if p)


def review_theses(theses: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    llm = LLMClient()
    evidence = _evidence_payload(doc)
    prompt = llm.load_prompt(
        "review_theses.md",
        theses=json.dumps(_thesis_payload(theses), ensure_ascii=False, indent=1),
    )
    user = prompt + "\n" + json.dumps(evidence, ensure_ascii=False)
    result = llm.complete_json("review_theses.md", user, ThesisReviewResult).model_dump()

    known = {t.get("id") for t in theses.get("theses") or []}
    corpus = _quote_corpus(evidence)
    reviews = []
    for r in result.get("reviews") or []:
        if r.get("id") not in known:
            continue
        # 与变动原因同样的做法：引文在原材料里找不到就标出来，不当作证据展示
        r["quote_unverified"] = bool(r.get("quote")) and not fuzzy_quote_ok(r["quote"], corpus)
        reviews.append(r)
    return {
        "reviews": reviews,
        "new_concerns": result.get("new_concerns") or [],
        "fingerprint": review_fingerprint(theses, doc),
        "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": llm.model,
    }
