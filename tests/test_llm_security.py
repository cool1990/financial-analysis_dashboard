from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.commands.eval_cmd import _compare_numbers, _guidance_recall, _stable
from pipeline.llm import CostLimitExceeded, RunCostTracker, redact_secrets
from pipeline.secret_gate import gate_openrouter_key


def test_redact_secrets_masks_bearer_and_key():
    key = "sk-or-v1-abcdefghijklmnopqrstuvwxyz012345"
    text = f"Authorization: Bearer {key}\nOPENROUTER_API_KEY={key}\nok"
    out = redact_secrets(text, api_key=key)
    assert key not in out
    assert "REDACTED" in out
    assert "Bearer" in out or "***" in out


def test_redact_secrets_in_exception_message():
    key = "sk-test-secret-value-12345678"
    msg = f"LLM HTTP 401: invalid key {key} in body"
    assert key not in redact_secrets(msg, api_key=key)


def test_daily_missing_secret_does_not_fail(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    r = gate_openrouter_key(workflow="daily")
    assert r.has_key is False
    assert r.skip_llm is True
    assert r.fail is False
    assert "WARNING" in r.message


def test_manual_missing_secret_fails(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    r = gate_openrouter_key(workflow="manual")
    assert r.fail is True


def test_eval_missing_secret_fails(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    r = gate_openrouter_key(workflow="eval")
    assert r.fail is True


def test_backfill_dry_run_allows_missing_secret(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    r = gate_openrouter_key(workflow="manual", dry_run=True)
    assert r.fail is False


def test_cost_limit_stops_run():
    tracker = RunCostTracker(max_cost_usd=0.0001)
    pricing = {"prompt": 0.01, "completion": 0.01}
    # 已付费的这次结果照常返回，从下一次请求起熔断
    tracker.add(model="test/model", usage={"prompt_tokens": 100, "completion_tokens": 100}, pricing=pricing)
    with pytest.raises(CostLimitExceeded):
        tracker.check()


def test_token_limit_applies_without_pricing():
    tracker = RunCostTracker(max_cost_usd=100, max_tokens=1000)
    tracker.add(model="m", usage={"prompt_tokens": 900, "completion_tokens": 50}, pricing=None)
    tracker.check()
    tracker.add(model="m", usage={"prompt_tokens": 100, "completion_tokens": 10}, pricing=None)
    with pytest.raises(CostLimitExceeded):
        tracker.check()


def test_timeouts_trip_breaker():
    tracker = RunCostTracker(max_cost_usd=100, max_timeouts=2)
    tracker.add_timeout("m")
    tracker.check()
    tracker.add_timeout("m")
    with pytest.raises(CostLimitExceeded):
        tracker.check()


def test_eval_compare_logic_with_mock():
    expected = json.loads(
        (Path(__file__).parent / "fixtures" / "expected" / "MU_FY2025Q4.json").read_text(encoding="utf-8")
    )
    actual = {
        "_parsed": {"revenue": 11315000000, "eps_gaap": 2.60, "eps_nongaap": 3.03},
        "guidance": [
            {"metric_key": "revenue"},
            {"metric_key": "eps_nongaap"},
            {"metric_key": "gross_margin_nongaap"},
        ],
    }
    cmp = _compare_numbers(actual, expected)
    assert cmp["total"] == 3
    assert cmp["hits"] == 3
    assert cmp["accuracy"] == 1.0
    recall = _guidance_recall(actual["guidance"], expected["guidance_keys"])
    assert recall["recall"] == 1.0
    assert _stable(actual["_parsed"], dict(actual["_parsed"])) is True
    assert _stable(actual["_parsed"], {"revenue": 0}) is False


def test_llm_client_refuses_without_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from pipeline.llm import LLMClient, LLMError

    with pytest.raises(LLMError):
        LLMClient(require_key=True)
