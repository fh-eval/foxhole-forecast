from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from foxhole_forecast.config import Settings
from foxhole_forecast.providers import ModelProvider, _cost


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(
            {
                "choices": [{"message": {"content": "{\"ok\":true}"}}],
                "model": "test/model",
                "usage": {},
            }
        ).encode()


class _MalformedPaidResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(
            {
                "choices": [{"message": {"content": "{\"unfinished\":"}}],
                "model": "deepseek-v4-flash",
                "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
            }
        ).encode()


class ProviderTests(unittest.TestCase):
    def test_deepseek_cost_uses_cache_specific_rates(self) -> None:
        cost = _cost(
            "deepseek-v4-flash",
            {
                "prompt_cache_hit_tokens": 1_000_000,
                "prompt_cache_miss_tokens": 1_000_000,
                "completion_tokens": 1_000_000,
            },
        )
        self.assertEqual(cost, 0.4228)

    def test_deepseek_cost_conservatively_treats_unknown_prompt_as_cache_miss(self) -> None:
        cost = _cost(
            "deepseek-v4-flash",
            {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
        )
        self.assertEqual(cost, 0.42)

    def test_gemini_fallback_cost_uses_current_openrouter_rate(self) -> None:
        cost = _cost(
            "google/gemini-3.7-flash",
            {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
        )
        self.assertEqual(cost, 2.25)

    def test_reported_gateway_cost_takes_precedence(self) -> None:
        self.assertEqual(_cost("google/gemini-3.7-flash", {"cost": 0.0123}), 0.0123)

    def test_nvidia_model_request_overrides_are_sent(self) -> None:
        captured = {}

        def urlopen(request, timeout):
            self.assertEqual(timeout, 180)
            captured.update(json.loads(request.data))
            return _Response()

        config = {
            "gateway": "nvidia_nim",
            "model": "test/model",
            "api_key_env": "TEST_NVIDIA_KEY",
            "max_tokens": 8192,
            "request_extra": {"chat_template_kwargs": {"enable_thinking": False}},
        }
        with patch.dict("os.environ", {"TEST_NVIDIA_KEY": "secret"}), patch(
            "urllib.request.urlopen", side_effect=urlopen
        ):
            provider = ModelProvider(config, Settings.load())
            provider.complete_json(
                [{"role": "user", "content": "Return JSON"}],
                "test",
                {"type": "object"},
            )

        self.assertEqual(captured["max_tokens"], 8192)
        self.assertEqual(
            captured["chat_template_kwargs"], {"enable_thinking": False}
        )

    def test_deepseek_uses_direct_endpoint_and_disables_thinking(self) -> None:
        captured = {}

        def urlopen(request, timeout):
            self.assertEqual(timeout, 180)
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data)
            return _Response()

        config = {
            "gateway": "deepseek",
            "model": "deepseek-v4-flash",
            "api_key_env": "TEST_DEEPSEEK_KEY",
            "request_extra": {"thinking": {"type": "disabled"}},
        }
        with patch.dict("os.environ", {"TEST_DEEPSEEK_KEY": "secret"}), patch(
            "urllib.request.urlopen", side_effect=urlopen
        ):
            provider = ModelProvider(config, Settings.load())
            provider.complete_json(
                [{"role": "user", "content": "Return JSON"}],
                "test",
                {"type": "object"},
            )

        self.assertEqual(captured["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(captured["body"]["response_format"], {"type": "json_object"})
        self.assertEqual(captured["body"]["thinking"], {"type": "disabled"})

    def test_malformed_paid_response_still_counts_toward_budget(self) -> None:
        config = {
            "gateway": "deepseek",
            "model": "deepseek-v4-flash",
            "api_key_env": "TEST_DEEPSEEK_KEY",
        }
        with patch.dict("os.environ", {"TEST_DEEPSEEK_KEY": "secret"}), patch(
            "urllib.request.urlopen", return_value=_MalformedPaidResponse()
        ):
            provider = ModelProvider(config, Settings.load())
            with self.assertRaises(json.JSONDecodeError):
                provider.complete_json(
                    [{"role": "user", "content": "Return JSON"}],
                    "test",
                    {"type": "object"},
                )

        self.assertEqual(provider.accumulated_cost, 0.42)


if __name__ == "__main__":
    unittest.main()
