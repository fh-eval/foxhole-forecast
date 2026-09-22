from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from foxhole_forecast.config import (
    Settings,
    load_dashboard_hidden_series,
    load_dashboard_series_aliases,
    load_models,
)
from foxhole_forecast.forecasting import _transient_provider_failure
from foxhole_forecast.providers import (
    ModelProvider,
    ModelIdentityMismatch,
    ProviderBodyError,
    _cost,
    _parse_json_content,
    _redact_provider_error,
)


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


class _DeepSeekResponse(_Response):
    def read(self) -> bytes:
        return json.dumps(
            {
                "choices": [{"message": {"content": "{\"ok\":true}"}}],
                "model": "deepseek-flash",
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


class _StaticResponse:
    """Stub HTTP response carrying one arbitrary JSON body."""

    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


class ProviderTests(unittest.TestCase):
    def test_deepseek_model_identity_mismatch_is_invalid_but_raw_is_retained(self) -> None:
        config = {
            "gateway": "deepseek",
            "model": "deepseek-v4-flash",
            "series_id": "deepseek-v4-test",
            "api_key_env": "TEST_DEEPSEEK_KEY",
        }
        with patch.dict("os.environ", {"TEST_DEEPSEEK_KEY": "secret"}), patch(
            "urllib.request.urlopen", return_value=_Response()
        ):
            provider = ModelProvider(config, Settings.load())
            with self.assertRaises(ModelIdentityMismatch):
                provider.complete_json(
                    [{"role": "user", "content": "Return JSON"}],
                    "test",
                    {"type": "object"},
                )
        self.assertEqual(provider.attempts[0]["returned_model"], "test/model")
        self.assertIn("raw_response", provider.attempts[0])

    def test_provider_error_redacts_private_fields_recursively(self) -> None:
        detail = json.dumps(
            {
                "error": {
                    "message": "Account user_private needs confirmation",
                    "metadata": {
                        "user_id": "user_private",
                        "account-id": "account_private",
                        "token": "token_private",
                        "code": 403,
                    },
                }
            }
        )

        redacted = json.loads(_redact_provider_error(detail))

        self.assertEqual(redacted["error"]["metadata"]["user_id"], "[REDACTED]")
        self.assertEqual(redacted["error"]["metadata"]["account-id"], "[REDACTED]")
        self.assertEqual(redacted["error"]["metadata"]["token"], "[REDACTED]")
        self.assertEqual(redacted["error"]["metadata"]["code"], 403)
        self.assertEqual(redacted["error"]["message"], "Account [REDACTED] needs confirmation")

    def test_provider_error_redacts_key_and_bearer_token_in_plain_text(self) -> None:
        redacted = _redact_provider_error(
            "request key-private failed with Bearer header.payload.signature",
            "key-private",
        )

        self.assertEqual(
            redacted,
            "request [REDACTED] failed with Bearer [REDACTED]",
        )

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

    def test_deepseek_v41_cost_uses_its_documented_rates(self) -> None:
        cost = _cost(
            "deepseek-flash",
            {
                "prompt_cache_hit_tokens": 1_000_000,
                "prompt_cache_miss_tokens": 1_000_000,
                "completion_tokens": 1_000_000,
            },
        )
        self.assertEqual(cost, 0.753)

    def test_deepseek_models_keep_distinct_series_and_budget_groups(self) -> None:
        models = {
            model["model"]: model
            for model in load_models()
            if model["gateway"] == "deepseek"
        }

        self.assertEqual(models["deepseek-v4-flash"]["series_id"], "deepseek-v4-flash-direct-json-event-v5")
        self.assertEqual(models["deepseek-flash"]["series_id"], "deepseek-v4.1-flash-direct-json-event-v1")
        self.assertNotEqual(
            models["deepseek-v4-flash"]["budget_group"],
            models["deepseek-flash"]["budget_group"],
        )

    def test_retired_deepseek_v4_flash_series_is_configured_but_disabled(self) -> None:
        """Pin the retirement so re-enabling is a deliberate, reviewed edit."""
        models = load_models()
        retired = next(
            model
            for model in models
            if model["series_id"] == "deepseek-v4-flash-direct-json-event-v5"
        )

        self.assertIs(retired.get("enabled"), False)
        self.assertEqual(retired["model"], "deepseek-v4-flash")
        self.assertEqual(retired["expected_returned_model"], "deepseek-v4-flash")
        self.assertEqual(retired["budget_group"], "deepseek-v4-flash-direct")
        self.assertEqual(retired["max_paid_usd_per_day"], 0.5)
        self.assertIs(retired.get("catalog_retirement_skip"), True)
        self.assertEqual(
            load_dashboard_series_aliases().get(
                "deepseek-v4-flash-direct-json-event-v4"
            ),
            "deepseek-v4-flash-direct-json-event-v5",
        )
        self.assertIn(
            "deepseek-v4-flash-direct-json-event-v4", load_dashboard_hidden_series()
        )
        enabled_series = {
            model["series_id"] for model in models if model.get("enabled", True)
        }
        self.assertNotIn("deepseek-v4-flash-direct-json-event-v5", enabled_series)
        enabled_models = {
            model["model"] for model in models if model.get("enabled", True)
        }
        self.assertNotIn("deepseek-v4-flash", enabled_models)
        self.assertIn("deepseek-flash", enabled_models)

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

    def test_muse_spark_contributor_fallback_cost_uses_current_rate(self) -> None:
        cost = _cost(
            "meta/muse-spark-1.3-contributor",
            {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
        )
        self.assertEqual(cost, 0.3)

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

    def test_nemotron_series_is_configured_but_temporarily_disabled(self) -> None:
        """Pin the temporary disable so re-enabling is a deliberate edit."""
        models = load_models()
        nemotron = next(
            model
            for model in models
            if model["series_id"] == "nvidia-nemotron-3-ultra-550b-a55b-event-v4"
        )

        self.assertIs(nemotron.get("enabled"), False)
        self.assertEqual(nemotron["label"], "Nemotron 3 Ultra 550B A55B")
        self.assertEqual(nemotron["gateway"], "nvidia_nim")
        self.assertEqual(nemotron["model"], "nvidia/nemotron-3-ultra-550b-a55b")
        self.assertEqual(nemotron["api_key_env"], "NVIDIA_API_KEY")
        self.assertEqual(nemotron["max_tokens"], 32768)
        self.assertEqual(nemotron["request_timeout_seconds"], 300)
        self.assertIs(nemotron.get("omit_temperature"), True)
        self.assertEqual(nemotron["request_extra"], {"reasoning_effort": "medium"})
        self.assertIs(nemotron.get("paid"), False)
        enabled_series = {
            model["series_id"] for model in models if model.get("enabled", True)
        }
        self.assertNotIn("nvidia-nemotron-3-ultra-550b-a55b-event-v4", enabled_series)
        enabled_models = {
            model["model"] for model in models if model.get("enabled", True)
        }
        self.assertNotIn("nvidia/nemotron-3-ultra-550b-a55b", enabled_models)

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

    def test_gpt_6_luna_is_enabled_as_distinct_openai_series_at_high_reasoning(self) -> None:
        models = load_models()
        gpt_5_6 = next(
            model
            for model in models
            if model["series_id"] == "openrouter-openai-gpt-5.6-luna-event-v4"
        )
        gpt_6 = next(
            model
            for model in models
            if model["series_id"] == "openrouter-openai-gpt-6-luna-event-v1"
        )

        self.assertEqual(gpt_5_6["label"], "GPT-5.6 Luna")
        self.assertEqual(gpt_5_6["model"], "openai/gpt-5.6-luna")
        self.assertIs(gpt_5_6.get("catalog_retirement_skip"), True)
        self.assertEqual(gpt_6["label"], "GPT-6 Luna")
        self.assertEqual(gpt_6["model"], "openai/gpt-6-luna")
        self.assertNotEqual(gpt_6["series_id"], gpt_5_6["series_id"])
        self.assertIs(gpt_6.get("enabled"), True)
        self.assertNotIn("catalog_retirement_skip", gpt_6)
        self.assertEqual(gpt_6["gateway"], "openrouter")
        self.assertEqual(gpt_6["provider_only"], ["openai"])
        self.assertFalse(gpt_6["allow_fallbacks"])
        self.assertEqual(gpt_6["reasoning"], {"effort": "high", "exclude": False})
        self.assertEqual(gpt_6["max_tokens"], gpt_5_6["max_tokens"])
        self.assertEqual(
            gpt_6["request_timeout_seconds"], gpt_5_6["request_timeout_seconds"]
        )
        self.assertIs(gpt_6["omit_temperature"], True)

    def test_gpt_6_luna_fallback_cost_uses_catalog_rates(self) -> None:
        self.assertEqual(
            _cost(
                "openai/gpt-6-luna",
                {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
            ),
            0.6,
        )

    def test_openrouter_model_catalog_uses_models_endpoint(self) -> None:
        observed = {}

        def urlopen(request, timeout):
            observed["url"] = request.full_url
            observed["method"] = request.get_method()
            observed["authorization"] = request.get_header("Authorization")
            observed["timeout"] = timeout
            return _StaticResponse({"data": [{"id": "openai/gpt-6-luna"}]})

        config = {
            "gateway": "openrouter",
            "model": "openai/gpt-6-luna",
            "api_key_env": "TEST_OPENROUTER_KEY",
        }
        with patch.dict("os.environ", {"TEST_OPENROUTER_KEY": "secret"}), patch(
            "urllib.request.urlopen", side_effect=urlopen
        ):
            catalog = ModelProvider(config, Settings.load()).model_catalog()

        self.assertEqual(observed["url"], "https://openrouter.ai/api/v1/models")
        self.assertEqual(observed["method"], "GET")
        self.assertEqual(observed["authorization"], "Bearer secret")
        self.assertEqual(catalog, {"data": [{"id": "openai/gpt-6-luna"}]})

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

    def test_muse_spark_config_pins_contributor_with_medium_reasoning(self) -> None:
        muse = next(
            model
            for model in load_models()
            if model["series_id"]
            == "openrouter-meta-muse-spark-1.3-contributor-event-v4"
        )

        self.assertEqual(muse["label"], "Muse Spark 1.3")
        self.assertEqual(muse["model"], "meta/muse-spark-1.3-contributor")
        self.assertEqual(muse["provider_only"], ["meta"])
        self.assertFalse(muse["allow_fallbacks"])
        self.assertEqual(muse["reasoning"], {"effort": "medium", "exclude": False})
        self.assertNotIn("budget_group", muse)

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

    def test_deepseek_v41_request_records_exact_released_model_id(self) -> None:
        captured = {}

        def urlopen(request, timeout):
            captured.update(json.loads(request.data))
            return _DeepSeekResponse()

        model = next(
            candidate
            for candidate in load_models()
            if candidate["model"] == "deepseek-flash"
        )
        with patch.dict("os.environ", {"TEST_DEEPSEEK_KEY": "secret"}), patch(
            "urllib.request.urlopen", side_effect=urlopen
        ):
            config = {**model, "api_key_env": "TEST_DEEPSEEK_KEY"}
            response = ModelProvider(config, Settings.load()).complete_json(
                [{"role": "user", "content": "Return JSON"}],
                "test",
                {"type": "object"},
            )

        self.assertEqual(captured["model"], "deepseek-flash")
        self.assertEqual(response.requested_model, "deepseek-flash")

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

    def test_openrouter_timeout_envelope_raises_typed_provider_error(self) -> None:
        config = {
            "gateway": "openrouter",
            "model": "google/gemini-3.8-flash",
            "api_key_env": "TEST_OPENROUTER_KEY",
        }
        envelope = {
            "error": {
                "code": 504,
                "message": "A Timeout Occurred",
                "metadata": {"error_type": "timeout"},
            }
        }
        with patch.dict("os.environ", {"TEST_OPENROUTER_KEY": "secret"}), patch(
            "urllib.request.urlopen", return_value=_StaticResponse(envelope)
        ):
            provider = ModelProvider(config, Settings.load())
            with self.assertRaises(ProviderBodyError) as raised:
                provider.complete_json(
                    [{"role": "user", "content": "Return JSON"}],
                    "test",
                    {"type": "object"},
                )

        message = str(raised.exception)
        self.assertIn("upstream error 504", message)
        self.assertIn("A Timeout Occurred", message)
        self.assertIn("timeout", message)
        self.assertNotIn("KeyError", message)
        self.assertEqual(provider.attempts[0]["raw_response"], envelope)
        self.assertEqual(provider.attempts[0]["error"], f"ProviderBodyError: {message}")

    def test_deepseek_timeout_envelope_raises_typed_provider_error(self) -> None:
        config = {
            "gateway": "deepseek",
            "model": "deepseek-flash",
            "series_id": "deepseek-v4-flash-direct-json-event-v5",
            "api_key_env": "TEST_DEEPSEEK_KEY",
        }
        envelope = {
            "error": {
                "message": (
                    "We were unable to start processing your request within the "
                    "900-second timeout limit. Please try again later."
                )
            }
        }
        with patch.dict("os.environ", {"TEST_DEEPSEEK_KEY": "secret"}), patch(
            "urllib.request.urlopen", return_value=_StaticResponse(envelope)
        ):
            provider = ModelProvider(config, Settings.load())
            with self.assertRaises(ProviderBodyError) as raised:
                provider.complete_json(
                    [{"role": "user", "content": "Return JSON"}],
                    "test",
                    {"type": "object"},
                )

        message = str(raised.exception)
        self.assertIn("900-second timeout limit", message)
        self.assertIn("Please try again later.", message)
        self.assertNotIn("ModelIdentityMismatch", message)
        self.assertNotIn("got None", message)
        self.assertEqual(provider.attempts[0]["raw_response"], envelope)
        self.assertEqual(provider.attempts[0]["error"], f"ProviderBodyError: {message}")

    def test_stored_body_errors_drive_the_transient_retry_rule(self) -> None:
        """The stored text of a real envelope classifies like its HTTP twin."""
        cases = {
            "timeout envelope": (
                {
                    "gateway": "openrouter",
                    "model": "google/gemini-3.8-flash",
                    "api_key_env": "TEST_OPENROUTER_KEY",
                },
                {
                    "error": {
                        "code": 504,
                        "message": "A Timeout Occurred",
                        "metadata": {"error_type": "timeout"},
                    }
                },
                True,
            ),
            "authentication envelope that mentions a timeout": (
                {
                    "gateway": "openrouter",
                    "model": "google/gemini-3.8-flash",
                    "api_key_env": "TEST_OPENROUTER_KEY",
                },
                {
                    "error": {
                        "code": 403,
                        "message": "Your session timed out. Please sign in again.",
                        "metadata": {"error_type": "authentication_error"},
                    }
                },
                False,
            ),
            "code-less queue timeout": (
                {
                    "gateway": "deepseek",
                    "model": "deepseek-flash",
                    "api_key_env": "TEST_DEEPSEEK_KEY",
                },
                {
                    "error": {
                        "message": (
                            "We were unable to start processing your request within "
                            "the 900-second timeout limit. Please try again later."
                        )
                    }
                },
                True,
            ),
            "empty choices body": (
                {
                    "gateway": "openrouter",
                    "model": "google/gemini-3.8-flash",
                    "api_key_env": "TEST_OPENROUTER_KEY",
                },
                {"choices": []},
                False,
            ),
        }
        for label, (config, envelope, expected) in cases.items():
            with self.subTest(envelope=label):
                with patch.dict(
                    "os.environ", {config["api_key_env"]: "secret"}
                ), patch(
                    "urllib.request.urlopen", return_value=_StaticResponse(envelope)
                ):
                    provider = ModelProvider(config, Settings.load())
                    with self.assertRaises(ProviderBodyError) as raised:
                        provider.complete_json(
                            [{"role": "user", "content": "Return JSON"}],
                            "test",
                            {"type": "object"},
                        )
                stored = provider.attempts[0]["error"]
                self.assertEqual(stored, f"ProviderBodyError: {raised.exception}")
                self.assertEqual(
                    _transient_provider_failure({"error": stored}),
                    expected,
                )

    def test_stored_parse_errors_drive_the_transient_retry_rule(self) -> None:
        """Real parse failures must classify as retryable from their stored text."""
        config = {
            "gateway": "openrouter",
            "model": "google/gemini-3.8-flash",
            "api_key_env": "TEST_OPENROUTER_KEY",
        }
        cases = {
            "empty content": ("", True),
            "prose without json": ("No bets are warranted this round.", True),
            "json that is not an object": ("[1, 2, 3]", True),
        }
        for label, (content, expected) in cases.items():
            with self.subTest(content=label):
                payload = {
                    "choices": [{"message": {"content": content}}],
                    "model": "google/gemini-3.8-flash",
                    "usage": {"prompt_tokens": 12, "completion_tokens": 3},
                }
                with patch.dict(
                    "os.environ", {"TEST_OPENROUTER_KEY": "secret"}
                ), patch(
                    "urllib.request.urlopen", return_value=_StaticResponse(payload)
                ):
                    provider = ModelProvider(config, Settings.load())
                    with self.assertRaises(
                        (json.JSONDecodeError, ValueError)
                    ) as raised:
                        provider.complete_json(
                            [{"role": "user", "content": "Return JSON"}],
                            "test",
                            {"type": "object"},
                        )
                stored = provider.attempts[0]["error"]
                self.assertEqual(
                    stored,
                    f"{type(raised.exception).__name__}: {raised.exception}",
                )
                self.assertEqual(
                    _transient_provider_failure({"error": stored}),
                    expected,
                )

    def test_empty_choices_body_raises_typed_provider_error(self) -> None:
        config = {
            "gateway": "deepseek",
            "model": "deepseek-flash",
            "series_id": "deepseek-v4-flash-direct-json-event-v5",
            "api_key_env": "TEST_DEEPSEEK_KEY",
        }
        payload = {"choices": [], "model": "deepseek-flash"}
        with patch.dict("os.environ", {"TEST_DEEPSEEK_KEY": "secret"}), patch(
            "urllib.request.urlopen", return_value=_StaticResponse(payload)
        ):
            provider = ModelProvider(config, Settings.load())
            with self.assertRaises(ProviderBodyError) as raised:
                provider.complete_json(
                    [{"role": "user", "content": "Return JSON"}],
                    "test",
                    {"type": "object"},
                )

        message = str(raised.exception)
        self.assertIn("empty choices list", message)
        self.assertNotIn("ModelIdentityMismatch", message)
        self.assertEqual(provider.attempts[0]["error"], f"ProviderBodyError: {message}")
        self.assertEqual(provider.attempts[0]["returned_model"], "deepseek-flash")

    def test_unusable_choices_shapes_raise_typed_provider_error(self) -> None:
        config = {
            "gateway": "openrouter",
            "model": "google/gemini-3.8-flash",
            "api_key_env": "TEST_OPENROUTER_KEY",
        }
        cases = {
            "object choices": ({"a": 1}, "non-list choices value (dict)"),
            "string choices": ("nope", "non-list choices value (str)"),
            "int choices": (5, "non-list choices value (int)"),
            "bool choices": (True, "non-list choices value (bool)"),
            "list entry": ([[1]], "non-object choices entry (list)"),
            "int entry": ([1, 2], "non-object choices entry (int)"),
            "empty entry object": ([{}], "no completion message content"),
            "string message": ([{"message": "text"}], "no completion message content"),
            "message without content": (
                [{"message": {"role": "assistant"}}],
                "no completion message content",
            ),
        }
        for label, (choices, expected) in cases.items():
            payload = {"choices": choices, "model": "google/gemini-3.8-flash", "usage": {}}
            with self.subTest(choices=label):
                with patch.dict("os.environ", {"TEST_OPENROUTER_KEY": "secret"}), patch(
                    "urllib.request.urlopen", return_value=_StaticResponse(payload)
                ):
                    provider = ModelProvider(config, Settings.load())
                    with self.assertRaises(ProviderBodyError) as raised:
                        provider.complete_json(
                            [{"role": "user", "content": "Return JSON"}],
                            "test",
                            {"type": "object"},
                        )

                message = str(raised.exception)
                self.assertIn(expected, message)
                for symptom in ("KeyError", "AttributeError", "TypeError", "IndexError"):
                    self.assertNotIn(symptom, message)
                self.assertEqual(provider.attempts[0]["error"], f"ProviderBodyError: {message}")
                self.assertEqual(provider.attempts[0]["raw_response"], payload)
                self.assertEqual(provider.attempts[0]["reasoning_trace_returned"], False)

    def test_usable_choices_still_record_reasoning_trace(self) -> None:
        config = {
            "gateway": "openrouter",
            "model": "google/gemini-3.8-flash",
            "api_key_env": "TEST_OPENROUTER_KEY",
        }
        payload = {
            "choices": [
                {
                    "message": {
                        "content": "{\"ok\":true}",
                        "reasoning_content": "trace",
                    }
                }
            ],
            "model": "google/gemini-3.8-flash",
            "usage": {"completion_tokens_details": {"reasoning_tokens": 7}},
        }
        with patch.dict("os.environ", {"TEST_OPENROUTER_KEY": "secret"}), patch(
            "urllib.request.urlopen", return_value=_StaticResponse(payload)
        ):
            provider = ModelProvider(config, Settings.load())
            response = provider.complete_json(
                [{"role": "user", "content": "Return JSON"}],
                "test",
                {"type": "object"},
            )

        self.assertEqual(response.parsed, {"ok": True})
        self.assertIs(provider.attempts[0]["reasoning_trace_returned"], True)
        self.assertEqual(provider.attempts[0]["reasoning_tokens"], 7)
        self.assertNotIn("error", provider.attempts[0])

    def test_error_envelope_with_choices_present_still_raises_typed_error(self) -> None:
        config = {
            "gateway": "openrouter",
            "model": "google/gemini-3.8-flash",
            "api_key_env": "TEST_OPENROUTER_KEY",
        }
        cases = {
            "object envelope": (
                {"code": 429, "message": "Rate limit exceeded"},
                "upstream error 429",
            ),
            "string envelope": ("upstream boilerplate", "upstream boilerplate"),
        }
        for label, (error_value, expected) in cases.items():
            payload = {
                "error": error_value,
                "choices": [{"message": {"content": "{\"ok\":true}"}}],
                "model": "google/gemini-3.8-flash",
            }
            with self.subTest(envelope=label):
                with patch.dict("os.environ", {"TEST_OPENROUTER_KEY": "secret"}), patch(
                    "urllib.request.urlopen", return_value=_StaticResponse(payload)
                ):
                    provider = ModelProvider(config, Settings.load())
                    with self.assertRaises(ProviderBodyError) as raised:
                        provider.complete_json(
                            [{"role": "user", "content": "Return JSON"}],
                            "test",
                            {"type": "object"},
                        )

                self.assertIn(expected, str(raised.exception))
                self.assertEqual(len(provider.attempts), 1)
                self.assertEqual(provider.attempts[0]["raw_response"], payload)

    def test_falsy_error_key_keeps_a_valid_response(self) -> None:
        config = {
            "gateway": "openrouter",
            "model": "google/gemini-3.8-flash",
            "api_key_env": "TEST_OPENROUTER_KEY",
        }
        shapes = {
            "absent": {},
            "null": {"error": None},
            "empty string": {"error": ""},
            "empty object": {"error": {}},
            "empty list": {"error": []},
        }
        for label, extra in shapes.items():
            payload = {
                **extra,
                "choices": [{"message": {"content": "{\"ok\":true}"}}],
                "model": "test/model",
                "usage": {},
            }
            with self.subTest(shape=label):
                with patch.dict("os.environ", {"TEST_OPENROUTER_KEY": "secret"}), patch(
                    "urllib.request.urlopen", return_value=_StaticResponse(payload)
                ):
                    provider = ModelProvider(config, Settings.load())
                    response = provider.complete_json(
                        [{"role": "user", "content": "Return JSON"}],
                        "test",
                        {"type": "object"},
                    )

                self.assertEqual(response.parsed, {"ok": True})
                self.assertNotIn("error", provider.attempts[0])
                self.assertEqual(provider.attempts[0]["raw_response"], payload)

    def test_provider_error_detail_is_bounded_for_over_long_fields(self) -> None:
        config = {
            "gateway": "openrouter",
            "model": "google/gemini-3.8-flash",
            "api_key_env": "TEST_OPENROUTER_KEY",
        }
        over_long = 200_000
        envelopes = {
            "code first with short message": {
                "code": "A" * over_long,
                "message": "m",
            },
            "code and message both over long": {
                "code": "A" * over_long,
                "message": "m" * over_long,
            },
            "type over long": {"type": "T" * over_long, "message": "m"},
            "message over long": {"message": "m" * over_long},
        }
        for label, envelope in envelopes.items():
            with self.subTest(field=label):
                with patch.dict("os.environ", {"TEST_OPENROUTER_KEY": "secret"}), patch(
                    "urllib.request.urlopen",
                    return_value=_StaticResponse({"error": envelope}),
                ):
                    provider = ModelProvider(config, Settings.load())
                    with self.assertRaises(ProviderBodyError) as raised:
                        provider.complete_json(
                            [{"role": "user", "content": "Return JSON"}],
                            "test",
                            {"type": "object"},
                        )

                self.assertEqual(len(str(raised.exception)), 1000)
                self.assertEqual(
                    provider.attempts[0]["error"],
                    f"ProviderBodyError: {raised.exception}",
                )
                self.assertEqual(
                    len(provider.attempts[0]["error"]),
                    len("ProviderBodyError: ") + 1000,
                )

    def test_deepseek_wrong_model_with_valid_body_still_raises_identity_mismatch(self) -> None:
        config = {
            "gateway": "deepseek",
            "model": "deepseek-flash",
            "series_id": "deepseek-v4-flash-direct-json-event-v5",
            "api_key_env": "TEST_DEEPSEEK_KEY",
        }
        valid_body = {
            "choices": [{"message": {"content": "{\"ok\":true}"}}],
            "model": "deepseek-v4-flash",
            "usage": {},
        }
        with patch.dict("os.environ", {"TEST_DEEPSEEK_KEY": "secret"}), patch(
            "urllib.request.urlopen", return_value=_StaticResponse(valid_body)
        ):
            provider = ModelProvider(config, Settings.load())
            with self.assertRaises(ModelIdentityMismatch) as raised:
                provider.complete_json(
                    [{"role": "user", "content": "Return JSON"}],
                    "test",
                    {"type": "object"},
                )

        self.assertEqual(
            str(raised.exception),
            "ModelIdentityMismatch: expected deepseek-flash, got deepseek-v4-flash",
        )
        self.assertEqual(provider.attempts[0]["error"], str(raised.exception))
        self.assertEqual(provider.attempts[0]["raw_response"], valid_body)

    def test_non_object_body_raises_typed_provider_error(self) -> None:
        config = {
            "gateway": "openrouter",
            "model": "google/gemini-3.8-flash",
            "api_key_env": "TEST_OPENROUTER_KEY",
        }
        payload = ["unexpected"]
        with patch.dict("os.environ", {"TEST_OPENROUTER_KEY": "secret"}), patch(
            "urllib.request.urlopen", return_value=_StaticResponse(payload)
        ):
            provider = ModelProvider(config, Settings.load())
            with self.assertRaises(ProviderBodyError) as raised:
                provider.complete_json(
                    [{"role": "user", "content": "Return JSON"}],
                    "test",
                    {"type": "object"},
                )

        message = str(raised.exception)
        self.assertIn("not an object (list)", message)
        self.assertNotIn("AttributeError", message)
        self.assertEqual(provider.attempts[0]["raw_response"], payload)

    def test_provider_error_envelope_redacts_private_metadata(self) -> None:
        config = {
            "gateway": "openrouter",
            "model": "google/gemini-3.8-flash",
            "api_key_env": "TEST_OPENROUTER_KEY",
        }
        envelope = {
            "error": {
                "code": 403,
                "message": "Account user_private needs confirmation",
                "metadata": {"user_id": "user_private", "token": "token_private"},
            }
        }
        with patch.dict("os.environ", {"TEST_OPENROUTER_KEY": "secret"}), patch(
            "urllib.request.urlopen", return_value=_StaticResponse(envelope)
        ):
            provider = ModelProvider(config, Settings.load())
            with self.assertRaises(ProviderBodyError) as raised:
                provider.complete_json(
                    [{"role": "user", "content": "Return JSON"}],
                    "test",
                    {"type": "object"},
                )

        message = str(raised.exception)
        self.assertIn("upstream error 403", message)
        self.assertIn("Account [REDACTED] needs confirmation", message)
        self.assertNotIn("user_private", message)
        self.assertNotIn("token_private", message)
        self.assertEqual(provider.attempts[0]["raw_response"], envelope)

    def test_valid_response_is_recorded_without_a_typed_error(self) -> None:
        config = {
            "gateway": "openrouter",
            "model": "google/gemini-3.8-flash",
            "api_key_env": "TEST_OPENROUTER_KEY",
        }
        with patch.dict("os.environ", {"TEST_OPENROUTER_KEY": "secret"}), patch(
            "urllib.request.urlopen", return_value=_Response()
        ):
            provider = ModelProvider(config, Settings.load())
            response = provider.complete_json(
                [{"role": "user", "content": "Return JSON"}],
                "test",
                {"type": "object"},
            )

        self.assertEqual(response.parsed, {"ok": True})
        self.assertEqual(response.requested_model, "google/gemini-3.8-flash")
        self.assertEqual(response.returned_model, "test/model")
        self.assertEqual(response.usage, {})
        self.assertEqual(response.cost_usd, 0.0)
        self.assertEqual(provider.accumulated_cost, 0.0)
        self.assertNotIn("error", provider.attempts[0])
        self.assertNotIn("json_salvaged", provider.attempts[0])
        self.assertEqual(
            provider.attempts[0]["raw_response"]["choices"][0]["message"]["content"],
            "{\"ok\":true}",
        )

    def test_json_salvaged_response_is_unchanged(self) -> None:
        config = {
            "gateway": "openrouter",
            "model": "google/gemini-3.8-flash",
            "api_key_env": "TEST_OPENROUTER_KEY",
        }
        payload = {
            "choices": [
                {"message": {"content": "Here you go:\n```json\n{\"ok\":true}\n```"}}
            ],
            "model": "test/model",
            "usage": {},
        }
        with patch.dict("os.environ", {"TEST_OPENROUTER_KEY": "secret"}), patch(
            "urllib.request.urlopen", return_value=_StaticResponse(payload)
        ):
            provider = ModelProvider(config, Settings.load())
            response = provider.complete_json(
                [{"role": "user", "content": "Return JSON"}],
                "test",
                {"type": "object"},
            )

        self.assertEqual(response.parsed, {"ok": True})
        self.assertIs(provider.attempts[0]["json_salvaged"], True)
        self.assertNotIn("error", provider.attempts[0])


if __name__ == "__main__":
    unittest.main()
