# Free Nemotron through OpenRouter

## Outcome

Add an enabled forecast series for OpenRouter's free Nemotron 3 Ultra endpoint, pinned to the official Nvidia upstream and requesting high reasoning. Preserve the disabled direct NVIDIA NIM series and all historical records under their original identity.

## Ordered tasks

1. Add a distinct `series_id`, clear label, and model `nvidia/nemotron-3-ultra-550b-a55b:free` in `config/models.json`. Use the existing OpenRouter credential, `provider_only: ["nvidia"]`, no provider fallbacks, high reasoning, and the existing Nemotron output ceiling and timeout. Mark it free.
2. Let an OpenRouter model explicitly omit `response_format` while preserving current request bodies for every existing series. The Nvidia free endpoint does not advertise `response_format`; prompts and local parsing/validation still require JSON-shaped answers.
3. Add deterministic tests for the configured series, exact outbound request, unchanged existing request formats, and cohort enablement. Verify all intersecting consumers, including war settings, dashboard identity, budget accounting, retry/replay, and workflow model discovery.
4. Update roster prose. Run focused tests, independent review, then the full integration gates before merging.

## Non-goals

Do not rewrite past Nemotron runs, reuse the direct-NIM series ID, alter prompts/scoring/settlement, relax local forecast validation, enable provider fallback, or make a live model call during implementation.

## Acceptance examples

- A new cohort includes the OpenRouter free Nemotron series and excludes the disabled direct-NIM series.
- Every free Nemotron request names `nvidia/nemotron-3-ultra-550b-a55b:free`, pins `provider.only` to `nvidia`, disables fallbacks, requests high reasoning, and omits `response_format`.
- Existing OpenRouter models still send their former `json_schema` or `json_object` format; direct NIM and DeepSeek still send `json_object`.
- A free endpoint's invalid/non-JSON answer is recorded as invalid through the existing parser/validator, never accepted merely because the provider omitted schema enforcement.
