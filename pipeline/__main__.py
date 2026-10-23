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
    sub.add_parser("stage3-recent", help="已停用：空操作（旧 daily 兼容）")
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
    p_run.add_argument("--stage", type=int, required=True, choices=[1, 2, 3], help="1=提取 2=总结；3 已停用（空操作）")
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
    p_bf.add_argument("--xbrl-only", action="store_true", help="只写 SEC companyfacts 单季 GAAP，不调用模型，不覆盖已有季度")
    p_bf.add_argument("--press-only", action="store_true", help="ADR：只解析 6-K 新闻稿里的官方美元和 US$ per ADR，不调用模型，不覆盖已有季度")
    p_bf.add_argument("--start", default="2016-01-01", help="xbrl-only / press-only 的起始日期")
    p_bf.add_argument("--model", default=None)

    p_val = sub.add_parser("validate", help="校验已有季度 JSON")
    p_val.add_argument("--ticker", required=True)
    p_val.add_argument("--period", required=True)

    p_hist = sub.add_parser("history", help="生成分部、报表和 XBRL 活动历史")
    p_hist.add_argument("--ticker", default=None, help="一只股票。与 --if-new 合用时只检查这一只")
    p_hist.add_argument("--if-new", action="store_true", help="仅在出现新的 10-Q/10-K，或历史文件还不齐时刷新")
    p_hist.add_argument(
        "--from-periods",
        action="store_true",
        help="不改季度 JSON。指定美股时用 SEC 重建分部、报表、股本和应收；港股和 ADR 从已有季度汇总，缺的留空。不指定 ticker 时离线处理，并保留已有的 META/TSM 历史",
    )
    p_hist.add_argument("--start", default="2016-01-01", help="季度结束日的起点")

    p_build = sub.add_parser("build", help="生成静态页面到 site/")
    p_build.add_argument("--ticker", default=None)
    p_build.add_argument("--out", default=None, help="输出目录。缺省写到 site/")

    p_av = sub.add_parser("av-estimates", help="用 EARNINGS_ESTIMATES 写入 data/{TICKER}/consensus/av_estimates.json")
    p_av.add_argument("--ticker", default=None, help="一只，或逗号分隔。缺省则全部股票")

    p_tv = sub.add_parser("tv-scan", help="用 TradingView scanner 写入 data/{TICKER}/consensus/tradingview_scan.json")
    p_tv.add_argument("--ticker", default=None, help="一只，或逗号分隔。缺省则全部股票")

    p_fx = sub.add_parser("fx-refresh", help="写入当天 HKDCNY（1 港币折合人民币），来自 Yahoo HKDCNY=X")
    p_fx.add_argument("--date", default=None, help="YYYY-MM-DD，缺省为香港当天")

    p_imp = sub.add_parser("import-period", help="港股手工导入业绩公告 PDF 与本地电话会")
    p_imp.add_argument("--ticker", required=True)
    p_imp.add_argument("--period", required=True, help="例如 FY2026Q1。中期六个月不会写成 Q2")
    p_imp.add_argument("--pdf", default=None, help="业绩公告 PDF（或已抽出的 .txt）")
    p_imp.add_argument("--transcript", default=None, help="电话会 markdown / 文本，写入 raw/{period}/transcript.txt")

    p_hk = sub.add_parser("hkex-poll", help="披露易业绩公布试跑（默认只列链接，不下载，不接每天的 poll）")
    p_hk.add_argument("--ticker", required=True, help="目前只支持 0700.HK")
    p_hk.add_argument("--since", default=None, help="YYYY-MM-DD，缺省为香港今天往前 18 个月")
    p_hk.add_argument("--until", default=None, help="YYYY-MM-DD，缺省为香港今天")
    p_hk.add_argument("--limit", type=int, default=20, help="最多输出多少条业绩公布")
    p_hk.add_argument("--download", action="store_true", help="把 PDF 写到 data/{ticker}/raw/hkex/；默认不落盘")

    p_ping = sub.add_parser("llm-ping", help="校验 OpenRouter 模型 slug 与 API key")
    p_ping.add_argument("--model", default=None, help="模型 slug，默认取 settings.yaml")

    p_replay = sub.add_parser("replay-filings", help="回放 ADR 6-K 业绩过滤器（不调用 LLM）")
    p_replay.add_argument("--ticker", required=True)

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
                    reported = ",".join(out.get("reported") or [])
                    f.write(f"did_work={'true' if out.get('did_work') else 'false'}\n")
                    f.write(f"reported={reported}\n")
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
            if not args.dry_run and not args.xbrl_only and not args.press_only:
                require_openrouter_key(context="backfill")
            out = backfill(
                args.ticker,
                args.from_period,
                limit=args.limit,
                dry_run=args.dry_run,
                model=args.model,
                xbrl_only=args.xbrl_only,
                press_only=args.press_only,
                start=args.start,
            )
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
        if args.cmd == "history":
            from pipeline.commands.history_cmd import (
                refresh_history,
                refresh_history_from_periods,
                refresh_history_if_new,
            )

            if args.if_new and args.from_periods:
                print("不要同时使用 --if-new 和 --from-periods", file=sys.stderr)
                return 2
            if args.from_periods:
                out = refresh_history_from_periods(args.ticker, start=args.start)
            elif args.if_new:
                out = refresh_history_if_new(args.ticker, start=args.start)
            elif args.ticker:
                out = refresh_history(args.ticker, start=args.start)
            else:
                print("需要 --ticker，或加上 --if-new / --from-periods", file=sys.stderr)
                return 2
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "build":
            from builder.build import build_site

            paths = build_site(args.ticker, out_dir=args.out)
            print(json.dumps({"pages": [str(p) for p in paths]}, ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "fx-refresh":
            from datetime import datetime
            from zoneinfo import ZoneInfo

            from pipeline.sources.fx import fetch_hkd_cny

            day = args.date or datetime.now(ZoneInfo("Asia/Hong_Kong")).date().isoformat()
            record = fetch_hkd_cny(day)
            if record is None:
                print(json.dumps({"pair": "HKDCNY", "date": day, "rate": None}, ensure_ascii=False))
                return 1
            print(json.dumps(record, ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "import-period":
            from pathlib import Path

            from pipeline.commands.import_cmd import import_period

            out = import_period(
                args.ticker,
                args.period,
                pdf=Path(args.pdf) if args.pdf else None,
                transcript=Path(args.transcript) if args.transcript else None,
            )
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "hkex-poll":
            from pipeline.commands.hkex_poll_cmd import hkex_poll
            from pipeline.sources.hkex_news import HkexError

            try:
                out = hkex_poll(
                    args.ticker,
                    since=args.since,
                    until=args.until,
                    limit=args.limit,
                    download=bool(args.download),
                )
            except HkexError as exc:
                print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2))
                return 1
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0 if out.get("ok") else 1
        if args.cmd == "av-estimates":
            from pipeline.commands.av_estimates_cmd import refresh_earnings_estimates

            raw = args.ticker or ""
            tickers = [part.strip() for part in raw.split(",") if part.strip()] or None
            out = refresh_earnings_estimates(tickers)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0 if out.get("ok") else 1
        if args.cmd == "tv-scan":
            from pipeline.commands.tv_scan_cmd import refresh_tv_quotes

            raw = args.ticker or ""
            tickers = [part.strip() for part in raw.split(",") if part.strip()] or None
            out = refresh_tv_quotes(tickers)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0 if out.get("ok") else 1
        if args.cmd == "replay-filings":
            from pipeline.commands.replay_cmd import replay_earnings_filters

            out = replay_earnings_filters(args.ticker)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0 if out.get("ok") else 1
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
