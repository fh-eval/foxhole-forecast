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


class ProviderTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
