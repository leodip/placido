# Placido

An AI coding workflow on [Herdr](https://herdr.dev), driving claude, codex, and
pi. The design and the step-by-step implementation plan are in
[placido-plan.md](placido-plan.md).

## Install

Placido needs Python 3.11 or newer and nothing else.

```sh
ln -s "$PWD/bin/placido" ~/.local/bin/placido
placido doctor
```

## Commands

- `placido doctor` checks herdr, the three agents, their logins, the billing
  guards, claude's auto-compact, and the Herdr integrations. It exits non-zero when any check fails.
- `placido start <issue>` starts work on an issue: it creates the branch
  `placido/<issue>` in a worktree opened as a Herdr workspace, runs the
  project's setup command there, and logs everything to a new run folder,
  including the issue's text as `issue.md`. The issue can be a GitHub URL,
  `owner/repo#439`, or `439` when the repository's `origin` is on GitHub (read
  with `gh`), or a local `issues/*.md` file named in full or by a unique prefix
  such as `01`.
- `placido spec` interviews you about the active run's issue in a new tab, one
  question at a time, then checks and seals the agreement and slice list in the
  issue's folder.
- `placido run` builds every remaining slice in blocker order, committing each.
  Run it again after an interruption and it resumes: the interrupted attempt's
  agent is stopped, its changes are stashed, and the slice starts again. When
  an agent needs your decision, you are notified and answer in its tab; the
  decision goes to the issue's `decisions.md`, which later slices and the
  reviewer follow. Every run ends with `summary.md` in its run folder.
- `placido review` reviews the finished change (Spec and Standards), has a
  fresh agent fix or answer each finding, and verifies the fixes in further
  rounds with the same reviewer, up to `final_rounds`. `placido run` runs the
  final gates and reviews automatically once every slice is committed. The
  review folds in-scope follow-ups into the change; at its end you choose, in
  the terminal, which out-of-scope follow-ups to fold in as well.
- When the review is done and the repository is on GitHub, placido pushes the
  branch and opens the pull request, ready for review: `Closes #N` for the
  issue and any issue folded in during the interview, then the run's summary.
  Follow-ups you did not fold in become issues linked from it. Placido waits
  for CI and, when it is red, has an agent fix it from the failed logs, up to
  twice. The run ends when CI is green or there is none. Without a GitHub
  remote the branch stays local.
- `placido retro` has an agent look back on a run and suggest changes to the
  project (lint rules, tests, standards, notes for agents) so the same trouble
  does not come back, written to the run's `retro.md`. It changes nothing.
  The `retro` role follows the `spec` role unless `[roles.retro]` sets it.
- `placido close [issue]` cleans up an issue once you are done with it, usually
  after merging its pull request, or to abandon it: it runs the project's
  `teardown`, removes the worktree and its Herdr workspace (the branch stays),
  and closes the run. It refuses a worktree with uncommitted changes, or a
  failing teardown, unless `--force`. Run it from outside the issue's own
  workspace, since that workspace is removed.
- `placido implement [slice]` builds the next ready slice of the sealed
  agreement, tests first, checks it (result, mutations, gates), and commits it.
  It uses the same agent `placido run` would: if the run has moved the
  implement role to a fallback agent, it continues there, unless `--agent`,
  `--model`, or `--effort` says otherwise. A failed attempt is answered as in
  `placido run`: its changes go to a git stash, and a refusal or quota moves
  the role to its next agent for every later command.
- `placido mutate <file> --replace <old> --with <new> -- <test command>` breaks
  one piece of code on purpose, runs the tests, restores the file exactly, and
  records whether the tests noticed.
- `--agent`, `--model`, and `--effort` on `spec`, `implement`, and `step`
  change the role's agent for that one command.
- `placido step <role> <task>` runs one role's agent (spec, implement, or
  review) on a task in the project's active run: a new tab in the run's
  workspace, the agent started with its configured model and effort and with
  approvals bypassed, and its result, screen, and transcript saved in a step
  folder.
- `placido config [--project DIR]` shows the resolved configuration from the
  project's `.placido/config.toml` (or the built-in defaults) and the exact
  command each role's agent runs with.
- `placido runs [-n N]` lists runs, newest first, with their outcomes.
- `placido report [run]` shows what a run spent and how it went: each step
  with its agent, duration, outcome, tokens (and how many came from the
  cache), and anything notable such as questions or results sent back; then
  the slices, the review, the time it waited for you, usage by agent (with
  OpenRouter's real cost, and codex's usage window), and what went wrong. By
  default it reports the project's current run, or its latest. `--runs` shows
  trends across the project's runs (`--all` for every project), and `--json`
  gives the same data as JSON.
- `placido status` lists every issue in progress, across projects, with its
  status, those needing you first.
- `placido log [run]` prints a run's events in readable form, the latest run by
  default. The run is a folder or a name as `placido runs` lists it.
- `placido trial` makes a trial run folder with a short event log. It is
  scaffolding until `placido start` exists.

Several issues can be in progress at once, in one project or several. Each has
its own branch, worktree, Herdr workspace, and run folder. Run placido in an
issue's worktree to act on that issue; in the main checkout it acts on the only
active run, and asks when there are several.

When an agent's turn fails, placido reads why from the agent's own session file
and responds without you:

| Failure | What placido does |
|---|---|
| Refusal (a provider's safety system flagged the work) | Moves the role to the next agent in its `[[fallback]]` chain, and the slice or fix starts again clean (the partial work goes to a git stash) |
| Quota | Waits for the reset in the same session if it comes within `limits.quota_wait` (2h by default); otherwise moves to the next agent on a different subscription |
| Transient (overloaded, 5xx, dropped connection) | Tries again in the same session after 2, 5, and 10 minutes |
| Context window overflowed, despite auto-compact | Starts once more in a fresh session, then stops |
| Logged out | Stops and tells you |

A role that moved to a fallback stays there for the rest of the run's slices, or
the rest of the review. When every agent in the chain refuses, the run stops.
Each move is logged and notified.

Runs are recorded under `~/placido/runs/<project>/<issue>/<run-id>/`; set
`PLACIDO_RUNS_DIR` to move them.

## Sandbox

`sandbox/reset.sh` builds a tiny practice project at `~/code/placido-sandbox`
(or `$PLACIDO_SANDBOX`) from `sandbox/template`, with sample issues to run
placido on. Running it again resets the project, removing the worktrees placido
made and closing their Herdr workspaces.

## Tests

```sh
python3 -m unittest discover -s tests -t .
```
