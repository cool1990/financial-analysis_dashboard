from __future__ import annotations

import argparse
import json
import sys

from pipeline.commands.backfill_cmd import backfill
from pipeline.commands.init_cmd import init_ticker
from pipeline.commands.poll_cmd import poll_once
from pipeline.commands.run_cmd import run_pipeline
from pipeline.commands.snapshot_cmd import take_all_snapshots, take_snapshot
from pipeline.config import list_tickers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline", description="财报解读 MVP pipeline")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="初始化股票：CIK + EPS 口径识别")
    p_init.add_argument("--ticker", required=True)
    p_init.add_argument("--force", action="store_true")

    p_snap = sub.add_parser("snapshot", help="保存一致预期快照")
    p_snap.add_argument("--ticker", default=None, help="缺省则全部股票")

    p_poll = sub.add_parser("poll", help="财报窗口轮询 / 文字稿获取")

    p_run = sub.add_parser("run", help="按阶段重跑")
    p_run.add_argument("--ticker", required=True)
    p_run.add_argument("--period", default=None)
    p_run.add_argument("--stage", type=int, required=True, choices=[1, 2, 3])
    p_run.add_argument("--accession", default=None, help="指定 8-K accession 模拟/重跑")

    p_bf = sub.add_parser("backfill", help="历史回补")
    p_bf.add_argument("--ticker", required=True)
    p_bf.add_argument("--from", dest="from_period", default=None)
    p_bf.add_argument("--limit", type=int, default=8)

    p_val = sub.add_parser("validate", help="校验已有季度 JSON")
    p_val.add_argument("--ticker", required=True)
    p_val.add_argument("--period", required=True)

    p_build = sub.add_parser("build", help="生成静态页面到 site/")
    p_build.add_argument("--ticker", default=None)

    args = parser.parse_args(argv)

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
        print(json.dumps(poll_once(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "run":
        doc = run_pipeline(args.ticker, args.period, args.stage, accession=args.accession)
        print(json.dumps({"period": doc["meta"]["fiscal_period"], "stage": doc["status"]["stage"], "warnings": doc["status"]["warnings"]}, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "backfill":
        periods = backfill(args.ticker, args.from_period, limit=args.limit)
        print(json.dumps({"written": periods}, ensure_ascii=False, indent=2))
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
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
