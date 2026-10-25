"""首页「今日修正」「RSI信号」「是否触发」：对齐 Hermes 美港股盈利跟踪简要总结。

权威逻辑：Hermes ``us_hk_earnings_tracker.py`` 的 build_brief /
revision_signal_current_q / Fwd PE≤目标PE。不是投资日报里的标普 RSI。

数据只读本仓库：``data/earnings_tracker/盈利跟踪_*.md``、
``data/market_brief.json``、``config/stock_pool.csv``、各票 TV/AV JSON。
不读 Mac Obsidian vault。笔记缺失或过期时，用仓库内 TV/AV/pe_target 现算。

首页展示为紧凑三栏（每票短句 / RSI 两行 / 触发名单），脚注保留数据日来源。
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from pipeline import ROOT
from pipeline.compute.revision_signal import (
    chg_30d_pct,
    choose_current_estimate,
    earnings_drive_tag,
)
from pipeline.config import list_tickers, load_settings, load_ticker_config, repo_path
from pipeline.sources.av_estimates import load_earnings_record

_NOTE_NAME = re.compile(r"^盈利跟踪_(\d{4}-\d{2}-\d{2})\.md$")
_BRIEF_REVISION = re.compile(r"^1[、.．]\s*今日修正[：:]\s*(.+)$")
_BRIEF_RSI = re.compile(r"^2[、.．]\s*RSI信号[：:]\s*(.+)$")
_BRIEF_VALUATION = re.compile(r"^3[、.．]\s*估值触发[：:]\s*(.+)$")
_SIGNAL_BASE = re.compile(r"^([^/]+)")


def _signal_base(label: str | None) -> str:
    text = str(label or "").strip()
    if not text:
        return ""
    # 表列可能是「强上修/财报驱动」；今日修正对比只看基标签
    return text.split("/", 1)[0].strip()


def hermes_revision_signal(
    eps: Any,
    coverage: Any,
    chg: Any,
    up: Any,
    down: Any,
) -> str:
    """Hermes revision_signal_current_q（不含 /财报驱动 后缀）。

    顺序：无数据 → eps低基数 → 样本不足 → 强上/强下 → 少数上/下 → 中性。
    少数：|chg|≥5% 且 |净修正人数| < 2（与站点表列 revision_signal 的 ≥2 口径不同）。
    chg 为小数比例（0.05 = 5%）。
    """
    if eps is None:
        return "无数据"
    try:
        eps_f = float(eps)
    except (TypeError, ValueError):
        return "无数据"
    up_f = 0.0 if up is None else float(up)
    down_f = 0.0 if down is None else float(down)
    if coverage is None:
        return "样本不足"
    try:
        cov_f = float(coverage)
    except (TypeError, ValueError):
        return "样本不足"
    net = up_f - down_f
    net_over = (net / cov_f) if cov_f else None
    if abs(eps_f) < 0.1:
        return "eps低基数"
    if cov_f < 3 or (up_f + down_f) == 0:
        return "样本不足"
    chg_f: float | None
    try:
        chg_f = None if chg is None else float(chg)
    except (TypeError, ValueError):
        chg_f = None
    if chg_f is not None and chg_f >= 0.05 and net_over is not None and net_over > 0.10:
        return "强上修"
    if chg_f is not None and chg_f <= -0.05 and net_over is not None and (-net / cov_f) > 0.10:
        return "强下修"
    if chg_f is not None and abs(chg_f) >= 0.05 and abs(net) < 2:
        return "少数上修" if chg_f > 0 else "少数下修"
    return "中性"


def note_search_roots() -> list[Path]:
    """笔记目录：仅仓库内 data/earnings_tracker（可测时用 EARNINGS_TRACKER_NOTES 覆盖）。

    不读 Mac Obsidian vault / Hermes 工作区；日更需同步进仓库后再建站。
    """
    roots: list[Path] = []
    env = (os.environ.get("EARNINGS_TRACKER_NOTES") or "").strip()
    if env:
        roots.append(Path(env).expanduser())
    settings = load_settings() or {}
    configured = ((settings.get("paths") or {}).get("earnings_tracker_notes") or "").strip()
    if configured:
        # 仅允许仓库内相对路径，避免运行时扫 vault
        cfg_path = Path(configured).expanduser()
        try:
            cfg_path.resolve().relative_to(ROOT.resolve())
            roots.append(cfg_path)
        except ValueError:
            pass
    roots.append(repo_path("data", "earnings_tracker"))
    seen: set[Path] = set()
    out: list[Path] = []
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            resolved = root
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(root)
    return out


def find_latest_note(roots: list[Path] | None = None) -> Path | None:
    best: tuple[date, Path] | None = None
    for root in roots or note_search_roots():
        if not root.is_dir():
            continue
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for path in entries:
            match = _NOTE_NAME.match(path.name)
            if not match or not path.is_file():
                continue
            try:
                day = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            if best is None or day > best[0] or (day == best[0] and path.stat().st_mtime > best[1].stat().st_mtime):
                best = (day, path)
    return best[1] if best else None


def parse_brief_from_note(path: Path) -> dict[str, Any] | None:
    """解析笔记开头简要总结第 1–2 条。失败返回 None。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _NOTE_NAME.match(path.name)
    data_date = match.group(1) if match else ""
    # frontmatter data_date 优先
    fm = re.search(r"^data_date:\s*[\"']?(\d{4}-\d{2}-\d{2})", text, re.M)
    if fm:
        data_date = fm.group(1)
    revision_body = ""
    rsi_body = ""
    valuation_body = ""
    in_brief = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## ") and "简要总结" in stripped:
            in_brief = True
            continue
        if in_brief and stripped.startswith("## "):
            break
        if in_brief and stripped.startswith("|"):
            break
        if not in_brief:
            continue
        rev = _BRIEF_REVISION.match(stripped)
        if rev:
            revision_body = rev.group(1).rstrip("。").strip()
            continue
        rsi = _BRIEF_RSI.match(stripped)
        if rsi:
            rsi_body = rsi.group(1).rstrip("。").strip()
            continue
        val = _BRIEF_VALUATION.match(stripped)
        if val:
            valuation_body = val.group(1).rstrip("。").strip()
            continue
    if not revision_body and not rsi_body and not valuation_body:
        return None
    names = _parse_valuation_names(valuation_body)
    rsi_fallback = rsi_body or "低于30 无；高于70 无"
    display = _display_texts(
        revision_body=revision_body or "无",
        rsi_body=rsi_fallback,
        valuation_body=valuation_body or "无",
        valuation_names=names,
    )
    return {
        "data_date": data_date,
        "source": "note",
        "source_path": str(path),
        "revision_body": revision_body or "无",
        "rsi_body": rsi_fallback,
        "valuation_body": valuation_body or "无",
        **display,
    }



