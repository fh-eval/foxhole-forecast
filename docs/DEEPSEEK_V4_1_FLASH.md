# DeepSeek V4.1 Flash cohort change

## Outcome

Start a distinct DeepSeek V4.1 Flash series in future forecast cohorts. Keep
the existing DeepSeek V4 Flash series, including its historical records and
identity, intact. When both direct API model IDs are available, run both in the
same cohort so their results remain directly comparable.

## Work and dependencies

1. Add a V4.1 Flash model configuration using the released direct-API alias
   `deepseek-flash`, with a new, stable series ID and reader-facing label.
2. Retain `deepseek-v4-flash` and its current series ID as the V4 Flash model.
3. Give the two paid direct models separate daily budget groups, so running
   both does not cause one series to suppress the other or double-spend a
   shared cap unintentionally.
4. Make fallback cost accounting identify the two endpoints separately and
   use the documented current rates for each. Provider-reported cost remains
   authoritative when supplied.
5. Update the operational README and deterministic tests for configuration,
   request identity, and fallback cost behavior.

## Acceptance examples

- `load_models()` returns enabled entries for both `deepseek-v4-flash` and
  `deepseek-flash`, with distinct series IDs and budget groups.
- A new cohort includes both enabled entries when `DEEPSEEK_KEY` is present;
  existing V4 runs and raw evidence names are not renamed or rewritten.
- A V4.1 request records `deepseek-flash` as its requested model, never as
  V4 Flash, and fallback pricing is selected from that exact model ID.
- The budget test demonstrates that spend in V4 Flash cannot consume V4.1
  Flash's daily allowance.

## Non-goals

- Do not rewrite cohorts, run ledgers, scores, or frozen evidence.
- Do not merge V4 and V4.1 dashboard identities or alter forecast/scoring
  semantics.
- Do not probe or invoke paid generation endpoints during this change.

## Availability note

The local environment has no `DEEPSEEK_KEY`, so its account-level model list
cannot be queried here. The direct API's public catalog retains
`deepseek-v4-flash`; the V4.1 Flash release uses `deepseek-flash`. Configure
both explicit model IDs so normal cohorts run both whenever the account exposes
them; ordinary missing-key/provider-error handling still preserves the other
models' cohorts.
