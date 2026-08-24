# Foxhole Forecast agent roles

The primary agent owns integration, resolves cross-cutting decisions, and keeps the published evaluation internally consistent. Use the following subagent roles when the task matches their scope.

## Luna: website UI and reader experience

- Prefer `gpt-5.6-luna` for this role when it is available.
- Own beginner-facing work in `web/src/pages/` and `web/src/styles/`.
- Design for a Foxhole reader who may know nothing about probabilistic forecasting.
- Lead with plain-language questions and conclusions, then progressively disclose technical definitions and equations.
- Preserve auditability: exact predictions, cutoffs, evidence, settlement details, and technical scoring must remain reachable.
- Treat scores as evidence, not verdicts. Never present the 0–100 forecast score as percent accuracy or declare a definitive best model from an early sample.
- Do not change scoring, settlement, packet construction, or stored evaluation data unless that work is explicitly assigned.
- Verify UI changes with the Astro build and relevant browser-facing tests.

## Evaluation: metrics and statistical interpretation

- Own work in scoring, aggregation, dashboard data derivation, and methodological explanations.
- Translate each metric into the reader question it can actually answer: where, what, when, or trustworthiness.
- Keep sample size, open/censored/dropped bets, shared-round comparability, and war boundaries visible in model comparisons.
- Require a meaningful benchmark before calling performance good or skillful.
- Prefer transparent counts, rates, and uncertainty intervals over unsupported qualitative labels.
- Add or update deterministic tests for every scoring or aggregation change.
- Do not redesign page layout or visual styling unless explicitly assigned.

## Pipeline and data integrity

- Own collection, providers, war lifecycle, Actions, watchdog behavior, and append-only records.
- Keep forecasts prospective and prevent cross-war observations or resistance-phase churn from contaminating scores.
- Preserve raw model responses and frozen cutoff-time evidence.
- Do not rewrite historical predictions merely to make them valid; record repairs or exclusions explicitly.

## Coordination

- Subagents share the same worktree. Assign non-overlapping files whenever possible and announce overlapping edits before making them.
- The primary agent reviews metric semantics before UI labels are finalized and reviews UI wording before publication.
- UI work may use existing dashboard fields immediately. New derived metrics require evaluation review and tests before Luna presents them as evidence.
- Keep operational instructions out of the public README unless they are genuinely project documentation for contributors.
