"""从电话会回答里抽出数字和时间，用来核对摘要有没有漏。"""

from __future__ import annotations

import re
from typing import Any


_WORD_NUM = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}

_SPEAKER_HEAD = re.compile(
    r"^(?P<name>Operator|[A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){0,5})"
    r"(?:\s+--\s+(?P<title>[^:]{0,80}))?"
    r"(?::\s*(?P<rest>.*))?$"
)

# 「how reality evolves」曾被写成公司部门「现实实验室」。
_FALSE_FRIENDS = (
    ("现实实验室", re.compile(r"reality\s+labs", re.IGNORECASE), "how reality evolves 是现实会怎么演变，不是 Reality Labs"),
)


def speaker_turns(text: str) -> list[tuple[str, str]]:
    """按发言人切开。返回 (姓名, 发言)。姓名为空的是还没出现抬头的行。"""
    turns: list[tuple[str, list[str]]] = []
    current: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if current is None and not any(part.strip() for part in buf):
            return
        turns.append((current or "", buf))

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _SPEAKER_HEAD.match(line)
        if match and _is_speaker_line(match, line):
            flush()
            current = match.group("name").strip()
            buf = [match.group("rest") or ""]
        else:
            buf.append(line)
    flush()
    return [(name, " ".join(part for part in parts if part.strip()).strip()) for name, parts in turns]


def _is_speaker_line(match: re.Match[str], line: str) -> bool:
    name = match.group("name")
    if name == "Operator" or " -- " in line or match.group("rest") is not None:
        # 整行必须被抬头模式吃完，避免把普通句子当成姓名
        return True
    return False


def _is_analyst(name: str, analyst: str | None) -> bool:
    if not analyst or not name:
        return False
    analyst_l = analyst.lower()
    name_l = name.lower()
    if analyst_l in name_l or name_l in analyst_l:
        return True
    last = analyst_l.split()[-1]
    return bool(last) and last in name_l.split()


def management_answer_text(text: str, analyst: str | None) -> str:
    """只留管理层回答，不含主持人和分析师提问。"""
    parts = []
    for name, utterance in speaker_turns(text):
        if not utterance:
            continue
        if not name or name == "Operator" or _is_analyst(name, analyst):
            continue
        parts.append(utterance)
    return "\n".join(parts)


def analyst_question_text(text: str, analyst: str | None) -> str:
    parts = []
    for name, utterance in speaker_turns(text):
        if name and _is_analyst(name, analyst):
            parts.append(utterance)
    return "\n".join(parts)


def analyst_subquestion_count(text: str, analyst: str | None) -> int:
    """一轮里分析师问了几件互不相关的事。问号、I have 2、the second 1 都算。"""
    blob = analyst_question_text(text, analyst)
    if not blob.strip():
        return 0
    count = blob.count("?") + blob.count("？")
    match = re.search(r"\b(?:i have|have)\s+(\d+|two|three|four)\b", blob, re.IGNORECASE)
    if match:
        raw = match.group(1).lower()
        count = max(count, int(raw) if raw.isdigit() else _WORD_NUM.get(raw, 1))
    if re.search(r"\b(?:the\s+)?second\s+(?:1|one|question)\b", blob, re.IGNORECASE):
        count = max(count, 2)
    if re.search(r"\b(?:the\s+)?third\s+(?:1|one|question)\b", blob, re.IGNORECASE):
        count = max(count, 3)
    ons = len(re.findall(r"\b1\s+on\b", blob, re.IGNORECASE))
    if ons >= 2:
        count = max(count, ons)
    return max(count, 1)


def _canon(number: float, unit: str) -> tuple[str, float]:
    unit_l = unit.lower()
    if unit_l in {"million"}:
        return ("count", number * 1_000_000)
    if unit_l in {"billion"}:
        return ("count", number * 1_000_000_000)
    if unit_l in {"万"}:
        return ("count", number * 10_000)
    if unit_l in {"亿"}:
        return ("count", number * 100_000_000)
    if unit_l.startswith("week") or unit_l == "周":
        return ("weeks", number)
    if unit_l.startswith("month"):
        return ("months", number)
    if unit_l.startswith("year"):
        return ("years", number)
    if unit_l in {"%", "percent"}:
        return ("percent", number)
    return (unit_l, number)


def extract_facts(text: str) -> set[tuple[str, float]]:
    """抽出可核对的数字和时间。10 million 与 1000万 是同一条。"""
    raw = (text or "").replace(",", "").replace("，", "")
    facts: set[tuple[str, float]] = set()
    for match in re.finditer(
        r"\b(\d+(?:\.\d+)?)\s*(million|billion|percent|%|weeks?|months?|years?)\b",
        raw,
        re.IGNORECASE,
    ):
        facts.add(_canon(float(match.group(1)), match.group(2)))
    for match in re.finditer(
        r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|a|an)\s+"
        r"(million|billion|weeks?|months?|years?)\b",
        raw,
        re.IGNORECASE,
    ):
        facts.add(_canon(float(_WORD_NUM[match.group(1).lower()]), match.group(2)))
    if re.search(r"\b(?:a|one)\s+billion\s+plus\b|\bbillion\s+plus\b|\bmore than a billion\b", raw, re.IGNORECASE):
        facts.add(("count", 1_000_000_000))
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*万", raw):
        facts.add(_canon(float(match.group(1)), "万"))
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*亿", raw):
        facts.add(_canon(float(match.group(1)), "亿"))
    if "十亿" in raw:
        facts.add(("count", 1_000_000_000))
    if re.search(r"六周|6\s*周", raw):
        facts.add(("weeks", 6.0))
    for match in re.finditer(r"(\d+)\s*周", raw):
        facts.add(("weeks", float(match.group(1))))
    for match in re.finditer(r"百分之\s*(\d+(?:\.\d+)?)", raw):
        facts.add(("percent", float(match.group(1))))
    return facts


def fact_label(fact: tuple[str, float]) -> str:
    kind, value = fact
    if kind == "count":
        if value >= 1_000_000_000 and value % 1_000_000_000 == 0:
            return f"{value / 1_000_000_000:g} billion"
        if value >= 1_000_000 and value % 1_000_000 == 0:
            return f"{value / 1_000_000:g} million"
        return f"{value:g}"
    if kind == "weeks":
        return f"{value:g} weeks"
    if kind == "percent":
        return f"{value:g}%"
    return f"{value:g} {kind}"


def missing_answer_facts(text: str, summaries: list[str], analyst: str | None) -> list[tuple[str, float]]:
    """原文回答里有、所有摘要合在一起仍然没有的数字和时间。"""
    source = extract_facts(management_answer_text(text, analyst))
    covered: set[tuple[str, float]] = set()
    for summary in summaries:
        covered |= extract_facts(summary or "")
    missing = [fact for fact in source if fact not in covered]
    missing.sort(key=lambda item: (item[0], item[1]))
    return missing


def false_friend_flags(source_text: str, summary: str) -> list[str]:
    """摘要里的专名在这一轮原文里没有对应英文时标出来。"""
    flags = []
    for phrase, pattern, note in _FALSE_FRIENDS:
        if phrase in (summary or "") and not pattern.search(source_text or ""):
            flags.append(f"「{phrase}」对不上原文：{note}")
    return flags


def attach_flags(item: dict[str, Any], flags: list[str]) -> None:
    if not flags:
        return
    current = [str(flag) for flag in item.get("review_flags") or [] if str(flag).strip()]
    for flag in flags:
        if flag not in current:
            current.append(flag)
    item["review_flags"] = current
