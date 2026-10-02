from __future__ import annotations

import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Type, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from pipeline.config import cache_dir, load_settings, logs_dir, openrouter_api_key, prompts_dir

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class CostLimitExceeded(LLMError):
    pass


class LLMTimeoutError(LLMError):
    pass


_SENSITIVE_RE = re.compile(
    r"(?i)(Bearer\s+)[A-Za-z0-9._\-]+|(OPENROUTER_API_KEY\s*[=:]\s*)\S+|(sk-[A-Za-z0-9_\-]{8,})"
)


def redact_secrets(text: str | None, api_key: str | None = None) -> str:
    """脱敏 API key / Authorization，避免写入日志或异常信息。"""
    if text is None:
        return ""
    out = str(text)
    key = api_key or openrouter_api_key()
    if key:
        out = out.replace(key, "***REDACTED***")
        if len(key) > 8:
            out = out.replace(key[:8], "***")
    out = _SENSITIVE_RE.sub(lambda m: (m.group(1) or m.group(2) or "sk-") + "***REDACTED***", out)
    return out


def auth_headers(api_key: str) -> dict[str, str]:
    """构造请求头；调用方不得打印返回值。"""
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _loads_json_payload(content: str) -> Any:
    """解析模型输出：去 markdown 围栏、截取首个 JSON 值，拒绝空响应。"""
    text = (content or "").strip()
    if not text:
        raise json.JSONDecodeError("Expecting value", content or "", 0)
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        candidates: list[tuple[int, str]] = []
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            end = text.rfind(closer)
            if start != -1 and end > start:
                candidates.append((start, text[start : end + 1]))
        candidates.sort(key=lambda x: x[0])
        for _, frag in candidates:
            try:
                return json.loads(frag)
            except json.JSONDecodeError:
                continue
        raise


class RunCostTracker:
    """单次进程内累计 LLM 花费。"""

    def __init__(self, max_cost_usd: float):
        self.max_cost_usd = max_cost_usd
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0
        self.estimated_cost_usd = 0.0
        self.model = ""
        self.warnings: list[str] = []

    def add(self, *, model: str, usage: dict[str, Any], pricing: dict[str, float] | None) -> None:
        self.model = model or self.model
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        self.prompt_tokens += pt
        self.completion_tokens += ct
        self.calls += 1
        if pricing:
            cost = pt * pricing.get("prompt", 0.0) + ct * pricing.get("completion", 0.0)
            self.estimated_cost_usd += cost
            if self.estimated_cost_usd > self.max_cost_usd:
                raise CostLimitExceeded(
                    f"本轮估算花费 ${self.estimated_cost_usd:.4f} 超过上限 "
                    f"${self.max_cost_usd:.2f}（模型 {model}）"
                )

    def summary(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "warnings": list(self.warnings),
        }


_RUN_TRACKER: RunCostTracker | None = None
_MODEL_PRICING_CACHE: dict[str, dict[str, Any]] = {}


def get_run_tracker() -> RunCostTracker:
    global _RUN_TRACKER
    if _RUN_TRACKER is None:
        settings = load_settings().get("llm") or {}
        _RUN_TRACKER = RunCostTracker(float(settings.get("max_cost_per_run_usd", 2)))
    return _RUN_TRACKER


def reset_run_tracker() -> RunCostTracker:
    global _RUN_TRACKER
    settings = load_settings().get("llm") or {}
    _RUN_TRACKER = RunCostTracker(float(settings.get("max_cost_per_run_usd", 2)))
    return _RUN_TRACKER


def write_github_step_summary(extra_warnings: list[str] | None = None) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    tracker = get_run_tracker()
    s = tracker.summary()
    warnings = list(s["warnings"]) + list(extra_warnings or [])
    lines = [
        "## LLM 运行摘要",
        "",
        f"- 模型: `{s['model'] or '(未调用)'}`",
        f"- 调用次数: {s['calls']}",
        f"- 输入 token: {s['prompt_tokens']}",
        f"- 输出 token: {s['completion_tokens']}",
        f"- 估算花费: ${s['estimated_cost_usd']:.4f}",
        "",
        "### Warnings",
    ]
    if warnings:
        lines.extend(f"- {w}" for w in warnings)
    else:
        lines.append("- （无）")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def fetch_openrouter_models(api_key: str | None = None) -> list[dict[str, Any]]:
    """GET /api/v1/models（公开接口，有 key 时一并带上）。"""
    headers = {"Content-Type": "application/json"}
    key = api_key or openrouter_api_key()
    # 公开可读；有 key 时带上但不在日志中打印 headers
    if key:
        headers["Authorization"] = f"Bearer {key}"
    with httpx.Client(timeout=60.0) as client:
        resp = client.get("https://openrouter.ai/api/v1/models", headers=headers)
    if resp.status_code >= 400:
        raise LLMError(f"拉取模型列表失败 HTTP {resp.status_code}: {redact_secrets(resp.text[:300], key)}")
    data = resp.json()
    return list(data.get("data") or [])


