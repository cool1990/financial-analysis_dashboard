from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Type, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from pipeline.config import cache_dir, load_settings, logs_dir, openrouter_api_key, prompts_dir

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self):
        settings = load_settings()["llm"]
        self.provider = settings.get("provider", "openrouter")
        self.model = settings["model"]
        self.temperature = settings.get("temperature", 0)
        self.base_url = settings.get("base_url", "https://openrouter.ai/api/v1")
        self.max_retries = settings.get("max_retries", 1)
        self.api_key = openrouter_api_key()
        self.cache_root = cache_dir() / "llm"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.usage_log = logs_dir() / "llm_usage.csv"
        self.usage_log.parent.mkdir(parents=True, exist_ok=True)
        if not self.usage_log.exists():
            self.usage_log.write_text(
                "timestamp,prompt,model,prompt_tokens,completion_tokens,latency_ms,cached\n",
                encoding="utf-8",
            )

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
        path = self.cache_root / f"{key}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return None

    def _write_cache(self, key: str, payload: dict[str, Any]) -> None:
        path = self.cache_root / f"{key}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _log_usage(self, prompt: str, usage: dict[str, Any], latency_ms: float, cached: bool) -> None:
        line = (
            f"{datetime.now(timezone.utc).isoformat()},{prompt},{self.model},"
            f"{usage.get('prompt_tokens', '')},{usage.get('completion_tokens', '')},"
            f"{latency_ms:.0f},{int(cached)}\n"
        )
        with self.usage_log.open("a", encoding="utf-8") as f:
            f.write(line)

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

        if not self.api_key:
            raise LLMError("缺少 OPENROUTER_API_KEY，无法调用 LLM")

        last_err: Exception | None = None
        content = ""
        usage: dict[str, Any] = {}
        for attempt in range(self.max_retries + 2):
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ]
            if last_err is not None:
                messages.append(
                    {
                        "role": "user",
                        "content": f"上次输出未通过校验：{last_err}。请只输出修正后的 JSON。",
                    }
                )
            t0 = time.monotonic()
            with httpx.Client(timeout=120.0) as client:
                resp = client.post(
                    f"{self.base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "temperature": self.temperature,
                        "messages": messages,
                        "response_format": {"type": "json_object"},
                    },
                )
            latency = (time.monotonic() - t0) * 1000
            if resp.status_code >= 400:
                raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:500]}")
            payload = resp.json()
            content = payload["choices"][0]["message"]["content"]
            usage = payload.get("usage") or {}
            self._log_usage(prompt_name, usage, latency, False)
            try:
                data = json.loads(content)
                # Allow root array by wrapping
                if schema is list or getattr(schema, "__origin__", None) is list:
                    pass
                model = schema.model_validate(data)
                self._write_cache(key, {"data": data, "usage": usage, "raw": content})
                return model
            except (json.JSONDecodeError, ValidationError) as e:
                last_err = e
                if attempt >= self.max_retries:
                    break
        raise LLMError(f"LLM JSON 校验失败: {last_err}; raw={content[:500]}")

    def complete_json_list(
        self,
        prompt_name: str,
        user_content: str,
        item_schema: Type[BaseModel],
    ) -> list[Any]:
        """当模型返回数组时使用。"""

        class _Wrap(BaseModel):
            items: list[Any]

        # Encourage object wrapper if needed
        try:
            # First try direct array via a shim schema
            class ArrayRoot(BaseModel):
                root: list[dict[str, Any]]

            # Use raw call
            system = "You output only a valid JSON array. No markdown fences."
            key = self._cache_key(prompt_name, system, user_content)
            cached = self._read_cache(key)
            if cached:
                self._log_usage(prompt_name, cached.get("usage", {}), 0, True)
                return [item_schema.model_validate(x) for x in cached["data"]]

            if not self.api_key:
                raise LLMError("缺少 OPENROUTER_API_KEY，无法调用 LLM")

            t0 = time.monotonic()
            with httpx.Client(timeout=120.0) as client:
                resp = client.post(
                    f"{self.base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "temperature": self.temperature,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user_content},
                        ],
                    },
                )
            latency = (time.monotonic() - t0) * 1000
            payload = resp.json()
            content = payload["choices"][0]["message"]["content"]
            usage = payload.get("usage") or {}
            self._log_usage(prompt_name, usage, latency, False)
            data = json.loads(content)
            if isinstance(data, dict) and "items" in data:
                data = data["items"]
            if not isinstance(data, list):
                raise LLMError("期望 JSON 数组")
            models = [item_schema.model_validate(x) for x in data]
            self._write_cache(key, {"data": data, "usage": usage, "raw": content})
            return models
        except Exception as e:
            raise LLMError(str(e)) from e
