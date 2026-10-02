from __future__ import annotations

from collections import Counter
from typing import Any


def compute_topic_stats(
    current_items: list[dict[str, Any]],
    prior_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    cur = Counter((i.get("topic") or "其他") for i in current_items)
    pri = Counter((i.get("topic") or "其他") for i in (prior_items or []))
    new_topics = sorted(set(cur) - set(pri))
    dropped_topics = sorted(set(pri) - set(cur))
    deltas = {t: cur[t] - pri.get(t, 0) for t in cur}
    hot = sorted(deltas, key=lambda t: deltas[t], reverse=True)[:3]
    evasive = [
        i
        for i in current_items
        if (i.get("directness") or "") in {"partial", "evasive"} and not i.get("parse_failed")
    ]
    return {
        "topic_stats": dict(cur),
        "prior_topic_stats": dict(pri),
        "new_topics": new_topics,
        "dropped_topics": dropped_topics,
        "hot_topics": hot,
        "evasive_list": evasive,
    }
