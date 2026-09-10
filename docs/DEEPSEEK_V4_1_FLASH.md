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

## Endpoint retirement safety

### Outcome

If DeepSeek removes V4 Flash from its account-visible model catalog and serves
only V4.1 Flash, forecast cohorts must run V4.1 only. They must not spend a
second call on a withdrawn V4 alias, record V4.1 results under the V4 series,
or open a model-failure incident for that documented retirement state.

### Required behavior

1. Before a normal cohort calls the direct DeepSeek models, fetch the
   no-generation `/models` catalog once for that credential.
2. If the catalog is usable and V4 Flash is absent, append an auditable
   `skipped_provider_unavailable` V4 run with the catalog evidence/reason;
   continue to run V4.1. Treat that explicit retirement skip as healthy in the
   model-run audit. A missing V4.1 entry remains a real failure.
3. Require each DeepSeek response to report the model identity expected by its
   configuration. A mismatch (including the V4 request returning
   `deepseek-flash`) is invalid and must retain the raw response/audit details.
   The same protection applies to delayed replay. Salvage must not turn such a
   mismatch into a valid run.
4. If catalog lookup itself fails, retain the existing call behavior and alert
   semantics rather than treating a transient catalog outage as proof of model
   retirement.

### Acceptance examples

- Catalog: `{deepseek-flash}` produces a V4 retirement skip and a V4.1 call;
  the health audit reports no incident for the intentional V4 skip.
- Catalog: `{deepseek-v4-flash, deepseek-flash}` calls both models.
- A V4 response labelled `deepseek-flash` is invalid, even when its JSON is
  otherwise valid; a later salvage attempt remains refused.
- Missing credentials and catalog-request errors preserve the prior per-model
  missing-key/failure handling.

### Limit

This can verify the account-visible catalog and the model ID returned by the
provider. No client can prove which weights ran if DeepSeek deliberately keeps
both identifiers listed and reports the requested legacy identifier while
silently changing its backing model; the raw catalog (when it causes a
retirement skip) and returned identities remain stored to make that limitation
auditable.
