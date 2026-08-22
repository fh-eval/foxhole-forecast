# Foxhole Forecast

Foxhole Forecast is a prospective LLM evaluation: every three hours, several models receive the same cutoff-safe public war data and estimate which strategic bases will change ownership in the next 1, 6, and 24 hours. Later API observations settle those predictions, and a static dashboard publishes calibration, event, and ETA scores.

The implementation deliberately starts with the [official Foxhole War API](https://github.com/clapfoot/warapi). FoxholeStats or other community data can be added as a separately versioned data source later; it is not scraped by this version.

## What the evaluation measures

Each model run has two fixed calls:

1. A scout call selects up to six regions from a compact whole-map packet.
2. A forecast call receives detailed history for exactly those regions and returns base-level probabilities, exact event bets, ETAs, and evidence references.

Every model sees data with the same UTC cutoff. Local validation rejects malformed output, unknown bases or evidence IDs, non-monotonic probabilities, invalid actors, and ETAs outside the 24-hour window. One correction attempt is allowed. A model may omit bases, but every omitted strategic base is scored as a 0% prediction. This prevents selective coverage from inflating the result.

The primary metric is the integrated Brier score across 1h, 6h, and 24h base-change outcomes:

```text
Brier = mean((forecast probability - observed outcome)^2)
Brier skill = 100 * (1 - model Brier / zero-change baseline Brier)
```

Lower Brier is better; positive skill beats the always-zero baseline. The dashboard also reports exact-event Brier score, hit/miss counts, median ETA error, and the share of matched ETAs within 15, 30, 60, and 180 minutes. Evidence relevance is preserved for audit and display, not treated as truth by an LLM judge.

A result is censored instead of guessed when collector coverage has a gap longer than two polling intervals or an ownership transition straddles a cutoff/deadline. Because the API is sampled every 15 minutes, event time is an observation interval rather than an exact instant.

## Repository layout

```text
config/                    Evaluation and model-series configuration
data/                      Append-only observations, forecasts, and scores
src/foxhole_forecast/      Collector, packet builder, adapters, validator, scorer
tests/                     Deterministic unit tests
web/                       Astro GitHub Pages dashboard
.github/workflows/         Collection, CI, and Pages automation
```

The configured model series are GPT-5.6 Luna through OpenRouter (pinned to OpenAI), Gemini 3.7 Flash through OpenRouter (pinned to Google Vertex), Inkling and Nemotron 3 Ultra 550B A55B through NVIDIA NIM, and Ox Alpha through OpenRouter. GLM 5.2 and DeepSeek V4 Flash remain defined but disabled after NVIDIA retired their endpoints. Gateway, requested model, returned model, and upstream provider are recorded so silent routing changes cannot masquerade as the same series. Raw provider responses remain in the repository data but are excluded from the public dashboard JSON.

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

To run model forecasts, export one or both provider keys first:

```bash
cd /path/to/foxhole-forecast
export OPENROUTER_API_KEY="..."
export NVIDIA_API_KEY="..."
PYTHONPATH=src python -m foxhole_forecast forecast --force
```

`run` performs collection, a forecast when the current three-hour slot is due, settlement, and dashboard generation in one command. The paid-model software cap shared by Luna and Gemini is `$0.25` per UTC day in `config/settings.json`; provider-reported cost is recorded, with current published token rates used as a fallback. Set an independent OpenRouter spending limit as the stronger backstop. Missing keys skip the affected series without stopping collection or scoring.

## Evaluation cautions

- This is a base-ownership forecast, not a claim about server state unavailable through the public API.
- Initial collection creates a baseline and no synthetic capture event. Only a later observed transition can become an outcome.
- Direct faction-to-faction changes count as a loss and a capture. Faction-to-neutral counts as a loss and neutralization. A temporarily absent map item is not called destroyed.
- Model comparisons are only meaningful within the same cohort and schema version. Change prompts, packets, horizons, strategic icon types, or provider routing by starting a new versioned series.
- Results need enough completed 24-hour cohorts and positive base changes before Brier skill becomes informative. Early leaderboards should be read as provisional.

This project is an independent experiment and is not affiliated with Siege Camp. Foxhole and its related marks belong to their respective owners.
