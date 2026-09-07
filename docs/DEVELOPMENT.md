# Interruption-safe development

Production work should be safe to leave unfinished for hours or days. Scheduled collection and forecasting run from `main`, so human and agent source changes are isolated on feature branches until validation is complete.

Enable the repository's local push guard once per checkout:

```bash
git config core.hooksPath .githooks
```

It rejects direct pushes of source, configuration, workflow, prompt, documentation, or website changes to `main`. Direct generated-data pushes remain available for trusted operational work.

## Start work

```bash
git switch main
git pull --rebase
git switch -c work/descriptive-topic
```

Make normal commits on the feature branch. If a session or usage allowance is about to end, either leave the local worktree intact or make a clearly labeled checkpoint commit and push only that branch:

```bash
git add <scoped-files>
git commit -m "WIP: checkpoint descriptive topic"
git push -u origin work/descriptive-topic
```

A WIP branch does not deploy and does not affect scheduled production jobs. Do not merge it or push it to `main`.

## Validate and integrate

After implementation is complete:

```bash
git fetch origin main
git rebase origin/main
ruff check --select F src tests
PYTHONPATH=src python3 -m unittest discover -s tests
node --test watchdog/test/*.test.mjs
(cd web && npm run build)
git push -u origin work/descriptive-topic
gh pr create --fill
```

Wait for all `Validate` jobs—lint, Python, watchdog, and web—to pass. Review the final diff and then merge through the pull request. Never use an admin bypass for a failed check.

Trusted collection and forecast workflows are the sole exception: they may continue committing generated append-only data directly to `main`.

## Tiered multi-agent workflow

[`AGENTS.md`](../AGENTS.md) is the canonical workflow policy. Roles are defined by function and cost tier, not by harness or model name: a frontier-tier orchestrator owns product intent, architecture, prose, presentation, spec sheets, and final acceptance; worker-tier implementers and reviewers (cost-efficient capable models) do routine code-heavy engineering, correctness review, and coordination.

The orchestrator writes a durable spec sheet with ordered tasks, acceptance examples, and non-goals, then runs the sequential loop: implementer → fresh independent reviewer → fixes/recheck → next task → integrated handoff → final acceptance. Always prefer the most cost-efficient capable model for worker roles; never substitute the frontier model for routine implementation or review. Reuse existing workers for small follow-ups, keep briefs compact, and escalate genuine blockers or spec ambiguity rather than inventing requirements.

Website review must exercise the real page and dataset. After the loop completes, the orchestrator judges the whole result against the intended experience and may directly make small editorial/visual touchups to solid work, with appropriate revalidation. Implementation defects, substantial changes, or a spec that did not capture the user's intent return to the worker loop with revised instructions. Existing design conventions may be improved; accessibility, truthful metrics, and data integrity remain constraints.

Use targeted checks while iterating and the required full suite before integration. Before long operations, record a durable checkpoint with the branch/worktree, evidence, and next action. Keep `main` and production unchanged until the release gates above pass. This policy does not itself change local model settings, permissions, or network configuration.
