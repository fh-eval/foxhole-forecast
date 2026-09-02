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
