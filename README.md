# Placido

AI coding workflow on [Herdr](https://herdr.dev): from GitHub issue to reviewed
pull request.

You point placido at an issue. It interviews you until every decision is made, then
coding agents build the change unattended, slice by slice and tests first, in their
own Herdr tabs. Another agent reviews the whole change, a third fixes what it finds,
and placido opens a pull request and sees CI through. You are only asked when a
decision is genuinely yours.

```
placido start 439      worktree, branch, Herdr workspace, the project's setup
placido spec           an interview, one question at a time, sealed as an agreement
placido run            slices built and committed, final checks, review, pull request
placido close 439      after you merge: teardown, worktree removed, main pulled, branch deleted
```

## How a run goes

1. **Start.** `placido start <issue>` creates the branch `placido/<issue>` in a git
   worktree, opens it as a Herdr workspace, and runs your project's setup command
   (for example, a Docker stack of its own for this issue).
2. **Interview.** `placido spec` opens an agent that reads the issue and the code,
   then asks you one question at a time until no decision is left open. It also looks
   for adjacent work (problems next to the change, open issues on the same area) and
   asks whether to fold it in. The result is an *agreement*, sealed, with the work
   cut into *slices*.
3. **Build.** `placido run` builds the slices in order, each by a fresh agent: tests
   at the agreed seams first, then the code, then a few deliberate mutations to prove
   the tests notice breakage. Placido runs your project's checks after each slice and
   commits it only when they pass. Agents never commit.
4. **Questions.** When an agent cannot follow the agreement, it asks in its own tab
   and placido notifies you. You answer there; the decision is recorded beside the
   agreement, and every later agent follows it.
5. **Review.** A different agent reviews the whole change against the agreement and
   the project's standards, with severities. A fixer answers each finding, and the
   reviewer, resumed with its context, verifies the fixes, for up to three rounds.
   Work that belongs to the issue is folded in; for out-of-scope follow-ups, you pick
   in the terminal which to fold in anyway.
6. **Pull request.** Placido pushes the branch once, opens the pull request ready for
   review (`Closes #N`, then a summary of everything the run did and decided), and
   waits for CI. A red run's failed logs go to a fixer agent, up to twice. The body
   says what the change does, its decisions, and what the review found. The
   follow-ups, those the interview drafted and those from the review you did not fold
   in (drafted by an agent with evidence and a duplicate search), go into one comment
   on the pull request, each with the `gh issue create` command that files it;
   placido files none. Other open issues the change affects get a short note.
7. **Close.** After you merge, `placido close` runs your project's teardown,
   removes the worktree and its workspace, brings your base branch up to date, and
   deletes the issue's branch once its pull request is merged. An unmerged branch
   stays.

Every step is recorded: prompts, results, agents' transcripts, check output, and an
event log, under `~/placido/runs/`. A run that stops, or that you stop with Ctrl+C,
resumes where it left off when you run the same command again.

> **Read this first.** Placido runs its agents with every approval bypassed (claude
> with `--dangerously-skip-permissions`, codex with
> `--dangerously-bypass-approvals-and-sandbox`), so an unattended run never stalls on
> a permission prompt. The agents can run any command your user can. Use placido only
> on code and machines where you accept that, such as a dedicated user, VM, or
> container. Nothing is pushed until the review is done.

## Requirements

- **Linux or macOS** (on Windows, WSL works), with `git` and Python 3.11 or newer.
  Placido itself has no Python dependencies.
- **[Herdr](https://herdr.dev) 0.9.3 or newer**, with placido run from inside a Herdr
  pane.
- **The agent CLIs**, logged in:
  - [Claude Code](https://claude.com/claude-code) (`claude`), on a Claude subscription;
  - [Codex CLI](https://github.com/openai/codex) (`codex`), on a ChatGPT subscription;
  - [pi](https://github.com/badlogic/pi-mono), with an OpenRouter key, used only as a
    last fallback.

  `placido doctor` fails while `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
  `OPENAI_API_KEY`, or `CODEX_API_KEY` is set, since any of them can switch an agent
  to paid API billing.
- **The [GitHub CLI](https://cli.github.com)** (`gh`), logged in, to read issues and
  open pull requests. Without a GitHub remote, placido works locally and the branch
  stays local.

## Install

```sh
git clone https://github.com/leodip/placido.git ~/code/placido
ln -s ~/code/placido/bin/placido ~/.local/bin/placido   # any folder on your PATH

herdr integration install claude
herdr integration install codex
herdr integration install pi

placido doctor
```

`placido doctor` checks everything placido needs and says how to fix what is
missing. Two one-time steps it may ask for:

- Accept Claude Code's bypass-mode warning once: run `claude
  --dangerously-skip-permissions` in any folder and choose "Yes, I accept".
- Keep Claude Code's auto-compact on (it is on unless you turned it off), since long
  steps rely on it.

## Set up a project

A project needs one file, `.placido/config.toml`, committed with it. Every key is
optional; without the file, placido uses its defaults and runs no checks. A typical
one:

```toml
[project]
base     = "main"                    # the branch issues start from
setup    = "scripts/dev-stack up"    # runs in each new worktree; optional
teardown = "scripts/dev-stack down"  # runs on `placido close`; optional

# The commands agents may run, and that placido's checks pick from. `about` tells
# agents what a command covers, how long it takes, and how to narrow it.
[commands.unit]
run   = "go test ./..."
about = "Unit tests, about 20 seconds. Narrow with -run '<regex>' and a package path."

[commands.integration]
run   = "go test -tags integration ./tests/..."
about = "End-to-end tests against the dev database, about 2 minutes."

[commands.lint]
run   = "golangci-lint run ./..."
about = "The linters CI runs."

[gates]
slice = ["unit"]                     # after each slice, before its commit
final = ["lint", "integration"]      # once every slice is in, before the review
```

Setup, teardown, the gates, and every agent's tab get these variables, so your own
scripts can find the run's resources:

| Variable | Example |
|---|---|
| `PLACIDO_ISSUE` | `439-rate-limiter-counts-half-moves` |
| `PLACIDO_ISSUE_NUMBER` | `439` (empty for a local issue file) |
| `PLACIDO_NAME` | `goiabada-439`, unique per issue: a good Docker Compose project name |
| `PLACIDO_WORKTREE` | the issue's worktree |
| `PLACIDO_RUN_DIR` | the run's record folder |

`placido config` prints the resolved configuration and the exact command each agent
runs with.

### What agents read

Placido's skills are generic; project knowledge comes from the files agents
conventionally read, so a project set up for other agent tools works too:

- `AGENTS.md` or `CLAUDE.md`, `CONTRIBUTING.md`, `CODING_STANDARDS.md`,
  `GLOSSARY.md`, and ADRs in `docs/adr/`: read by every role.
- `.placido/notes/<role>.md`, optional: what only placido's agents need, per role
  (`spec`, `implement`, `review`, `fix`, `retro`), such as how to run the database
  tests in this project.

### Agents and fallbacks

Each role has an agent, model, and effort, and a fallback chain:

| Role | Default |
|---|---|
| `spec` (the interview) | claude, Opus, xhigh effort |
| `implement` | claude, Opus, high |
| `review` | codex, gpt-6.1-sol, max, fast tier |
| `fix` | as `implement` |
| `retro` | as `spec` |
| fallback chain | codex gpt-daybreak-blue-latest, then pi with DeepSeek through OpenRouter |

Change any of them per project:

```toml
[roles.implement]
agent  = "codex"
model  = "gpt-6.1-sol"
effort = "high"            # low, medium, high, xhigh, or max

[[fallback]]               # tried in order when an agent refuses or runs out of quota
agent = "pi"
model = "deepseek/deepseek-v4.1-flash"
```

`--agent`, `--model`, and `--effort` change a role's agent for one command: on
`spec`, `implement` and `run` (the implementer), `review` (the reviewer), `retro`,
and `step`.

### Other settings

```toml
[implement]
test_first = true      # tests first, seen failing; false allows tests after the code
mutations  = 2         # deliberate breakages each slice's tests must catch; 0 turns it off

[review]
final_rounds = 3       # review rounds; each one after the first must be earned

[limits]
stage_attempts = 3     # failed attempts at a slice before the run stops
quota_wait     = "2h"  # wait this long for a quota reset before falling back
ci_wait        = "1h"  # wait this long for CI on the pull request
```

## Use it

Work from a Herdr pane in your project's checkout.

```sh
placido doctor
placido start 439          # or a URL, owner/repo#439, or a local issues/*.md file
placido spec               # answer the interview in the tab it opens
placido run                # unattended from here; Ctrl+C stops it cleanly
```

While a run works:

- **`placido status`** lists every issue in progress, across projects, those that
  need you first.
- **A question** comes as a Herdr notification: go to the agent's tab and answer
  there.
- **At the end of the review**, the terminal running `placido run` may ask which
  follow-ups to fold in: `all`, `none`, or numbers such as `1,3`.
- **`placido run` again** resumes after a stop: an interruption, a failed check, red
  CI, or a logout you have since fixed.
- **`placido start` again** after a failed setup retries it in the same worktree,
  brought up to the base, once you have fixed the setup.

Afterwards:

- **`placido report`** shows what the run spent and how it went: each step's agent,
  time, outcome, and tokens; `--runs` shows trends across runs.
- **`placido retro`** has an agent read the run and suggest changes to the project,
  such as a lint rule or a note for agents, so the same trouble does not come back.
  It changes nothing.
- **`placido close`**, once you have merged or given up: teardown, worktree, and
  workspace go. Your main checkout is then pulled (a fast-forward, only when it is on
  the base branch with no changes), and the issue's branch is deleted if its pull
  request was merged, or it is merged into the base; otherwise it stays. Run it from
  outside the issue's own workspace.

Several issues can be in flight at once, in one project or several: each has its own
branch, worktree, workspace, and run. Inside an issue's worktree, placido acts on
that issue; in the main checkout it acts on the only one in progress, or asks.

### When an agent's turn fails

Placido reads why from the agent's own session file and responds by itself:

| Failure | What placido does |
|---|---|
| Refusal by a provider's safety system | Moves the role to the next agent in its fallback chain; the slice or fix starts again clean |
| Quota | Waits for the reset in the same session if it comes within `quota_wait`, otherwise falls back to an agent on another subscription |
| Overloaded, 5xx, dropped connection | Tries again in the same session after 2, 5, and 10 minutes |
| Context window full, despite auto-compact | Starts once more in a fresh session, then stops |
| Logged out | Stops and tells you |

## Commands

| Command | What it does |
|---|---|
| `placido doctor` | Checks Herdr, the agents, their logins, and billing |
| `placido start <issue>` | Starts an issue: branch, worktree, workspace, setup |
| `placido spec` | The interview that seals the agreement |
| `placido run` | Builds the slices, runs the final checks, reviews, opens the pull request; resumes |
| `placido implement [slice]` | Builds one slice by hand |
| `placido review` | Reviews the change and delivers it, as `run` does at its end |
| `placido close [issue]` | Teardown, worktree, and workspace removed, base pulled, merged branch deleted; `--force` discards changes |
| `placido status` | Every issue in progress, those needing you first |
| `placido report [run]` | A run's steps, time, and tokens; `--runs` for trends, `--json` for data |
| `placido retro` | Suggestions for the project from a finished run |
| `placido runs` / `placido log [run]` | The runs, and one run's events |
| `placido config` | The resolved configuration and agent commands |
| `placido mutate` | Used by agents: break one piece of code, run the tests, restore it |
| `placido step <role> <task>` | Runs one role's agent on any task, for experiments |

Runs are kept under `~/placido/runs/<project>/<issue>/<run>/`; set
`PLACIDO_RUNS_DIR` to move them.

## Try it on the sandbox

`sandbox/reset.sh` builds a tiny practice project at `~/code/placido-sandbox` (or
`$PLACIDO_SANDBOX`): a Python greeting script with four small issues in `issues/`.
It has no GitHub remote, so runs there stop before the pull request.

```sh
~/code/placido/sandbox/reset.sh
cd ~/code/placido-sandbox
placido start 01
placido spec
placido run
```

Running `reset.sh` again closes the sandbox's issues and rebuilds it.

## Development

```sh
python3 -m unittest discover -s tests -t .
```

[placido-plan.md](placido-plan.md) holds the design, every decision with its reason,
and the log of how placido was built.

## License

MIT
