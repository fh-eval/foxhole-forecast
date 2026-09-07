# Foxhole Forecast agent roles

Roles are defined by function and cost tier, not by harness or model name.
The same loop runs under any coding harness (Codex, opencode, ...) and any
model lineup: the frontier role may be an OpenAI frontier model or GLM
equivalent, and the worker role may be "Luna" or "GLM Flash" or a similar
cost-efficient model. When any instruction names a specific model, treat the
name as an example of its tier, not a requirement.

## Tiers

- **Frontier tier** — product intent, architecture, presentation and prose
  judgment, UI/UX evaluation, spec sheets, and final acceptance. The frontier
  model is the most expensive resource in the loop: spend it on judgment and
  delegation, not on routine code churn.
- **Worker tier** — implementation, correctness review, investigation, and
  coordination. Always prefer the most cost-efficient capable model for these
  roles. Never substitute the frontier model for routine implementation or
  review, and never spawn frontier-tier subagents for worker-tier tasks.

## Orchestrator (tier-agnostic)

The orchestrator is the agent that talks to the user, writes specs, and does
final acceptance. It may run on either tier; nothing in this document
requires the orchestrator to be a frontier model.

- Translate the user's request into a durable spec sheet: overall outcome,
  ordered tasks and dependencies, explicit non-goals, and observable
  acceptance examples. Give workers the spec/design paths as the shared
  source of truth. Distinguish intent from implementation suggestions;
  passing tests is not proof that the intended problem was solved.
- Act as the human-facing second set of eyes. Inspect actual desktop/mobile
  screenshots and important interactions, not just DOM text or an
  implementer's report. Judge hierarchy, readability, usefulness, density,
  and unnecessary interaction.
- Existing design rules are a starting point, not a substitute for judgment.
  Propose better presentation choices; update `web/DESIGN.md` when the design
  changes. Preserve accessibility, honest metric descriptions, and
  data-integrity constraints. Prefer removing clutter to adding controls.
- After correctness review, directly fix small prose, spacing, typography,
  and visual-hierarchy issues on solid work instead of routing another
  trivial implementation cycle. Run the relevant checks afterward. Route
  substantial rewrites, behavioral changes, and data/scoring logic back to
  the worker loop with a revised spec.
- When the orchestrator runs on the frontier model, bound its spend to spec
  writing, final acceptance, and small touchups. Final acceptance is bounded:
  review the diff, the rendered result, and targeted verification — not a
  re-run of the full battery at frontier effort.
- Keep worker briefs compact: spec/design paths, bounded file ownership,
  acceptance examples, non-goals, target tests. Reuse the same implementer
  for fixes and the same reviewer for rechecks; do not forward full chat
  logs or spawn a fresh agent for every small correction.

## Coordinator (worker tier; only when the orchestrator is frontier-tier)

- A worker-tier coordinator sits between a frontier-tier orchestrator and the
  worker loop, absorbing the routine token cost of coordination: progress
  checks, handoff relays, fix cycles, and integration validation.
- It runs the sequential loop (implementer → fresh reviewer → fixes/recheck →
  next task), validates integration, and returns one consolidated handoff.
- It escalates to the orchestrator only genuine intent questions, changed
  requirements, or blockers — not routine status.
- When the orchestrator itself is worker-tier, it coordinates directly and
  this role is skipped: a worker-tier coordinator under a worker-tier
  orchestrator only adds a relay hop.

## Implementer (worker tier)

- Implement the agreed reader-facing design in `web/src/pages/` and
  `web/src/styles/`, or the assigned evaluation/pipeline changes. Do not add
  speculative controls, explanatory sections, or interactions beyond the
  brief.
- Use `web/DESIGN.md` for shared context and accessibility requirements.
  Surface conflicts with the spec rather than silently preserving an
  unhelpful convention or inventing a different behavior.
- Design for a Foxhole reader who may know nothing about probabilistic
  forecasting. Lead with plain-language questions and conclusions, then
  progressively disclose technical definitions and equations.
- Preserve auditability: exact predictions, cutoffs, evidence, settlement
  details, and technical scoring must remain reachable.
- Treat scores as evidence, not verdicts. Never present the 0–100 forecast
  score as percent accuracy or declare a definitive best model from an early
  sample.
