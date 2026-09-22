# GPT-6 Luna roster addition

## Outcome

Add GPT-6 Luna as a distinct, enabled forecast series at high requested reasoning effort. Keep GPT-5.6 Luna and its historical series intact. When OpenRouter's catalog later drops GPT-5.6 Luna, record a proven skip without spending on a doomed call or raising a false missing-model incident. Keep automated failure triage usable after that retirement.

## Tasks and dependencies

1. Add an OpenRouter model entry using the catalog ID `openai/gpt-6-luna`, the existing OpenAI provider route, and the same forecast output envelope as GPT-5.6 Luna. Give it a new stable series ID and label.
2. Add a focused deterministic configuration test that proves the new entry is enabled, distinct from GPT-5.6 Luna, and requests high reasoning.
3. Verify the config and forecast provider tests. Review every consumer that discovers enabled models or displays series labels.
4. Extend model-catalog preflight and health audit to handle a catalog-proven retirement of the configured GPT-5.6 Luna series. Fetch the OpenRouter catalog at most once per forecast operation; a missing key, malformed response, or catalog outage must preserve normal forecasting and alert behavior. Store compact, sufficient evidence with a skipped run; never store the full OpenRouter catalog. Continue to run GPT-6 Luna when 5.6 is absent.
5. Change the model-triage agent's OpenRouter target to GPT-6 Luna and update its local model registration. Test the workflow reference and any catalog logic deterministically.

## Non-goals

No historical prediction changes, scoring changes, prompt changes, war-setting overrides, site layout changes, or live paid model call. Do not silently route a GPT-5.6 Luna series to GPT-6 Luna.

## Acceptance examples

- A new cohort includes both GPT-5.6 Luna and GPT-6 Luna when credentials and budget permit.
- GPT-6 Luna requests `reasoning: {"effort": "high", "exclude": false}` through OpenRouter, with OpenAI-only routing and fallbacks disabled.
- Existing GPT-5.6 Luna records retain their original series ID and label.
- If a valid OpenRouter catalog omits GPT-5.6 Luna, its new run records `skipped_provider_unavailable`, `model_absent_from_catalog`, the checked time and compact catalog evidence; health audit accepts only that proven case. GPT-6 Luna still runs.
- If the catalog request fails or is malformed, forecasting attempts the configured models as before, and failures remain visible to health audit.
