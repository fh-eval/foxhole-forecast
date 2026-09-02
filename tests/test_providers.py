from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from foxhole_forecast.config import Settings, load_models
from foxhole_forecast.providers import ModelProvider, _cost, _parse_json_content


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
    def test_connection_reset_is_retried(self) -> None:
        config = {
            "gateway": "nvidia_nim",
            "model": "test/model",
            "api_key_env": "TEST_NVIDIA_KEY",
            "retry_delays_seconds": [0, 0],
        }
        with patch.dict("os.environ", {"TEST_NVIDIA_KEY": "secret"}), patch(
            "urllib.request.urlopen",
            side_effect=[ConnectionResetError("reset"), _Response()],
        ) as urlopen:
            response = ModelProvider(config, Settings.load()).complete_json(
                [{"role": "user", "content": "Return JSON"}],
                "test",
                {"type": "object"},
            )

        self.assertEqual(response.parsed, {"ok": True})
        self.assertEqual(urlopen.call_count, 2)

    def test_json_parser_salvages_markdown_fences_and_surrounding_text(self) -> None:
        self.assertEqual(
            _parse_json_content(
                "Here is the requested JSON:\n```json\n{\"ok\":true}\n```\nDone."
            ),
            {"ok": True},
        )

    def test_json_parser_does_not_salvage_two_adjacent_objects(self) -> None:
        with self.assertRaises(json.JSONDecodeError):
            _parse_json_content('{"first":true}\n{"second":true}')

    def test_json_parser_does_not_extract_nested_object_from_malformed_output(self) -> None:
        with self.assertRaises(json.JSONDecodeError):
            _parse_json_content('{"outer":{"ok":true}')

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
        self.assertEqual(cost, 4.5)

    def test_gemini_38_fallback_cost_uses_current_openrouter_rate(self) -> None:
        cost = _cost(
            "google/gemini-3.8-flash",
            {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
        )
        self.assertEqual(cost, 4.5)

    def test_glm_flash_fallback_cost_uses_conservative_list_rate(self) -> None:
        cost = _cost(
            "z-ai/glm-5.3-flash",
            {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
        )
        self.assertEqual(cost, 0.65)

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

    def test_nemotron_config_omits_unsupported_reasoning_budget(self) -> None:
        nemotron = next(
            model
            for model in load_models()
            if model["series_id"] == "nvidia-nemotron-3-ultra-550b-a55b-event-v4"
        )

        self.assertEqual(nemotron["request_extra"]["reasoning_effort"], "medium")
        self.assertNotIn("reasoning_budget", nemotron["request_extra"])

    def test_glm_flash_config_pins_official_zai_with_max_reasoning(self) -> None:
        glm = next(
            model
            for model in load_models()
            if model["series_id"] == "openrouter-z-ai-glm-5.3-flash-event-v4"
        )

        self.assertEqual(glm["model"], "z-ai/glm-5.3-flash")
        self.assertEqual(glm["provider_only"], ["z-ai"])
        self.assertFalse(glm["allow_fallbacks"])
        self.assertEqual(glm["reasoning"], {"effort": "max", "exclude": False})
        self.assertEqual(glm["max_paid_usd_per_day"], 0.25)

    def test_gemini_38_config_pins_google_vertex_with_medium_reasoning(self) -> None:
        gemini = next(
            model
            for model in load_models()
            if model["series_id"]
            == "openrouter-google-gemini-3.8-flash-json-event-v4"
        )

        self.assertEqual(gemini["model"], "google/gemini-3.8-flash")
        self.assertEqual(gemini["provider_only"], ["google-vertex"])
        self.assertFalse(gemini["allow_fallbacks"])
        self.assertEqual(gemini["reasoning"], {"effort": "medium", "exclude": False})

    def test_openrouter_uses_per_model_reasoning_and_token_budget(self) -> None:
        captured = {}

        def urlopen(request, timeout):
            self.assertEqual(timeout, 180)
            captured.update(json.loads(request.data))
            return _Response()

        config = {
            "gateway": "openrouter",
            "model": "z-ai/glm-5.2:free",
            "api_key_env": "TEST_OPENROUTER_KEY",
            "max_tokens": 32768,
            "reasoning": {"effort": "high", "exclude": False},
            "provider_only": ["z-ai"],
            "allow_fallbacks": False,
        }
        with patch.dict("os.environ", {"TEST_OPENROUTER_KEY": "secret"}), patch(
            "urllib.request.urlopen", side_effect=urlopen
        ):
            provider = ModelProvider(config, Settings.load())
            provider.complete_json(
                [{"role": "user", "content": "Return JSON"}],
                "test",
                {"type": "object"},
            )

        self.assertEqual(captured["max_tokens"], 32768)
        self.assertEqual(
            captured["reasoning"], {"effort": "high", "exclude": False}
        )
        self.assertEqual(
            captured["provider"], {"allow_fallbacks": False, "only": ["z-ai"]}
        )
        self.assertEqual(
            provider.attempts[0]["request_reasoning"],
            {"effort": "high", "exclude": False},
        )
        self.assertEqual(provider.attempts[0]["request_max_tokens"], 32768)

    def test_model_can_extend_request_timeout_for_reasoning(self) -> None:
        observed = {}

        def urlopen(_request, timeout):
            observed["timeout"] = timeout
            return _Response()

        config = {
            "gateway": "openrouter",
            "model": "z-ai/glm-5.2:free",
            "api_key_env": "TEST_OPENROUTER_KEY",
            "request_timeout_seconds": 300,
        }
        with patch.dict("os.environ", {"TEST_OPENROUTER_KEY": "secret"}), patch(
            "urllib.request.urlopen", side_effect=urlopen
        ):
            ModelProvider(config, Settings.load()).complete_json(
                [{"role": "user", "content": "Return JSON"}],
                "test",
                {"type": "object"},
            )

        self.assertEqual(observed["timeout"], 300)

    def test_model_can_extend_retry_schedule_for_shared_free_capacity(self) -> None:
        attempts = 0

        def urlopen(_request, timeout):
            nonlocal attempts
            self.assertEqual(timeout, 180)
            attempts += 1
            if attempts < 4:
                raise TimeoutError("shared provider still busy")
            return _Response()

        config = {
            "gateway": "openrouter",
            "model": "z-ai/glm-5.2:free",
            "api_key_env": "TEST_OPENROUTER_KEY",
            "retry_delays_seconds": [0, 5, 20, 60],
        }
        with patch.dict("os.environ", {"TEST_OPENROUTER_KEY": "secret"}), patch(
            "urllib.request.urlopen", side_effect=urlopen
        ), patch("time.sleep") as sleep:
            ModelProvider(config, Settings.load()).complete_json(
                [{"role": "user", "content": "Return JSON"}],
                "test",
                {"type": "object"},
            )

        self.assertEqual(attempts, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 20, 60])

    def test_deepseek_uses_direct_endpoint_and_enables_thinking(self) -> None:
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
            "request_extra": {
                "thinking": {"type": "enabled"},
                "reasoning_effort": "high",
            },
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
        self.assertEqual(captured["body"]["thinking"], {"type": "enabled"})
        self.assertEqual(captured["body"]["reasoning_effort"], "high")

    def test_deepseek_forecast_series_keeps_high_reasoning_with_room_to_finish(self) -> None:
        model = next(
            candidate
            for candidate in load_models()
            if candidate["series_id"] == "deepseek-v4-flash-direct-json-event-v5"
        )
        self.assertEqual(model["request_extra"]["reasoning_effort"], "high")
        self.assertEqual(model["request_extra"]["thinking"], {"type": "enabled"})
        self.assertEqual(model["max_tokens"], 65536)

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
        self.assertEqual(len(provider.attempts), 1)
        self.assertEqual(
            provider.attempts[0]["raw_response"]["choices"][0]["message"]["content"],
            '{"unfinished":',
        )
        self.assertIn("JSONDecodeError", provider.attempts[0]["error"])


if __name__ == "__main__":
    unittest.main()
