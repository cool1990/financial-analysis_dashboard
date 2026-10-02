from __future__ import annotations

"""GitHub Actions secret 门控逻辑（供 workflow 与测试共用）。"""

from dataclasses import dataclass

from pipeline.config import openrouter_api_key


@dataclass
class SecretGateResult:
    has_key: bool
    skip_llm: bool
    fail: bool
    message: str


def gate_openrouter_key(*, workflow: str, dry_run: bool = False) -> SecretGateResult:
    """根据 workflow 类型决定缺 key 时的行为。

    - daily / poll: 跳过 LLM，不失败
    - manual / eval: 失败（除非 backfill dry-run）
    """
    has = bool(openrouter_api_key())
    if has:
        return SecretGateResult(True, False, False, "OPENROUTER_API_KEY present")
    if workflow in {"daily", "poll"}:
        return SecretGateResult(
            False,
            True,
            False,
            "WARNING: OPENROUTER_API_KEY missing; skip LLM steps, continue snapshot/build",
        )
    if workflow == "manual" and dry_run:
        return SecretGateResult(False, True, False, "backfill dry-run does not require key")
    return SecretGateResult(
        False,
        True,
        True,
        f"ERROR: OPENROUTER_API_KEY required for {workflow}",
    )