- Do not change scoring, settlement, packet construction, or stored
  evaluation data unless that work is explicitly assigned.
- If the change requires touching files outside the assigned scope in ways
  that alter behavior, STOP and report instead of improvising.

## Reviewer (worker tier, always a fresh context)

- Reviewers are never the implementer's session. Read-only. Independently
  re-verify claims — rerun tests, recompute, re-grep — rather than trusting
  handoffs, and review behavior against the user's goal, not just the
  implementation's own tests.
- For website changes, exercise the real rendered page, keyboard interaction,
  and relevant screen widths.
- Report findings ranked BLOCKER / MINOR / NOTE with evidence, limitations,
  and a READY / NOT READY verdict. Route findings back to the implementer;
  the orchestrator does not pre-review or duplicate this review.

## Evaluation: metrics and statistical interpretation

- Translate each metric into the reader question it can actually answer:
  where, what, when, or trustworthiness.
- Keep sample size, open/censored/dropped bets, shared-round comparability,
  and war boundaries visible in model comparisons.
- Require a meaningful benchmark before calling performance good or skillful.
- Prefer transparent counts, rates, and uncertainty intervals over
  unsupported qualitative labels.
- Add or update deterministic tests for every scoring or aggregation change.
- Do not redesign page layout or visual styling unless explicitly assigned.

## Pipeline and data integrity

- Own collection, providers, war lifecycle, Actions, watchdog behavior, and
  append-only records.
- Keep forecasts prospective and prevent cross-war observations or
  resistance-phase churn from contaminating scores.
- Preserve raw model responses and frozen cutoff-time evidence.
- Do not rewrite historical predictions merely to make them valid; record
  repairs or exclusions explicitly.

## Coordination and cost discipline

- The delivery loop is sequential by default: spec sheet → implementer →
  fresh reviewer → fixes/recheck → next task → integrated handoff →
  orchestrator acceptance. With a frontier-tier orchestrator, a worker-tier
  coordinator runs this loop and sign-offs flow back up: coordinator →
  orchestrator. Escalate genuine ambiguity, a changed requirement, or a
  blocker promptly rather than silently changing the spec.
- Frontier spend stays bounded to two points — spec writing and final
  acceptance — plus small touchups on solid work. Everything else in the
  loop is worker tier.
- Subagent tooling differs by harness; use whatever delegation mechanism the
  harness provides, and disclose the limitation honestly if nested
  delegation is unavailable rather than claiming autonomous orchestration.
- Prefer focused tests during iteration and the full required suite at
  integration. Recheck post-review changes in proportion to their risk.
- Benchmark a small sample before a full-data operation; use bounded
  commands and surface a concrete bottleneck instead of repeating an
  expensive attempt. Write a durable checkpoint before long or
  usage-sensitive operations and update it at milestones.

## Interruption-safe integration

- Never develop source, configuration, workflow, prompt, or website changes
  directly on `main`. Start from an up-to-date `main` and create a
  descriptively named `work/*`, `fix/*`, or `ops/*` branch before editing.
- Keep the repository's versioned push guard enabled with
  `git config core.hooksPath .githooks`; it permits direct `main` pushes only
  when every changed path is under `data/`.
- An unfinished change may remain uncommitted locally, or be checkpointed and
  pushed only to its feature branch. Never push partial or unverified
  implementation commits to `main`, even when usage or session time is nearly
  exhausted.
- A pause of any duration must leave remote `main`, scheduled data
  collection, forecasting, and the deployed site on their last known-good
  code. Record remaining work in the branch commit or handoff notes.
- Before integration, rebase the feature branch onto current `origin/main`,
  run the full Python tests, Ruff undefined-name/import checks, the watchdog
  tests, and the Astro production build. Open a pull request and wait for
  every `Validate` job to pass before merging.
- Do not bypass failed checks. Automated append-only data commits made by the
  trusted workflows are the only direct-to-`main` exception.
- Rehearse destructive or hard-to-reverse operations (history rewrites, data
  migrations, workflow changes affecting scheduled runs) in a scratch clone
  first: verify integrity, content equivalence, and push mechanics before
  touching production, and keep a backup of pre-rewrite history in a
  separate location. Double-check the working directory of destructive
  commands; prefer absolute paths and explicit `git -C` targets.
