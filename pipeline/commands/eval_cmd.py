from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline import ROOT
from pipeline.commands.llm_ping_cmd import llm_ping
from pipeline.config import data_dir, load_ticker_config, load_settings
from pipeline.extract.press_release import load_press_release
from pipeline.llm import LLMClient, LLMError, get_run_tracker, redact_secrets, reset_run_tracker
from pipeline.schemas import ExtractFinancials, GuidanceItem, QAItem
from pipeline.validate import validate_extraction


EXPECTED_DIR = ROOT / "tests" / "fixtures" / "expected"
REPORTS_DIR = ROOT / "reports"


def _load_expected(ticker: str, period: str) -> dict[str, Any] | None:
    path = EXPECTED_DIR / f"{ticker.upper()}_{period}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_sample(sample: str) -> tuple[str, str]:
    """sample 形如 MU 或 MU:FY2025Q4。仅 ticker 时取 expected 下最新/唯一文件。"""
    if ":" in sample:
        t, p = sample.split(":", 1)
        return t.upper(), p
    ticker = sample.upper()
    files = sorted(EXPECTED_DIR.glob(f"{ticker}_*.json"))
    if not files:
        # fallback: data folder
        data_files = sorted((data_dir(ticker)).glob("FY*.json"), reverse=True)
        if data_files:
            return ticker, data_files[0].stem
        raise FileNotFoundError(f"样本 {ticker} 无标准答案且无 data/ 季度文件")
    return ticker, files[-1].stem.split("_", 1)[1]