def _parse_valuation_names(body: str) -> list[str]:
    """从「MU（5.03<6）, SNDK（5.82<6）」抽出 ticker 列表。"""
    text = (body or "").strip()
    if not text or text in {"无", "无。"}:
        return []
    names: list[str] = []
    for part in re.split(r"[,，;；]", text):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^([A-Za-z0-9.\-]+)", part)
        if m:
            names.append(m.group(1).upper() if m.group(1) != "0700.HK" else "0700.HK")
    return names


def valuation_triggered(forward_pe: float | None, target: float | None) -> bool:
    """Hermes：Fwd PE ≤ 目标PE。"""
    if forward_pe is None or target is None:
        return False
    return float(forward_pe) <= float(target)

def _parse_rsi_side(body: str, label: str) -> list[str]:
    if not body:
        return []
    # 「低于30 MU, SNDK；高于70 无」或「低于30：MU、SNDK」
    parts = re.split(r"[；;\n]", body)
    for part in parts:
        part = part.strip()
        if not part.startswith(label):
            continue
        rest = part[len(label) :].strip().lstrip("：:").strip()
        if not rest or rest == "无":
            return []
        return [t.strip() for t in re.split(r"[,，、\s]+", rest) if t.strip() and t.strip() != "无"]
    return []





def _strip_revision_detail(line: str) -> str:
    """去掉「（当前季EPS …）」括号细节，只留 TICKER：旧——新。"""
    s = (line or "").strip().rstrip("。").strip()
    if not s or s in {"无", "无。"}:
        return ""
    # 全角/半角括号细节
    s = re.sub(r"[（(][^）)]*[）)]", "", s).strip()
    return s.rstrip("，,；;").strip()


