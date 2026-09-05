# What this experiment can tell us

Foxhole Forecast asks whether models make different calls from the available public data, and where their answers break down: choosing active bases, naming the ownership change, predicting its timing, or following the output contract. It is an exploratory, budget-constrained comparison. It does not yet establish a best model, well-calibrated probabilities, or an advantage over a simple probability-and-timing forecasting baseline.

## Preserve the experiment already collected

The model's original output, cutoff-time evidence, prompts, schema, and provider identity are the evidence. Changing a page explanation or deriving another diagnostic from those records does not require another inference call. Historical predictions and their original series identities remain intact.

This clarification keeps the existing score formula, horizons, model prompts, packets, and output schema. Stored `crps_minutes` and protocol names remain compatible with existing rounds. Validation hardening isolates malformed rows under the existing contract; it does not invent replacement forecasts or automatically rewrite previously invalid runs.

A future alternative scoring rule should be a separately named and versioned derived analysis with its eligibility rules recorded. It may reuse old predictions where they contain the necessary information, but should not silently replace their original score interpretation. Missing historical information remains missing. Changes to what models see or must predict require a new prospective series.

## What the current score measures

Each bet names a base and outcome, supplies confidence, gives an ETA, and specifies timing uncertainty. Its scoring window runs from the data cutoff to that ETA plus three hours. The model's Normal timing distribution is truncated to that window and scaled by its confidence.

The loss uses the existing outcome-credit rules: an exact outcome gets 1, certain capture/destruction near-matches get 0.75, and an incorrect outcome gets 0. For example, an enemy capture prediction can get 0.75 when a base becomes neutral and the capture does not complete. This is separate from the exact-outcome count, which does not award fractional successes.

The implementation integrates `E[(F(t) - credit * I(T <= t))²]` over the bet's window, averaging uniformly over the observed event interval. Ordinary [CRPS](https://scoringrules.readthedocs.io/en/latest/weighted_scores.html) uses an observed-event indicator without that partial-credit multiplier. We therefore describe the existing metric as **partial-credit CRPS-style loss**, not as a proper score of the exact named event probability. Its minute unit measures integrated loss; it is not average ETA error.

The 0–100 display score averages short and long normalized losses with equal weight. Its fixed 540- and 1,620-minute divisors are maximum windows, not skill benchmarks. A high display score is not a percentage of correct predictions. A shorter chosen ETA also shortens the integration window, so the score alone cannot isolate probability quality from the model's choice of task and window.

Legacy bets without model-authored timing uncertainty use the existing confidence-based fallback and retain their `sigma_source`. They should not be interpreted as direct evidence about the model's ability to specify timing uncertainty.

## Keep usable answers and explain exclusions

| State | Meaning | Treatment |
| --- | --- | --- |
| Scored | The settlement rules provide an outcome and timing loss | Included in the applicable score and outcome counts |
| Open | The deadline or a required capture follow-up is still pending | Retained and revisited on later collection; excluded for now |
| Censored | Coverage, interval ambiguity, or the war ending prevents settlement under the rules | Retained with a reason; excluded rather than counted as a miss |
| Dropped | An individual model-authored bet violates the output contract | Retained in the raw response with an exclusion record; valid neighboring bets remain eligible |
| Invalid run | No usable forecast survives, or the call/output cannot be processed | Run status and raw response, when received, remain auditable; no guessed set of eight failed bets |

Validation checks base identity, rank, allowed outcome, probability, uncertainty, a timezone-aware ETA, and evidence references against the frozen packet. Malformed JSON values must fail as individual validation errors rather than crash processing of otherwise valid neighboring rows. Duplicate ranks/bases remain invalid; filtering keeps the first valid occurrence in response order and never renumbers the survivors. Adviser recommendations are validated separately from forecast bets.

Evidence is part of the current output contract, so a bad evidence reference still excludes that bet. We do not replace the citation or supply missing probability/timing values to keep it. An advisory-field error should not discard the forecast bets. Raw responses preserve what was actually returned, including malformed entries.

Coverage is checked against the nominal 15-minute cadence with a two-poll tolerance. A gap does not automatically discard an entire round. Settlement is per bet, and the existing coarse-interval rule retains timing credit when every possible time in the interval receives the same credit. That rule is an assumption of this experiment, not recovery of an exact event time. Interval averaging also does not reconstruct unobserved intervening ownership changes.

Retention counts cover the published rounds used in the dashboard view. A bet-level denominator is published bets plus recorded dropped bets, not eight times all attempted calls. It excludes attempts with no usable forecast, missing slots, and earlier correction responses; those are run-level failures, not invented bets. Censored and dropped reason counts describe different sources of missing scores and should not be combined into a model error rate. Older records without a reason are labeled as such rather than assigned a guessed explanation.

## Read differences as clues

Use the counts in this order:

1. **Answer validity:** does a model name possible outcomes and use valid identifiers? Look at exclusions alongside successful published bets, and inspect failed runs separately.
2. **Base selection:** does it choose bases where transitions happen? Compare with the scouted-region and whole-map selection baselines.
3. **Outcome:** among the evaluated bases that changed, how often does it name the exact change? Read that conditional denominator alongside the count of all evaluated bets.
4. **Timing:** how often is the exact outcome within three hours of the ETA? Read short and long timelines separately.
5. **Confidence and uncertainty:** inspect model-authored values and the custom loss as exploratory diagnostics. Neither the loss nor timing-band coverage on successful predictions establishes overall calibration.

The live summaries pool each model's own available rounds. Models may participate in different periods, and observed events can become scoreable before no-event bets reach their deadlines. Bets also share bases, time windows, and war conditions. Consequently, differences may reflect case mix, maturity, or correlated outcomes as well as model behavior. Round history permits inspection of comparable cases; the aggregate is not a paired statistical comparison and does not report confidence intervals.

Delayed replays use frozen inputs but are generated later, sometimes after an outcome. They are labeled separately and included in the existing descriptive aggregates. Frozen-input replay does not make a late submission a live prospective call. No-event misses still require coverage; third-party outage recovery remains provenance-tagged.

These limits leave the original questions useful: patterns can suggest what is difficult and motivate a future test. Stronger claims require an explicitly defined comparison on shared, mature cases and an appropriate benchmark, not more inference on the existing rounds.
