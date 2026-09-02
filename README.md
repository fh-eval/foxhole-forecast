# Foxhole Forecast

Foxhole Forecast is a prospective LLM evaluation: every three hours, several models receive the same cutoff-safe public war data and make eight probabilistic, exact-ETA predictions about strategic base transitions. Later API observations settle those predictions, and a static dashboard publishes short- and long-range CRPS results.

The implementation uses the [official Foxhole War API](https://github.com/clapfoot/warapi) by default. Provenance-tagged FoxholeStats event logs can recover documented polling outages without being presented as official observations.

## What the evaluation measures

Each model run has two calls:

1. A war-overview call describes what the model sees across a compact whole-map packet and selects up to six of the most active regions.
2. A forecast call receives detailed history for exactly those regions and returns eight ranked event bets with an outcome, confidence, ETA, timing uncertainty, and evidence references.

Every model sees data with the same UTC cutoff. Local validation rejects malformed output, unknown bases or evidence IDs, invalid outcomes, and ETAs outside the 24-hour window. Invalid individual bets can be dropped without discarding the rest of a forecast round.

Before a provider call, the evaluator stores a hashed replay bundle containing the exact model configuration, prompts, schemas, scout packet, and the complete cutoff-safe detail source. The shared detail source is deterministically gzip-compressed, while its SHA-256 digest covers the canonical uncompressed JSON. If a free provider has a transient outage, the trusted workflow may later submit that bundle without exposing any newer war data. The failed live run remains immutable; a successful result is appended and visibly labeled as a delayed replay, with its generation time, delay, source commit, and input hashes. Live and replay run counts are reported separately.

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

The configured model series are GPT-5.6 Luna through OpenRouter (pinned to OpenAI), Gemini 3.7 Flash through OpenRouter (pinned to Google Vertex), GLM 5.3 Flash through OpenRouter (pinned to Z.AI), DeepSeek V4 Flash through DeepSeek's direct API, and Nemotron 3 Ultra 550B A55B through NVIDIA NIM. Gateway, requested model, returned model, upstream provider, and requested reasoning settings are recorded so silent routing or reasoning changes cannot masquerade as the same series. Raw provider responses remain auditable as SHA-256-addressed deterministic gzip objects under `data/objects/`; `model_runs.jsonl` stores verified references and the public dashboard excludes those raw payloads. Historical Ox Alpha runs retain their original label and series identity.

The static site is built from three purpose-specific dashboard shards generated directly from repository data. The former combined dashboard payload is intentionally not versioned, avoiding a large derived-file rewrite after every observation while preserving the same public history.

## Run locally

Requirements are Python 3.11+ and Node.js 24.

```bash
cd /path/to/foxhole-forecast
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m foxhole_forecast collect
PYTHONPATH=src python -m foxhole_forecast score
PYTHONPATH=src python -m foxhole_forecast build-dashboard
# One-time/idempotent migration for legacy inline provider responses:
PYTHONPATH=src python -m foxhole_forecast compact-model-runs
# Reproducible copy-only archive for an ended war, plus an offline integrity check:
PYTHONPATH=src python -m foxhole_forecast archive-war --war-number 139
PYTHONPATH=src python -m foxhole_forecast verify-war-archive --war-number 139
# Preview pruning an archived war; mutation requires the explicit --apply flag:
PYTHONPATH=src python -m foxhole_forecast prune-archived-war --war-number 139
# For an invalid run that has a verified frozen bundle:
PYTHONPATH=src python -m foxhole_forecast replay-run --run-id RUN_ID

cd web
npm ci
npm run build
```

War archives under `data/archives/` contain only records associated with the selected ended war, including frozen packets and referenced provider-response objects. Artifacts use deterministic gzip and record their byte-level SHA-256 hashes, sizes, counts, and source-file hashes in the manifest. An existing archive is verified instead of overwritten; creating one does not prune or rewrite canonical operational data.

Dashboard builds and all-time score aggregation merge verified archive records with live data, deduplicating them while preferring live copies. Forecast generation, recovery, and model-health checks remain live-data-only, so archived history cannot enter a new model prompt or trigger an operational incident.

`prune-archived-war` is a dry run unless `--apply` is supplied. It verifies the archive first, refuses to remove unarchived cohort files, and deletes provider-response objects only when no remaining live run references them. The war registry and generated all-time scores stay in live storage.

### Optional FoxholeStats import

Historical context can be imported from a saved FoxholeStats event-log page:

```bash
PYTHONPATH=src python -m foxhole_forecast import-foxholestats --html /path/to/foxholestats.html
```

The importer preserves source IDs, URL, timestamp precision, and a SHA-256 provenance manifest in `data/imports/`. By default it stops at the first official poll. With `--recover-gaps`, it selects only official polling gaps longer than two expected intervals, records simulated 15-minute coverage separately, and permits the matched third-party ownership events to settle affected bets. Those scores retain FoxholeStats provenance in the settlement and public dashboard; `data/events.jsonl` remains official-only.

`run` performs collection, a forecast when the current three-hour slot is due, settlement, and dashboard generation in one command. The legacy shared OpenRouter software ceiling is `$3.00` per UTC day. GLM 5.3 Flash has an independent `$0.25` daily ceiling, and direct DeepSeek usage has an independent `$0.50` ceiling. Provider-reported cost is recorded, with published token rates used as a conservative fallback. Missing keys skip the affected series without stopping collection or scoring.

## Evaluation cautions

- This is a base-ownership forecast, not a claim about server state unavailable through the public API.
- Initial collection creates a baseline and no synthetic capture event. Only a later observed transition can become an outcome.
- Direct faction-to-faction changes count as a loss and a capture. Faction-to-neutral counts as a loss and neutralization. A temporarily absent map item is not called destroyed.
- Model comparisons are only meaningful within the same cohort and schema version. Change prompts, packets, horizons, strategic icon types, or provider routing by starting a new versioned series.
- Results need enough settled short- and long-range bets before comparisons become stable. Early leaderboards should be read as provisional.

This project is an independent experiment and is not affiliated with Siege Camp. Foxhole and its related marks belong to their respective owners.
