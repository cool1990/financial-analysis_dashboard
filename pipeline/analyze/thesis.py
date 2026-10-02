"""仓位评估：每季一次小调用，按仓位阶段回答不同的问题。

- 观察仓：用本季事实回答「想搞清楚的问题」
- 等待仓 / 持仓：论点被强化 / 不变 / 削弱，证伪是否出现，买卖条件是否触发
- 所有阶段：分析师问到、但档案没覆盖的新担忧

输入只用已经结构化的结果（不含文字稿全文），控制 token；
按「档案内容 + 本季数据」做指纹，两者都没变时不再调用。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pipeline.llm import LLMClient
from pipeline.schemas import PositionReviewResult
from pipeline.validate import fuzzy_quote_ok


def _position_payload(position: dict[str, Any]) -> dict[str, Any]:
    return {
        "阶段": position.get("stage_label"),
        "论点": [{"看好": t["bull"], "担心": t["bear"], "证伪": t["falsify"]} for t in position.get("theses") or []],
        "条件": {t["name"]: t["text"] for t in position.get("triggers") or []},
        "问题": position.get("questions") or [],
    }


def needs_review(position: dict[str, Any] | None) -> bool:
    """观察仓没写问题时只看事实，不需要调用。"""
    return bool(position and (position.get("theses") or position.get("triggers") or position.get("questions")))


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
        "price_reaction": {
            k: (doc.get("price_reaction") or {}).get(k)
            for k in ("next_day_pct", "close_before", "close_after", "benchmark", "benchmark_pct", "excess_pct")
        },
        "drivers": [
            {
                "metric": m.get("metric"),
                "summary": m.get("summary"),
                "evidence_quote": [d.get("evidence_quote") for d in m.get("drivers") or [] if d.get("evidence_quote")],
            }
            for m in (doc.get("drivers") or {}).get("metrics") or []
        ],
    }


def review_fingerprint(position: dict[str, Any], doc: dict[str, Any]) -> str:
    h = hashlib.sha256()
    h.update(json.dumps(_position_payload(position), ensure_ascii=False, sort_keys=True).encode())
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


def review_position(position: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    llm = LLMClient()
    evidence = _evidence_payload(doc)
    prompt = llm.load_prompt(
        "review_theses.md",
        position=json.dumps(_position_payload(position), ensure_ascii=False, indent=1),
    )
    user = prompt + "\n" + json.dumps(evidence, ensure_ascii=False)
    result = llm.complete_json("review_theses.md", user, PositionReviewResult).model_dump()

    corpus = _quote_corpus(evidence)

    def checked(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        out = []
        for r in rows:
            if not 0 <= int(r.get("index", -1)) < limit:
                continue
            # 引文在原材料里找不到就标出来，页面不当作证据展示
            r["quote_unverified"] = bool(r.get("quote")) and not fuzzy_quote_ok(r["quote"], corpus)
            out.append(r)
        return out

    names = {t["name"] for t in position.get("triggers") or []}
    return {
        "stage": position.get("stage"),
        "theses": checked(result.get("theses") or [], len(position.get("theses") or [])),
        "answers": checked(result.get("answers") or [], len(position.get("questions") or [])),
        "triggers": [t for t in result.get("triggers") or [] if t.get("name") in names],
        "new_concerns": result.get("new_concerns") or [],
        "fingerprint": review_fingerprint(position, doc),
        "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": llm.model,
    }
