from __future__ import annotations

import argparse
import json
import os
import sys

from pipeline.commands.backfill_cmd import backfill
from pipeline.commands.eval_cmd import run_eval
from pipeline.commands.init_cmd import init_ticker
from pipeline.commands.llm_ping_cmd import llm_ping, require_openrouter_key
from pipeline.commands.period_resolve import resolve_latest_period
from pipeline.commands.poll_cmd import poll_once
from pipeline.commands.run_cmd import refresh_comparatives, refresh_recent_stage3, run_pipeline, run_thesis_review
from pipeline.commands.snapshot_cmd import take_all_snapshots, take_snapshot
from pipeline.llm import write_github_step_summary, reset_run_tracker, get_run_tracker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline", description="财报解读 MVP pipeline")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="初始化股票：CIK + EPS 口径识别")
    p_init.add_argument("--ticker", required=True)
    p_init.add_argument("--force", action="store_true")

    p_snap = sub.add_parser("snapshot", help="保存一致预期快照")
    p_snap.add_argument("--ticker", default=None, help="缺省则全部股票")

    p_poll = sub.add_parser("poll", help="财报窗口轮询 / 文字稿获取")
    sub.add_parser("init-missing", help="给缺 CIK 的股票自动初始化（不调用 LLM）")
    sub.add_parser("stage3-recent", help="刷新近期财报的股价反应与分析师修正（无 LLM）")
    p_th = sub.add_parser("thesis", help="评估投资论点（1 次小调用；论点和数据都没变时跳过）")
    p_th.add_argument("--ticker", required=True)
    p_th.add_argument("--period", default="latest")
    p_th.add_argument("--model", default=None)
    p_th.add_argument("--force", action="store_true")
    p_cmp = sub.add_parser("comparatives", help="用新闻稿对比列补算同比 / 环比（无 LLM、不联网）")
    p_cmp.add_argument("--ticker", default=None)

    p_run = sub.add_parser("run", help="按阶段重跑")
    p_run.add_argument("--ticker", required=True)
    p_run.add_argument("--period", default=None, help="财季，或 latest")
    p_run.add_argument("--stage", type=int, required=True, choices=[1, 2, 3])
    p_run.add_argument("--accession", default=None, help="指定 8-K accession 模拟/重跑")
    p_run.add_argument("--model", default=None, help="覆盖 settings.yaml 中的模型")
    p_run.add_argument(
        "--force",
        action="store_true",
        help="Stage2：即使已有完整产物也强制重跑 LLM（默认跳过已完成）",
    )

    p_bf = sub.add_parser("backfill", help="历史回补")
    p_bf.add_argument("--ticker", required=True)
    p_bf.add_argument("--from", dest="from_period", default=None)
    p_bf.add_argument("--limit", type=int, default=8)
    p_bf.add_argument("--dry-run", action="store_true", help="只统计 LLM 调用与预估花费")
    p_bf.add_argument("--model", default=None)

    p_val = sub.add_parser("validate", help="校验已有季度 JSON")
    p_val.add_argument("--ticker", required=True)
    p_val.add_argument("--period", required=True)

    p_build = sub.add_parser("build", help="生成静态页面到 site/")
    p_build.add_argument("--ticker", default=None)

    p_ping = sub.add_parser("llm-ping", help="校验 OpenRouter 模型 slug 与 API key")
    p_ping.add_argument("--model", default=None, help="模型 slug，默认取 settings.yaml")

    p_eval = sub.add_parser("eval", help="多模型评测（仅 Actions）")
    p_eval.add_argument("--models", required=True, help="逗号分隔模型 slug")
    p_eval.add_argument("--samples", required=True, help="逗号分隔样本，如 MU 或 MU:FY2025Q4")

    args = parser.parse_args(argv)
    reset_run_tracker()

    if getattr(args, "model", None):
        os.environ["PIPELINE_LLM_MODEL"] = args.model

    try:
        if args.cmd == "init":
            cfg = init_ticker(args.ticker, force=args.force)
            print(json.dumps({"ticker": args.ticker, "cik": cfg.get("cik"), "eps_basis": cfg.get("eps_basis")}, ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "snapshot":
            if args.ticker:
                out = take_snapshot(args.ticker)
            else:
                out = take_all_snapshots()
            print(json.dumps(out if isinstance(out, dict) and "snapshot_at" not in out else {"ok": True}, ensure_ascii=False, indent=2)[:2000])
            return 0
        if args.cmd == "poll":
            out = poll_once()
            print(json.dumps(out, ensure_ascii=False, indent=2))
            gh_out = os.environ.get("GITHUB_OUTPUT")
            if gh_out:
                with open(gh_out, "a", encoding="utf-8") as f:
                    f.write(f"did_work={'true' if out.get('did_work') else 'false'}\n")
            write_github_step_summary()
            return 0
        if args.cmd == "init-missing":
            from pipeline.commands.init_cmd import init_missing

            print(json.dumps(init_missing(), ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "thesis":
            period = resolve_latest_period(args.ticker) if args.period.lower() == "latest" else args.period
            doc = run_thesis_review(args.ticker, period, force=args.force)
            print(json.dumps(doc.get("thesis_review") or {}, ensure_ascii=False, indent=2)[:4000])
            write_github_step_summary()
            return 0
        if args.cmd == "comparatives":
            print(json.dumps(refresh_comparatives(args.ticker), ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "stage3-recent":
            out = refresh_recent_stage3()
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "run":
            period = args.period
            if period and period.lower() == "latest":
                if args.stage == 1:
                    period = None  # stage1 自动从最新 8-K 推断
                else:
                    period = resolve_latest_period(args.ticker)
            doc = run_pipeline(
                args.ticker,
                period,
                args.stage,
                accession=args.accession,
                force=bool(getattr(args, "force", False)),
            )
            print(json.dumps({"period": doc["meta"]["fiscal_period"], "stage": doc["status"]["stage"], "warnings": doc["status"]["warnings"]}, ensure_ascii=False, indent=2))
            write_github_step_summary(doc["status"].get("warnings"))
            return 0
        if args.cmd == "backfill":
            if not args.dry_run:
                require_openrouter_key(context="backfill")
            out = backfill(args.ticker, args.from_period, limit=args.limit, dry_run=args.dry_run, model=args.model)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            write_github_step_summary()
            return 0
        if args.cmd == "validate":
            from pipeline.state import load_period_json

            doc = load_period_json(args.ticker, args.period)
            if not doc:
                print("not found", file=sys.stderr)
                return 1
            print(json.dumps(doc.get("status"), ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "build":
            from builder.build import build_site

            paths = build_site(args.ticker)
            print(json.dumps({"pages": [str(p) for p in paths]}, ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "llm-ping":
            require_openrouter_key(context="llm-ping")
            out = llm_ping(args.model)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            write_github_step_summary()
            return 0
        if args.cmd == "eval":
            require_openrouter_key(context="eval")
            models = [m.strip() for m in args.models.split(",") if m.strip()]
            samples = [s.strip() for s in args.samples.split(",") if s.strip()]
            out = run_eval(models, samples)
            print(json.dumps({"report_path": out["report_path"], "models": out["models"]}, ensure_ascii=False, indent=2))
            write_github_step_summary()
            return 0
    except Exception as e:
        get_run_tracker().warnings.append(str(e))
        write_github_step_summary()
        raise
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
