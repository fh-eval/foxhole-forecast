from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import Settings


@dataclass
class ProviderResponse:
    parsed: dict[str, Any]
    raw: dict[str, Any]
    requested_model: str
    returned_model: str | None
    upstream_provider: str | None
    usage: dict[str, Any]
    cost_usd: float


class MissingApiKey(RuntimeError):
    pass


class ModelProvider:
    def __init__(self, model_config: dict[str, Any], settings: Settings):
        self.config = model_config
        self.settings = settings
        self.accumulated_cost = 0.0
        self.attempts: list[dict[str, Any]] = []
        self.api_key = os.environ.get(model_config["api_key_env"])
        if not self.api_key:
            raise MissingApiKey(f"Missing {model_config['api_key_env']}")

    def complete_json(
        self,
        messages: list[dict[str, str]],
        schema_name: str,
        schema: dict[str, Any],
    ) -> ProviderResponse:
        gateway = self.config["gateway"]
        if gateway == "openrouter":
            url = "https://openrouter.ai/api/v1/chat/completions"
            if self.config.get("structured_outputs", True):
                response_format: dict[str, Any] = {
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "strict": True, "schema": schema},
                }
            else:
                response_format = {"type": "json_object"}
        elif gateway == "nvidia_nim":
            url = "https://integrate.api.nvidia.com/v1/chat/completions"
            response_format = {"type": "json_object"}
        elif gateway == "deepseek":
            url = "https://api.deepseek.com/chat/completions"
            response_format = {"type": "json_object"}
        else:
            raise ValueError(f"Unsupported gateway: {gateway}")

        body: dict[str, Any] = {
            "model": self.config["model"],
            "messages": messages,
            "max_tokens": int(self.config.get("max_tokens", self.settings.output_token_limit)),
            "stream": False,
            "response_format": response_format,
        }
        if not self.config.get("omit_temperature", False):
            body["temperature"] = self.settings.temperature
        if gateway == "openrouter":
            provider: dict[str, Any] = {
                "allow_fallbacks": bool(self.config.get("allow_fallbacks", False))
            }
            if self.config.get("provider_only"):
                provider["only"] = self.config["provider_only"]
            body["provider"] = provider
            body["reasoning"] = {"effort": self.settings.reasoning_effort}
        request_extra = self.config.get("request_extra", {})
        if not isinstance(request_extra, dict):
            raise ValueError("request_extra must be an object")
        body.update(request_extra)

        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "FoxholeForecast/0.1",
            },
        )
        raw = self._request_with_retry(request)
        usage = raw.get("usage", {})
        cost = _cost(self.config["model"], usage)
        self.accumulated_cost += cost
        prompt = json.dumps(messages, separators=(",", ":"), ensure_ascii=False)
        attempt = {
            "stage": schema_name.removeprefix("foxhole_"),
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "requested_model": self.config["model"],
            "returned_model": raw.get("model"),
            "upstream_provider": raw.get("provider"),
            "usage": usage,
            "cost_usd": cost,
            "raw_response": raw,
        }
        self.attempts.append(attempt)
        content = raw["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        try:
            parsed = _parse_json_content(str(content))
        except Exception as error:
            attempt["error"] = f"{type(error).__name__}: {error}"
            raise
        return ProviderResponse(
            parsed=parsed,
            raw=raw,
            requested_model=self.config["model"],
            returned_model=raw.get("model"),
            upstream_provider=raw.get("provider"),
            usage=usage,
            cost_usd=cost,
        )

    @staticmethod
    def _request_with_retry(request: urllib.request.Request) -> dict[str, Any]:
        last_error: Exception | None = None
        for delay in (0, 2, 8):
            if delay:
                time.sleep(delay)
            try:
                with urllib.request.urlopen(request, timeout=180) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")
                last_error = RuntimeError(f"Provider returned HTTP {error.code}: {detail[:1000]}")
                if error.code not in {408, 429, 500, 502, 503, 504}:
                    raise last_error
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                last_error = error
        assert last_error is not None
        raise last_error


def _parse_json_content(content: str) -> dict[str, Any]:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines)
    value = json.loads(stripped)
    if not isinstance(value, dict):
        raise ValueError("Model output must be a JSON object")
    return value


def _cost(model: str, usage: dict[str, Any]) -> float:
    direct = usage.get("cost")
    if isinstance(direct, (int, float)):
        return float(direct)
    if model == "deepseek-v4-flash":
        cache_hit = usage.get("prompt_cache_hit_tokens")
        cache_miss = usage.get("prompt_cache_miss_tokens")
        completion = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
        if isinstance(cache_hit, (int, float)) and isinstance(cache_miss, (int, float)):
            input_cost = cache_hit * 0.0028 / 1_000_000 + cache_miss * 0.14 / 1_000_000
        else:
            # Treat all prompt tokens as cache misses when detailed usage is absent.
            prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
            input_cost = prompt * 0.14 / 1_000_000
        return round(input_cost + completion * 0.28 / 1_000_000, 8)
    prices = {
        "openai/gpt-5.6-luna": (0.20, 1.20),
        "google/gemini-3.7-flash": (0.375, 1.875),
    }
    if model in prices:
        prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
        completion = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
        input_price, output_price = prices[model]
        return round(prompt * input_price / 1_000_000 + completion * output_price / 1_000_000, 8)
    return 0.0
