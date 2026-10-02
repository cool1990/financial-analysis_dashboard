from __future__ import annotations

from typing import Any

from pipeline.llm import LLMClient
from pipeline.schemas import DriversResult
from pipeline.validate import fuzzy_quote_ok


def analyze_drivers(
    metrics_table: dict[str, Any],
    press_text: str,
    prepared_remarks: str = "",
    qa_text: str = "",
    *,
    stage: int = 1,
) -> dict[str, Any]:
    llm = LLMClient()
    prompt = llm.load_prompt("analyze_drivers.md")
    user = (
        prompt
        + "\n\n指标表:\n"
        + __import__("json").dumps(metrics_table, ensure_ascii=False)
        + "\n\n新闻稿:\n"
        + press_text[:20000]
        + "\n\n管理层发言:\n"
        + prepared_remarks[:15000]
        + "\n\nQ&A:\n"
        + qa_text[:15000]
    )
    result = llm.complete_json("analyze_drivers.md", user, DriversResult)
    data = result.model_dump()
    warnings: list[str] = []
    corpus = {
        "press_release": press_text,
        "prepared_remarks": prepared_remarks,
        "qa": qa_text,
    }
    for metric in data.get("metrics", []):
        for drv in metric.get("drivers", []):
            src = drv.get("source") or "press_release"
            quote = drv.get("evidence_quote")
            text = corpus.get(src, press_text)
            if quote and not fuzzy_quote_ok(quote, text):
                drv["is_inference"] = True
                warnings.append(f"driver quote 未匹配: {metric.get('metric')}")
    return {"stage": stage, "metrics": data.get("metrics", []), "warnings": warnings}
