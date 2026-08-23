# Foxhole Forecast

Foxhole Forecast is a prospective LLM evaluation: every three hours, several models receive the same cutoff-safe public war data and make eight probabilistic, exact-ETA predictions about strategic base transitions. Later API observations settle those predictions, and a static dashboard publishes short- and long-range CRPS results.

The implementation deliberately starts with the [official Foxhole War API](https://github.com/clapfoot/warapi). FoxholeStats or other community data can be added as a separately versioned data source later; it is not scraped by this version.

## What the evaluation measures

Each model run has two calls:

1. A war-overview call describes what the model sees across a compact whole-map packet and selects up to six of the most active regions.
2. A forecast call receives detailed history for exactly those regions and returns eight ranked event bets with an outcome, confidence, ETA, timing uncertainty, and evidence references.

Every model sees data with the same UTC cutoff. Local validation rejects malformed output, unknown bases or evidence IDs, invalid outcomes, and ETAs outside the 24-hour window. Invalid individual bets can be dropped without discarding the rest of a forecast round.

CRPS evaluates the full event-time probability distribution. It incorporates the probability that the named outcome occurs, the exact ETA, and the model's conditional timing uncertainty. Lower CRPS is better and 0 minutes is perfect.

```text
short CRPS = mean CRPS for ranks 1-4
long CRPS = mean CRPS for ranks 5-8
forecast score = 100 * [1 - 0.5(short CRPS / 540) - 0.5(long CRPS / 1620)]
```

The fixed 540- and 1,620-minute scales are the maximum short and long scoring windows. The 0-100 forecast score gives each timeline equal weight and is not percent accuracy. Open and censored bets are excluded, and the score remains pending until a model has a scored bet in both timelines. Evidence relevance is preserved for audit and display, not treated as truth by an LLM judge.

The dashboard separately reports capture precision, any-transition precision, exact-outcome precision, top-ranked capture rate, and capture lift. Capture lift compares the model's selected bases with all strategic bases available at the same round cutoff and observed through the same bet deadlines, so quiet and chaotic periods receive a matched baseline.

A result is censored instead of guessed when collector coverage has a gap longer than two polling intervals or an ownership transition straddles a cutoff/deadline. Because the API is sampled every 15 minutes, event time is an observation interval rather than an exact instant.

## Repository layout

```text
config/                    Evaluation and model-series configuration
prompts/                   Editable scout, forecast, and correction prompts
data/                      Append-only observations, forecasts, and scores
src/foxhole_forecast/      Collector, packet builder, adapters, validator, scorer
tests/                     Deterministic unit tests
web/                       Astro GitHub Pages dashboard
.github/workflows/         Collection, CI, and Pages automation
```

The configured model series are GPT-5.6 Luna through OpenRouter (pinned to OpenAI), Gemini 3.7 Flash through OpenRouter (pinned to Google Vertex), DeepSeek V4 Flash through DeepSeek's direct API, Inkling and Nemotron 3 Ultra 550B A55B through NVIDIA NIM, and Ox Alpha through OpenRouter. Gateway, requested model, returned model, and upstream provider are recorded so silent routing changes cannot masquerade as the same series. Raw provider responses remain in the repository data but are excluded from the public dashboard JSON.

## Run locally

Requirements are Python 3.11+ and Node.js 24.

```bash
cd /path/to/foxhole-forecast
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m foxhole_forecast collect
PYTHONPATH=src python -m foxhole_forecast score
PYTHONPATH=src python -m foxhole_forecast build-dashboard

cd web
npm ci
npm run build
```

### Optional one-time FoxholeStats backfill

Historical context can be imported from a saved FoxholeStats event-log page without allowing community data to settle prospective scores:

```bash
PYTHONPATH=src python -m foxhole_forecast import-foxholestats --html /path/to/foxholestats.html
```

The importer preserves source IDs, URL, timestamp precision, and a SHA-256 provenance manifest in `data/imports/`. It stores the normalized archive separately in `data/historical_events.jsonl` and automatically stops the backfill at the first successful official-API poll to prevent overlap. Only matched strategic ownership events enter cutoff-safe prompt history; `data/events.jsonl` remains official-API-only and is the sole source used to settle forecasts.

`run` performs collection, a forecast when the current three-hour slot is due, settlement, and dashboard generation in one command. The paid-model software cap shared by Luna and Gemini is `$0.25` per UTC day. Direct DeepSeek usage has an independent `$0.10` daily ceiling. Provider-reported cost is recorded, with current published token rates used as a fallback. Missing keys skip the affected series without stopping collection or scoring.

## Evaluation cautions

- This is a base-ownership forecast, not a claim about server state unavailable through the public API.
- Initial collection creates a baseline and no synthetic capture event. Only a later observed transition can become an outcome.
- Direct faction-to-faction changes count as a loss and a capture. Faction-to-neutral counts as a loss and neutralization. A temporarily absent map item is not called destroyed.
- Model comparisons are only meaningful within the same cohort and schema version. Change prompts, packets, horizons, strategic icon types, or provider routing by starting a new versioned series.
- Results need enough settled short- and long-range bets before comparisons become stable. Early leaderboards should be read as provisional.

This project is an independent experiment and is not affiliated with Siege Camp. Foxhole and its related marks belong to their respective owners.