def compact_revision_lines(body: str) -> list[str]:
    """笔记或现算的修正正文 → 每票一行短句。"""
    text = (body or "").strip().rstrip("。").strip()
    text = re.sub(r"^今日修正[：:]\s*", "", text).strip()
    if not text or text in {"无", "无。"}:
        return []
    parts = re.split(r"[；;\n]", text)
    out: list[str] = []
    for part in parts:
        short = _strip_revision_detail(part)
        if short and short not in {"无"}:
            out.append(short)
    return out


def compact_rsi_lines(body: str = "", *, low: list[str] | None = None, high: list[str] | None = None) -> tuple[str, str]:
    """返回 (低于30行, 高于70行)，形如「低于30：无」「高于70：MU」。"""
    if low is None:
        low = _parse_rsi_side(body, "低于30")
    if high is None:
        high = _parse_rsi_side(body, "高于70")
    low_s = "、".join(low) if low else "无"
    high_s = "、".join(high) if high else "无"
    return f"低于30：{low_s}", f"高于70：{high_s}"


def compact_valuation_text(names: list[str] | None = None, body: str = "") -> str:
    """触发名单 → 「触发：A、B」或「无」。"""
    names = list(names or [])
    if not names and body:
        names = _parse_valuation_names(body)
    if not names:
        return "无"
    return "触发：" + "、".join(names)


def _display_texts(
    *,
    revision_body: str,
    rsi_body: str,
    valuation_body: str,
    valuation_names: list[str] | None = None,
    rsi_low: list[str] | None = None,
    rsi_high: list[str] | None = None,
) -> dict[str, Any]:
    """首页紧凑展示字段（标题已在 h2，正文不再重复前缀）。"""
    rev_lines = compact_revision_lines(revision_body)
    rsi_low_line, rsi_high_line = compact_rsi_lines(rsi_body, low=rsi_low, high=rsi_high)
    names = list(valuation_names) if valuation_names is not None else _parse_valuation_names(valuation_body)
    valuation_text = compact_valuation_text(names, valuation_body)
    return {
        "revision_lines": rev_lines,
        "revision_text": "\n".join(rev_lines) if rev_lines else "无",
        "rsi_low_line": rsi_low_line,
        "rsi_high_line": rsi_high_line,
        "rsi_text": f"{rsi_low_line}\n{rsi_high_line}",
        "valuation_text": valuation_text,
        "revision_empty": not rev_lines,
        "valuation_empty": not names,
        "rsi_low": list(rsi_low) if rsi_low is not None else _parse_rsi_side(rsi_body, "低于30"),
        "rsi_high": list(rsi_high) if rsi_high is not None else _parse_rsi_side(rsi_body, "高于70"),
        "valuation_names": names,
    }


def _tv_forward_pe(ticker: str) -> float | None:
    """与建站一致：close / eps_estimate_ntm（同币种直接相除）。"""
    path = ROOT / "data" / ticker / "consensus" / "tradingview_scan.json"
    if not path.exists():
        return None
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    cols = ((snap.get("quote") or {}).get("columns") or {})
    try:
        close = float(cols.get("close"))
        ntm = float(cols.get("eps_estimate_ntm"))
    except (TypeError, ValueError):
        return None
    if ntm == 0 or close != close or ntm != ntm:
        return None
    return close / ntm

def _tv_rsi(ticker: str) -> float | None:
    path = ROOT / "data" / ticker / "consensus" / "tradingview_scan.json"
    if not path.exists():
        return None
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    cols = ((snap.get("quote") or {}).get("columns") or {})
    raw = cols.get("RSI")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN
        return None
    return value


