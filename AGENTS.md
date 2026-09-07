# Foxhole Forecast agent roles

The root/frontier agent (Astra/Sol) owns product intent, architecture, presentation, prose, and UI/UX. Astra/Sol assigns a spec sheet to a Luna-managed delivery loop. Luna owns routine implementation, investigation, coordination, correctness review, and progression through the assigned tasks; Astra/Sol accepts the completed result or revises its direction.

## Root: product and presentation ownership

- Translate the user's casual request into a durable spec sheet with the overall outcome, ordered tasks/dependencies, explicit non-goals, and observable acceptance examples. Give Luna the spec/design paths as the shared source of truth. Distinguish intent from implementation suggestions; passing tests is not proof that the intended problem was solved.
- Act as the independent human-facing second set of eyes. Inspect actual desktop/mobile screenshots and important interactions, not just DOM text or an implementer's report. Judge hierarchy, readability, usefulness, density, and unnecessary interaction.
- Existing design rules are a starting point, not a substitute for judgment. Propose and make better presentation choices within the user's scope; update `web/DESIGN.md` when the design changes. Preserve accessibility, honest metric descriptions, and data-integrity constraints.
- Prefer removing clutter to adding controls or explanation. Every label, panel, disclosure, and interaction needs a reader benefit. For example, site navigation should remain visible; making it collapsible merely because a disclosure is available adds work without solving a reader problem.
- After correctness review, directly fix small prose, spacing, typography, and visual-hierarchy issues instead of routing another trivial implementation cycle. Run the relevant checks afterward. Route substantial rewrites, behavioral changes, and data/scoring logic back to Luna with a revised spec and targeted review.

## Luna: website implementation and functional review

- Prefer `gpt-5.6-luna` for this role when it is available.
- Implement the agreed reader-facing design in `web/src/pages/` and `web/src/styles/`; do not add speculative controls, explanatory sections, or interactions beyond the brief.
- Use `web/DESIGN.md` for shared context and accessibility requirements. Surface conflicts with the spec rather than silently preserving an unhelpful convention or inventing a different behavior.
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
- The Luna manager owns the delivery loop: assignments, progress checks, implement/review/fix cycles, advancing to the next specified task, integration, and validation. Root oversees intent and final product/presentation acceptance rather than coordinating every handoff or duplicating line-by-line correctness review.
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

- Astra/Sol writes the spec sheet, then hands it to the Luna manager. For each task, the manager assigns implementation, obtains independent Luna correctness review, routes findings back for fixes and rechecks, and advances to the next task only when that task is accepted. Keep this loop sequential by default; do not return routine handoff decisions to Astra/Sol.
- After all assigned tasks pass review, the manager checks integration and returns one consolidated handoff: spec coverage, validation evidence, known limitations, and any remaining decisions. Astra/Sol then evaluates the whole result for intended behavior, usefulness, visual coherence, and prose. Astra/Sol may directly touch up solid work with appropriate checks; implementation defects or a mismatch between spec and intent go back to the Luna loop with a corrected spec or bounded follow-up tasks.
- Escalate genuine ambiguity, a changed requirement, missing authority, or a blocker to Astra/Sol promptly rather than silently changing the spec. If nested delegation tools are unavailable, disclose that limitation: root may relay the manager's tool calls, but task tracking and implement/review/next-task decisions remain with Luna. Do not claim autonomous nested orchestration when the runtime cannot provide it.
- Use `gpt-5.6-luna` for routine engineering and management. Choose effort by difficulty: medium for routine coordination/mechanical work, high for complex implementation or review, xhigh for unusually difficult reasoning. Set it explicitly when spawning; do not silently switch to a larger model. Do not change runtime permissions or configuration merely to follow this document.
- Give each worker a compact brief with the canonical spec/design paths, bounded file ownership, acceptance examples, non-goals, and target tests. Reuse the implementer for fixes and the independent reviewer for targeted rechecks; avoid forwarding the full chat or spawning a new agent for every small correction.
- Review behavior against the user's goal, not only the implementation's own tests. For website changes, the Luna reviewer must exercise the real rendered page, keyboard interaction, and relevant screen widths. Report evidence, limitations, and actionable findings; root then judges whether the result is useful and visually coherent.
- Use focused tests during iteration and the full required suite at integration. Recheck changes made after review in proportion to their risk; a previous review does not cover new logic. Do not merge or deploy before root acceptance and passing CI.
- Before a long operation or usage-sensitive task, write a durable checkpoint with branch/worktree, intended behavior, completed evidence, blockers, and exact next action. Update it at milestones. Benchmark a small sample before a full-data operation; use bounded commands and surface a concrete bottleneck instead of repeating an expensive attempt.