def _compare_numbers(actual: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    fields = expected.get("numbers") or {}
    hits = 0
    total = 0
    details = []
    for key, exp_val in fields.items():
        total += 1
        # actual may nest under financials or top-level extract
        got = None
        if key in actual:
            node = actual[key]
            got = node.get("value") if isinstance(node, dict) else node
        fin = (actual.get("financials") or {}) if isinstance(actual, dict) else {}
        if got is None and key in fin:
            node = fin[key]
            got = node.get("value") if isinstance(node, dict) else node
        # also allow extract-style raw parsed in expected compare via _parsed
        parsed = actual.get("_parsed") or {}
        if got is None and key in parsed:
            got = parsed[key]
        ok = False
        if got is not None and exp_val is not None:
            try:
                ok = abs(float(got) - float(exp_val)) <= max(0.02, abs(float(exp_val)) * 0.005)
            except (TypeError, ValueError):
                ok = False
        if ok:
            hits += 1
        details.append({"field": key, "expected": exp_val, "actual": got, "ok": ok})
    return {"hits": hits, "total": total, "accuracy": (hits / total if total else None), "details": details}


def _guidance_recall(pred_items: list[dict[str, Any]], expected_keys: list[str]) -> dict[str, Any]:
    got = {i.get("metric_key") for i in pred_items if i.get("metric_key")}
    exp = set(expected_keys)
    if not exp:
        return {"recall": None, "expected": [], "got": sorted(got)}
    hit = len(got & exp)
    return {"recall": hit / len(exp), "expected": sorted(exp), "got": sorted(got), "hits": hit}


def _stable(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def run_eval(
    models: list[str],
    samples: list[str],
    *,
    runs_per_model: int = 2,
) -> dict[str, Any]:
    reset_run_tracker()
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"eval_{day}.md"

    # ping models first
    valid_models: list[str] = []
    ping_notes: list[str] = []
    for m in models:
        try:
            llm_ping(m)
            valid_models.append(m)
            ping_notes.append(f"- `{m}`: ping OK")
        except Exception as e:
            ping_notes.append(f"- `{m}`: SKIP — {e}")

    sections: list[str] = [
        f"# 模型评测报告 {day}",
        "",
        "## 模型可用性",
        *ping_notes,
        "",
    ]

    results: list[dict[str, Any]] = []

    for sample in samples:
        try:
            ticker, period = _resolve_sample(sample)
        except Exception as e:
            sections.append(f"## 样本 `{sample}`\n跳过：{e}\n")
            continue
        expected = _load_expected(ticker, period)
        if not expected:
            sections.append(f"## 样本 `{ticker} {period}`\n跳过：缺少标准答案 `tests/fixtures/expected/{ticker}_{period}.json`\n")
            continue

        # locate press release / transcript
        raw_dir = data_dir(ticker) / "raw" / period
        alt_raw = data_dir(ticker) / "raw" / "_init"
        press_path = raw_dir / "press_release.html"
        if not press_path.exists():
            press_path = alt_raw / "press_release.html"
        if not press_path.exists():
            # try fixture html
            press_path = ROOT / "tests" / "fixtures" / "mu_press_snip.html"
        parsed = load_press_release(press_path)
        press_text = parsed["combined"]
        transcript_path = raw_dir / "transcript.txt"
        transcript = transcript_path.read_text(encoding="utf-8") if transcript_path.exists() else None

        cfg = load_ticker_config(ticker)
        sections.append(f"## 样本 `{ticker} {period}`")
        sections.append("")

        for model in valid_models:
            run_outputs: list[dict[str, Any]] = []
            json_failures = 0
            retries = 0
            v1_pass = 0
            v1_total = 0
            for run_i in range(runs_per_model):
                client = LLMClient(model=model, use_cache=False, require_key=True)
                out: dict[str, Any] = {"run": run_i + 1, "model": model}
                try:
                    kpis = json.dumps(cfg.get("kpis") or [], ensure_ascii=False)
                    prompt = client.load_prompt("extract_financials.md", kpis=kpis)
                    fin = client.complete_json(
                        "extract_financials.md",
                        prompt + "\n\n----\n" + press_text[:80000],
                        ExtractFinancials,
                    ).model_dump()
                    out["financials"] = fin
                    # parse numeric for compare
                    from pipeline.extract.numbers import ParsedAmount

                    parsed_nums = {
                        "revenue": ParsedAmount.from_dict(fin.get("revenue")).value,
                        "eps_gaap": ParsedAmount.from_dict(fin.get("eps_gaap_diluted"), is_eps=True).value,
                        "eps_nongaap": ParsedAmount.from_dict(fin.get("eps_nongaap_diluted"), is_eps=True).value,
                    }
                    out["_parsed"] = parsed_nums
                    v = validate_extraction(fin, press_text)
                    v1_total += 1
                    if not v["needs_review"]:
                        v1_pass += 1
                    out["validation"] = v
                except Exception as e:
                    json_failures += 1
                    out["financials_error"] = str(e)
                try:
                    from pipeline.config import load_guidance_keys
                    from pipeline.extract.guidance import parse_guidance_item

                    g_prompt = client.load_prompt(
                        "extract_guidance.md",
                        guidance_keys=", ".join(load_guidance_keys()),
                    )
                    items = client.complete_json_list(
                        "extract_guidance.md",
                        g_prompt + "\n\n----\n" + press_text[:80000],
                        GuidanceItem,
                    )
                    out["guidance"] = [parse_guidance_item(i.model_dump()) for i in items]
                except Exception as e:
                    json_failures += 1
                    out["guidance_error"] = str(e)

                if transcript:
                    try:
                        from pipeline.config import load_topics

                        topics = load_topics()
                        topic_list = topics.get("approved", []) + topics.get("pending_review", [])
                        qa_prompt = client.load_prompt(
                            "structure_qa.md",
                            topics=json.dumps(topic_list, ensure_ascii=False),
                            press_release_numbers=json.dumps(out.get("_parsed") or {}, ensure_ascii=False),
                        )
                        # feed whole transcript as one batch for eval simplicity
                        batch = [{"exchange_id": "1", "text": transcript[:20000]}]
                        qa_items = client.complete_json_list(
                            "structure_qa.md",
                            qa_prompt + "\n\n" + json.dumps(batch, ensure_ascii=False),
                            QAItem,
                        )
                        out["qa"] = [q.model_dump() for q in qa_items]
                    except Exception as e:
                        json_failures += 1
                        out["qa_error"] = str(e)

                run_outputs.append(out)

            # metrics across runs
            num_cmp = _compare_numbers(run_outputs[0], expected) if run_outputs else {"accuracy": None}
            g_keys = expected.get("guidance_keys") or []
            g_recall = _guidance_recall(run_outputs[0].get("guidance") or [], g_keys) if run_outputs else {}
            stable = _stable(
                {k: run_outputs[0].get(k) for k in ("_parsed", "guidance")} if run_outputs else {},
                {k: run_outputs[1].get(k) for k in ("_parsed", "guidance")} if len(run_outputs) > 1 else {},
            ) if len(run_outputs) > 1 else None

            tracker = get_run_tracker().summary()
            block = {
                "ticker": ticker,
                "period": period,
                "model": model,
                "number_accuracy": num_cmp.get("accuracy"),
                "number_details": num_cmp.get("details"),
                "v1_pass_rate": (v1_pass / v1_total) if v1_total else None,
                "guidance_recall": g_recall.get("recall"),
                "json_failures": json_failures,
                "stable_across_runs": stable,
                "usage": tracker,
                "qa_side_by_side": [r.get("qa") for r in run_outputs],
                "summary_side_by_side": [r.get("summary") for r in run_outputs],
            }
            results.append(block)

            sections.append(f"### 模型 `{model}`")
            sections.append(
                f"- 数字准确率: {block['number_accuracy']}\n"
                f"- V1 通过率: {block['v1_pass_rate']}\n"
                f"- 指引召回率: {block['guidance_recall']}\n"
                f"- JSON 失败次数: {json_failures}\n"
                f"- 两次运行一致: {stable}\n"
                f"- 累计 token: in={tracker['prompt_tokens']} out={tracker['completion_tokens']} "
                f"花费≈${tracker['estimated_cost_usd']:.4f}"
            )
            sections.append("")
            sections.append("数字比对明细：")
            sections.append("```json")
            sections.append(json.dumps(num_cmp.get("details"), ensure_ascii=False, indent=2))
            sections.append("```")
            if any(r.get("qa") for r in run_outputs):
                sections.append("")
                sections.append("Q&A 并排（人工判断）：")
                for i, r in enumerate(run_outputs, 1):
                    sections.append(f"**Run {i}**")
                    sections.append("```json")
                    sections.append(json.dumps(r.get("qa"), ensure_ascii=False, indent=2)[:4000])
                    sections.append("```")
            sections.append("")

    report = "\n".join(sections) + "\n"
    report_path.write_text(report, encoding="utf-8")
    return {"report_path": str(report_path), "results": results, "models": valid_models}