def _current_signal_row(ticker: str, today: date) -> dict[str, Any] | None:
    try:
        cfg = load_ticker_config(ticker)
    except (FileNotFoundError, OSError):
        return None
    fye = int(cfg.get("fiscal_year_end_month") or 12)
    record = load_earnings_record(ticker, fetch=False)
    row = choose_current_estimate(
        record.get("estimates"),
        reported_period=None,
        fiscal_year_end_month=fye,
        today=today,
    ) or {}
    avg = row.get("eps_estimate_average")
    ago = row.get("eps_estimate_average_30_days_ago")
    up = row.get("eps_estimate_revision_up_trailing_30_days")
    down = row.get("eps_estimate_revision_down_trailing_30_days")
    cov = row.get("eps_estimate_analyst_count")
    chg = chg_30d_pct(avg, ago)
    signal = hermes_revision_signal(avg, cov, chg, up, down)
    drive = earnings_drive_tag(None, today)  # dates optional; brief base ignores drive
    return {
        "ticker": ticker,
        "signal": signal,
        "drive": drive,
        "eps": avg,
        "chg": chg,
        "up": up,
        "down": down,
        "coverage": cov,
        "period": row.get("fiscal_period") or row.get("date") or "",
    }


def _fmt_q_clause(row: dict[str, Any]) -> str:
    eps = row.get("eps")
    chg = row.get("chg")
    up = row.get("up")
    down = row.get("down")
    cov = row.get("coverage")
    try:
        eps_s = f"{float(eps):.2f}" if eps is not None else "—"
    except (TypeError, ValueError):
        eps_s = "—"
    if chg is None:
        chg_s = "—"
    else:
        chg_s = f"{float(chg) * 100:+.1f}%"
    try:
        up_s = str(int(float(up))) if up is not None else "—"
    except (TypeError, ValueError):
        up_s = "—"
    try:
        down_s = str(int(float(down))) if down is not None else "—"
    except (TypeError, ValueError):
        down_s = "—"
    try:
        cov_s = str(int(float(cov))) if cov is not None else "—"
    except (TypeError, ValueError):
        cov_s = "—"
    return f"当前季EPS {eps_s}，30日{chg_s}，上/下修30日 {up_s}/{down_s}，覆盖{cov_s}"


