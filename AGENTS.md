# Foxhole Forecast agent roles

The primary agent owns integration, resolves cross-cutting decisions, and keeps the published evaluation internally consistent. Use the following subagent roles when the task matches their scope.

## Luna: website UI and reader experience

- Prefer `gpt-5.6-luna` for this role when it is available.
- Own beginner-facing work in `web/src/pages/` and `web/src/styles/`.
- Follow the recorded design guidelines in `web/DESIGN.md` (type scale, contrast floor, voice rules) and update them when the design language changes.
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

## Interruption-safe integration

- Never develop source, configuration, workflow, prompt, or website changes directly on `main`. Start from an up-to-date `main` and create a descriptively named `work/*`, `fix/*`, or `ops/*` branch before editing.
- Keep the repository's versioned push guard enabled with `git config core.hooksPath .githooks`; it permits direct `main` pushes only when every changed path is under `data/`.
- An unfinished change may remain uncommitted locally, or be checkpointed and pushed only to its feature branch. Never push partial or unverified implementation commits to `main`, even when usage or session time is nearly exhausted.
- A pause of any duration must leave remote `main`, scheduled data collection, forecasting, and the deployed site on their last known-good code. Record remaining work in the branch commit or handoff notes, not in a partially deployed change.
- Before integration, rebase the feature branch onto current `origin/main`, run the full Python tests, Ruff undefined-name/import checks, the watchdog tests, and the Astro production build. Open a pull request and wait for every `Validate` job to pass before merging.
- Do not bypass failed checks. Automated append-only data commits made by the trusted workflows are the only direct-to-`main` exception.

## Multi-agent delivery workflow

- The root agent owns ideas, the implementation overview, and the final check. It does not duplicate implementation or review before that final check.
- The Luna High manager owns task planning, metric/data contracts, worker routing, target tests, integration, and the final full suite. Keep no more than two active implementation workers under the manager.
- Luna High implementers receive fresh compact briefs, own their bounded files, and run their target tests. They must announce any necessary overlap before editing it.
- After implementation is complete, a fresh independent Luna High reviewer reads the finished diff and tests read-only. Route findings back to the implementers for resolution; do not have the root agent pre-review or duplicate the reviewer.
- Use explicit Luna High model/reasoning settings for spawned workers, preserve the repository permission and network fields, and do not silently escalate to a larger model. The manager integrates only after reviewer findings are resolved, then runs the full validation suite and prepares the pull request. Do not merge or deploy before the root's final check.
