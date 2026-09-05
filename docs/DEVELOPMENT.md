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

## Managed multi-agent workflow

The root agent owns the design specification and final check only. It does not duplicate implementation or pre-review work. A Luna xhigh manager coordinates the work. The desired workflow depth is 2: one manager, exactly one implementation worker, then one fresh browser reviewer. If the manager harness cannot invoke collaboration controls, the manager prepares the complete bounded assignment and decision brief while root relays only the depth-2 spawn/send call; root does not duplicate the manager's planning, coding, or review:

- Spawn exactly one implementation worker with `model = "gpt-5.6-luna"` and `reasoning_effort = "xhigh"`; give it a fresh compact brief, bounded file ownership, and target tests. When it finishes, commission one fresh independent Luna xhigh browser reviewer read-only against the finished diff, boundary tests, and UI runtime when feasible. Send findings back to the owning implementer.
- Keep contracts and overlapping edits with the manager; implementers change only their assigned files. Browser review must exercise the real page and dataset, not only source-string assertions or a production build.
- The manager runs one final full suite after review findings are resolved, then commits, rebases, pushes, and prepares the PR. Do not merge or deploy before the root's final check.
- At every interruption checkpoint, leave `main` and production unchanged; keep the feature branch usable with either a local uncommitted diff or a clearly marked checkpoint commit.

The ignored project-local `.codex/config.toml` should preserve the existing permission and network blocks and include:

```toml
[agents]
default_subagent_model = "gpt-5.6-luna"
default_subagent_reasoning_effort = "xhigh"
max_concurrent_threads_per_session = 2
```