def _load_prev_signals(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def compute_brief_from_pool(
    tickers: list[str] | None = None,
    *,
    today: date | None = None,
    prev_path: Path | None = None,
) -> dict[str, Any]:
    """用 TV RSI + AV 当前季按 Hermes 口径现算简要总结第 1–2 条。"""
    today = today or datetime.now(timezone.utc).astimezone().date()
    tickers = tickers or list_tickers()
    prev_path = prev_path or repo_path("data", "market_brief.json")
    prev = _load_prev_signals(prev_path)

    rows = [r for t in tickers if (r := _current_signal_row(t, today)) is not None]
    signals = {r["ticker"]: r["signal"] for r in rows}

    change_bits: list[str] = []
    prev_signals = (prev or {}).get("signals") if isinstance(prev, dict) else None
    prev_date = str((prev or {}).get("data_date") or "")
    # 仅当昨日快照存在且日期不同，才列基标签变动（对齐 Hermes「昨日 vs 今日」）
    if isinstance(prev_signals, dict) and prev_date and prev_date != today.isoformat():
        for r in rows:
            old = _signal_base(prev_signals.get(r["ticker"]))
            new = _signal_base(r["signal"])
            if not old or old == new:
                continue
            change_bits.append(f"{r['ticker']}：{old}——{new}")

    if change_bits:
        revision_body = "；".join(change_bits)
        revision_empty = False
    else:
        revision_body = "无"
        revision_empty = True

    low_rsi = [t for t in tickers if (rsi := _tv_rsi(t)) is not None and rsi < 30]
    high_rsi = [t for t in tickers if (rsi := _tv_rsi(t)) is not None and rsi > 70]
    rsi_body = (
        f"低于30：{'、'.join(low_rsi) if low_rsi else '无'}；"
        f"高于70：{'、'.join(high_rsi) if high_rsi else '无'}"
    )
    from pipeline.config import pe_target_for

    valuation_names: list[str] = []
    for t in tickers:
        pe = _tv_forward_pe(t)
        target = pe_target_for(t)
        if valuation_triggered(pe, target):
            valuation_names.append(t)
    valuation_body = "、".join(valuation_names) if valuation_names else "无"
    display = _display_texts(
        revision_body=revision_body,
        rsi_body=rsi_body,
        valuation_body=valuation_body,
        valuation_names=valuation_names,
        rsi_low=low_rsi,
        rsi_high=high_rsi,
    )
    return {
        "data_date": today.isoformat(),
        "source": "compute",
        "source_path": "",
        "revision_body": revision_body,
        "rsi_body": rsi_body,
        "valuation_body": valuation_body,
        **display,
        "signals": signals,
    }


def _note_is_fresh(data_date: str, today: date, *, max_age_days: int = 3) -> bool:
    try:
        day = date.fromisoformat(str(data_date)[:10])
    except ValueError:
        return False
    return 0 <= (today - day).days <= max_age_days


def load_market_brief(
    *,
    tickers: list[str] | None = None,
    today: date | None = None,
    prefer_note: bool = True,
    persist: bool = True,
) -> dict[str, Any]:
    """首页用：优先新鲜笔记简要总结，否则现算；可选写回 data/market_brief.json。"""
    today = today or datetime.now(timezone.utc).astimezone().date()
    brief: dict[str, Any] | None = None
    if prefer_note:
        note = find_latest_note()
        if note is not None:
            parsed = parse_brief_from_note(note)
            if parsed and _note_is_fresh(str(parsed.get("data_date") or ""), today):
                brief = parsed
    if brief is None:
        brief = compute_brief_from_pool(tickers, today=today)
    # 笔记路径补一份当前信号，供次日「今日修正」对比
    if "signals" not in brief:
        try:
            computed = compute_brief_from_pool(
                tickers,
                today=today,
                prev_path=repo_path("data", "market_brief.json.missing"),
            )
            brief["signals"] = computed.get("signals") or {}
            if not brief.get("data_date"):
                brief["data_date"] = computed.get("data_date")
        except Exception:
            brief.setdefault("signals", {})
    if persist:
        out = repo_path("data", "market_brief.json")
        payload = {
            "data_date": brief.get("data_date"),
            "source": brief.get("source"),
            "source_path": brief.get("source_path") or "",
            "revision_body": brief.get("revision_body"),
            "rsi_body": brief.get("rsi_body"),
            "valuation_body": brief.get("valuation_body"),
            "valuation_names": brief.get("valuation_names") or [],
            "signals": brief.get("signals") or {},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return brief


def brief_for_template(brief: dict[str, Any] | None) -> dict[str, Any]:
    """Jinja 友好视图（紧凑三栏，无冗长前缀/括号细节）。"""
    if not brief:
        low_line, high_line = compact_rsi_lines("")
        return {
            "data_date": "",
            "revision_text": "无",
            "revision_lines": [],
            "rsi_text": f"{low_line}\n{high_line}",
            "rsi_low_line": low_line,
            "rsi_high_line": high_line,
            "valuation_text": "无",
            "revision_empty": True,
            "valuation_empty": True,
            "rsi_low": [],
            "rsi_high": [],
            "valuation_names": [],
            "source": "empty",
        }
    display = _display_texts(
        revision_body=str(brief.get("revision_body") or brief.get("revision_text") or "无"),
        rsi_body=str(brief.get("rsi_body") or ""),
        valuation_body=str(brief.get("valuation_body") or ""),
        valuation_names=list(brief.get("valuation_names") or []) or None,
        rsi_low=list(brief.get("rsi_low") or []) if "rsi_low" in brief else None,
        rsi_high=list(brief.get("rsi_high") or []) if "rsi_high" in brief else None,
    )
    # 若上游已给紧凑 revision_lines，优先用
    if isinstance(brief.get("revision_lines"), list) and brief.get("revision_lines"):
        display["revision_lines"] = [str(x) for x in brief["revision_lines"]]
        display["revision_text"] = "\n".join(display["revision_lines"])
        display["revision_empty"] = False
    return {
        "data_date": brief.get("data_date") or "",
        **display,
        "source": brief.get("source") or "",
    }