def find_model_info(slug: str, models: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    models = models if models is not None else fetch_openrouter_models()
    for m in models:
        if m.get("id") == slug:
            return m
    return None


def pricing_from_model_info(info: dict[str, Any] | None) -> dict[str, float] | None:
    if not info:
        return None
    pricing = info.get("pricing") or {}
    try:
        return {
            "prompt": float(pricing.get("prompt") or 0),
            "completion": float(pricing.get("completion") or 0),
        }
    except (TypeError, ValueError):
        return None


class LLMClient:
    def __init__(
        self,
        *,
        model: str | None = None,
        use_cache: bool = True,
        require_key: bool = True,
    ):
        settings = load_settings()["llm"]
        self.provider = settings.get("provider", "openrouter")
        self.model = model or os.environ.get("PIPELINE_LLM_MODEL") or settings["model"]
        self.temperature = settings.get("temperature", 0)
        self.base_url = settings.get("base_url", "https://openrouter.ai/api/v1")
        self.max_retries = int(settings.get("max_retries", 1))
        self.max_tokens = int(settings.get("max_tokens", 4096))
        self.request_timeout_sec = float(settings.get("request_timeout_sec", 90))
        self.max_cost = float(settings.get("max_cost_per_run_usd", 2))
        self.use_cache = use_cache
        self.api_key = openrouter_api_key()
        self.cache_root = cache_dir() / "llm"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.usage_log = logs_dir() / "llm_usage.csv"
        self.usage_log.parent.mkdir(parents=True, exist_ok=True)
        if not self.usage_log.exists():
            self.usage_log.write_text(
                "timestamp,prompt,model,prompt_tokens,completion_tokens,latency_ms,cached,est_cost_usd\n",
                encoding="utf-8",
            )
        if require_key and not self.api_key:
            raise LLMError("缺少 OPENROUTER_API_KEY，无法调用 LLM（仅应在 GitHub Actions 中配置该 Secret）")
        self._pricing = self._load_pricing()

    def _load_pricing(self) -> dict[str, float] | None:
        if self.model in _MODEL_PRICING_CACHE:
            return pricing_from_model_info(_MODEL_PRICING_CACHE[self.model])
        try:
            info = find_model_info(self.model)
            if info:
                _MODEL_PRICING_CACHE[self.model] = info
            return pricing_from_model_info(info)
        except Exception as e:
            get_run_tracker().warnings.append(f"无法获取模型单价: {redact_secrets(str(e), self.api_key)}")
            return None

    def load_prompt(self, name: str, **kwargs: str) -> str:
        path = prompts_dir() / name
        text = path.read_text(encoding="utf-8")
        for k, v in kwargs.items():
            text = text.replace("{" + k + "}", v)
        return text

    def _cache_key(self, prompt_name: str, system: str, user: str) -> str:
        h = hashlib.sha256()
        h.update(prompt_name.encode())
        h.update(self.model.encode())
        h.update(system.encode())
        h.update(user.encode())
        return h.hexdigest()

    def _read_cache(self, key: str) -> dict[str, Any] | None:
        if not self.use_cache:
            return None
        path = self.cache_root / f"{key}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return None

    def _write_cache(self, key: str, payload: dict[str, Any]) -> None:
        if not self.use_cache:
            return
        path = self.cache_root / f"{key}.json"
        # 缓存中不写入任何可能含 key 的字段
        safe = {"data": payload.get("data"), "usage": payload.get("usage") or {}}
        path.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")

    def _estimate_cost(self, usage: dict[str, Any]) -> float:
        if not self._pricing:
            return 0.0
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        return pt * self._pricing.get("prompt", 0.0) + ct * self._pricing.get("completion", 0.0)

    def _log_usage(self, prompt: str, usage: dict[str, Any], latency_ms: float, cached: bool) -> None:
        cost = self._estimate_cost(usage)
        line = (
            f"{datetime.now(timezone.utc).isoformat()},{prompt},{self.model},"
            f"{usage.get('prompt_tokens', '')},{usage.get('completion_tokens', '')},"
            f"{latency_ms:.0f},{int(cached)},{cost:.8f}\n"
        )
        with self.usage_log.open("a", encoding="utf-8") as f:
            f.write(line)
        if not cached:
            get_run_tracker().add(model=self.model, usage=usage, pricing=self._pricing)

    def _post_chat(self, messages: list[dict[str, str]], *, json_object: bool = True) -> tuple[str, dict[str, Any], float]:
        if not self.api_key:
            raise LLMError("缺少 OPENROUTER_API_KEY，无法调用 LLM")
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": messages,
        }
        if json_object:
            body["response_format"] = {"type": "json_object"}
        # httpx 的 read timeout 会在每个 chunk 间重置；流式长输出可拖到数十分钟。
        # 用线程池施加墙钟上限，超时即放弃本次调用。
        deadline = max(1.0, float(self.request_timeout_sec))
        http_timeout = min(deadline, 60.0)

        def _do_request() -> httpx.Response:
            with httpx.Client(timeout=http_timeout) as client:
                return client.post(
                    f"{self.base_url.rstrip('/')}/chat/completions",
                    headers=auth_headers(self.api_key or ""),
                    json=body,
                )

        t0 = time.monotonic()
        try:
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                fut = pool.submit(_do_request)
                try:
                    resp = fut.result(timeout=deadline)
                except FuturesTimeout:
                    # 不 wait 卡住的请求线程，避免超时后仍阻塞到 httpx 读完
                    raise LLMTimeoutError(
                        f"LLM 请求超过墙钟上限 {deadline:.0f}s（model={self.model}）"
                    ) from None
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
        except LLMTimeoutError:
            raise
        except Exception as e:
            raise LLMError(redact_secrets(str(e), self.api_key)) from None
        latency = (time.monotonic() - t0) * 1000
        if resp.status_code >= 400:
            raise LLMError(
                f"LLM HTTP {resp.status_code}: {redact_secrets(resp.text[:500], self.api_key)}"
            )
        payload = resp.json()
        content = payload["choices"][0]["message"]["content"]
        usage = payload.get("usage") or {}
        return content, usage, latency

    def complete_json(
        self,
        prompt_name: str,
        user_content: str,
        schema: Type[T],
        *,
        extra_system: str = "",
    ) -> T:
        system = (
            "You output only valid JSON. No markdown fences, no commentary.\n" + extra_system
        ).strip()
        key = self._cache_key(prompt_name, system, user_content)
        cached = self._read_cache(key)
        if cached:
            self._log_usage(prompt_name, cached.get("usage", {}), 0, True)
            return schema.model_validate(cached["data"])

        last_err: Exception | None = None
        content = ""
        for attempt in range(self.max_retries + 2):
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ]
            if last_err is not None:
                messages.append(
                    {
                        "role": "user",
                        "content": f"上次输出未通过校验：{redact_secrets(str(last_err), self.api_key)}。请只输出修正后的 JSON。",
                    }
                )
            try:
                content, usage, latency = self._post_chat(messages, json_object=True)
            except LLMError:
                raise
            self._log_usage(prompt_name, usage, latency, False)
            try:
                data = json.loads(content)
                model = schema.model_validate(data)
                self._write_cache(key, {"data": data, "usage": usage})
                return model
            except (json.JSONDecodeError, ValidationError) as e:
                last_err = e
                if attempt >= self.max_retries:
                    break
        raise LLMError(
            f"LLM JSON 校验失败: {redact_secrets(str(last_err), self.api_key)}; "
            f"raw={redact_secrets(content[:500], self.api_key)}"
        )

    def complete_json_list(
        self,
        prompt_name: str,
        user_content: str,
        item_schema: Type[BaseModel],
    ) -> list[Any]:
        system = (
            "You output only a valid JSON array (or {\"items\": [...]}). "
            "No markdown fences, no commentary, no empty response."
        )
        key = self._cache_key(prompt_name, system, user_content)
        cached = self._read_cache(key)
        if cached:
            self._log_usage(prompt_name, cached.get("usage", {}), 0, True)
            return [item_schema.model_validate(x) for x in cached["data"]]

        last_err: Exception | None = None
        content = ""
        for attempt in range(self.max_retries + 2):
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ]
            if last_err is not None:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"上次输出无法解析：{redact_secrets(str(last_err), self.api_key)}。"
                            "请只输出 JSON 数组，不要其它文字。"
                        ),
                    }
                )
            try:
                # 优先 json_object，兼容模型返回 {"items":[...]}；失败再试纯数组模式
                use_obj = attempt == 0 or attempt % 2 == 0
                content, usage, latency = self._post_chat(
                    messages,
                    json_object=use_obj,
                )
            except LLMError:
                raise
            self._log_usage(prompt_name, usage, latency, False)
            try:
                data = _loads_json_payload(content)
                if isinstance(data, dict) and "items" in data:
                    data = data["items"]
                if not isinstance(data, list):
                    raise LLMError("期望 JSON 数组")
                models = [item_schema.model_validate(x) for x in data]
                self._write_cache(key, {"data": data, "usage": usage})
                return models
            except (json.JSONDecodeError, ValidationError, LLMError) as e:
                last_err = e
                if attempt >= self.max_retries + 1:
                    break
        raise LLMError(
            f"LLM JSON 数组校验失败: {redact_secrets(str(last_err), self.api_key)}; "
            f"raw={redact_secrets(content[:500], self.api_key)}"
        )

    def ping(self) -> dict[str, Any]:
        """极小请求，要求返回 {"ok": true}。"""
        content, usage, latency = self._post_chat(
            [
                {
                    "role": "system",
                    "content": 'Reply with JSON only: {"ok": true}',
                },
                {"role": "user", "content": "ping"},
            ],
            json_object=True,
        )
        self._log_usage("llm-ping", usage, latency, False)
        data = json.loads(content)
        if data.get("ok") is not True:
            raise LLMError(f"llm-ping 返回异常: {redact_secrets(content[:200], self.api_key)}")
        return {"ok": True, "model": self.model, "usage": usage, "latency_ms": latency}
