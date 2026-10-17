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

It also reviews pull requests other people send: `placido pr 602` has a Claude and a
Codex reviewer each review the pull request on its own, cross-check each other, and a
judge settle both into one review, with a drafted comment for the contributor. What
the judge leaves to you, you decide in a conversation, question by question. Nothing
is posted, pushed, or merged until you pick it (see
[Review a contributor's pull request](#review-a-contributors-pull-request)).

## How a run goes

1. **Start.** `placido start <issue>` creates the branch `placido/<issue>` in a git
   worktree and opens it as a Herdr workspace. Herdr switches to that workspace, where
   placido types `placido start <issue> && placido spec` into its pane, in the
   worktree: your project's setup command runs there (for example, a Docker stack of
   its own for this issue), then the interview. The terminal you typed it in is free
   at once, and Herdr's sidebar shows each issue once, under your repository.
   `--here` keeps it all in your terminal, as does starting outside Herdr.
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
   Work that belongs to the issue is folded in; out-of-scope work becomes a follow-up,
   without stopping to ask.
6. **Pull request.** Placido runs your project's last gates, if it has any, on the
   reviewed change, then pushes the branch once, opens the pull request ready for
   review (`Closes #N`, then a summary of everything the run did and decided), and
   waits for CI. A red run's failed logs go to a fixer agent, up to twice. The body
   says what the change does, its decisions, and what the review found. The
   follow-ups, those the interview drafted and those the review left out (drafted by an
   agent with evidence and a duplicate search), go into one comment
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
> container. Nothing is pushed until the review is done. The agents keep no memory
> between sessions: placido turns off Claude's auto-memory and Codex's memories for
> every agent it starts, so what one agent read, such as a stranger's pull request,
> never reaches the next, nor your own sessions in the project.

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
last  = []                           # once the review is done, before the push
```

The last gates are for a check too slow or costly to run more than once. A failing
gate goes to a fixer, as with the final gates, and placido runs the gates again on
its fix before committing it, and not after; when the fix cannot pass, the pull
request opens anyway, with the failure at the top of its description.

Setup, teardown, the gates, and every agent's tab get these variables, so your own
scripts can find the run's resources:

| Variable | Example |
|---|---|
| `PLACIDO_ISSUE` | `439-rate-limiter-counts-half-moves` |
| `PLACIDO_ISSUE_NUMBER` | `439` (empty for a local issue file) |
| `PLACIDO_NAME` | `goiabada-439`, unique per issue: a good Docker Compose project name |
| `PLACIDO_WORKTREE` | the issue's worktree |
| `PLACIDO_RUN_DIR` | the run's record folder |
| `PLACIDO_REPO` | your main checkout |

Point setup, teardown, and the gates at your main checkout's scripts with
`$PLACIDO_REPO`, such as `setup = "$PLACIDO_REPO/.placido/stack.sh up"`, and have the
scripts act on `$PLACIDO_WORKTREE`. A branch then runs them as they are today, not as
they were when it was made, so a pull request branched before a fix to them still
gets the fix.

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
| `scan` (`placido pr`, confined) | claude, Opus, high |
| `judge` (`placido pr`) | claude, Opus, high |
| `verify` (`placido pr`, checks the fixes) | as `review` |
| `decide` (`placido pr`, takes you through the review's open questions) | as `judge` |
| fallback chain | claude Opus at max, then pi with DeepSeek through OpenRouter; an entry with the role's own model is skipped after a refusal |

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

[pr]                   # placido pr: reviewing other people's pull requests
reviewers = [          # each reviews on its own, in this order; each may name a fallback list
  { agent = "claude", model = "opus", effort = "xhigh" },
  { agent = "codex", model = "gpt-6.1-sol", effort = "max", tier = "fast" },
]
gates = []             # commands run once before the reviews; none named: the slice and final gates
host_paths = [".placido", ".claude", ".codex", ".mcp.json"]  # changes wait for your approval
contributor_share = 0.5  # fixes this big, against the pull request, go back to its author
parallel   = true      # reviewers at once, each in a worktree and stack of its own
fix_rounds = 2         # times a failed check of the fixes sends them back to the fixer
```

### Your own settings

Some settings are yours rather than a project's: they live in
`~/.config/placido/config.toml` (or `$PLACIDO_USER_CONFIG`), never in a repository.

```toml
[spec]
then_run = true   # once the agreement is sealed, go straight on with `placido run`
```

With `then_run`, `placido spec` continues into the run in the same terminal; every
slice still gets a fresh agent session.

### Email alerts (your own settings)

Placido can email you whenever a run needs you or ends: an agent asks a question, a
dialog waits (the interview's own questions only get Herdr's notification, since you
started it), an agent falls back or is logged out, a check or CI stays red, a run
stops, the pull request is ready. Each email says what happened and what to do, links
the issue and the pull request, and shows how much of your Claude and Codex quota is
used. Email settings are yours, not the project's, so they live in
`~/.config/placido/config.toml` (or `$PLACIDO_USER_CONFIG`), never in a repository:

```toml
[email]
enabled      = true
to           = "you@example.com"
from         = "placido <placido@your-domain>"   # a sender your Resend account may use
api_key_file = "~/secrets/resend"                # a file holding only the Resend API key
```

Placido reads the key when it sends and never copies or logs it. A failed email is
logged in the run and never stops it. `placido email-test` sends a test email, and
`placido doctor` checks the settings.

The quota comes from Codex's own session files and, for Claude, from a file your
Claude Code status line writes, since Claude Code keeps its quota nowhere else. Add
this to the end of your status line script:

```sh
printf '%s' "$input" | python3 -c '
import json, os, sys, time
d = json.load(sys.stdin); r = d.get("rate_limits")
if r:
    p = os.path.expanduser("~/placido/claude-quota.json"); os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump({"saved_at": int(time.time()), "rate_limits": r}, open(p + ".tmp", "w")); os.replace(p + ".tmp", p)
' 2>/dev/null
```

where `$input` holds the JSON Claude Code passed the script on stdin.

## Use it

Work from a Herdr pane in your project's checkout.

```sh
placido doctor
placido start 439          # or a URL, owner/repo#439, or a local issues/*.md file
# placido spec starts by itself in the issue's workspace: answer there
placido run                # unattended from here; Ctrl+C stops it cleanly
```

While a run works:

- **`placido status`** lists every issue in progress, across projects, those that
  need you first.
- **A question** comes as a Herdr notification: go to the agent's tab and answer
  there.
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
| A model this login cannot use (such as one an account lost) | Moves to the next agent, as for a refusal |
| Quota | Waits for the reset in the same session if it comes within `quota_wait`, otherwise falls back to an agent on another subscription |
| Overloaded, 5xx, dropped connection | Tries again in the same session after 2, 5, and 10 minutes |
| Context window full, despite auto-compact | Starts once more in a fresh session, then stops |
| Logged out | Stops and tells you |

Codex runs one shared daemon for all its sessions, from the folder of whichever
session started it; once that folder is deleted, every new Codex session fails. So
placido starts the daemon from your home folder before it launches Codex, and
`placido close` restarts it from there when it runs from the worktree being removed.
Restarting would cut off any Codex session running, so then placido only says how to
do it later; `placido doctor` reports a daemon whose folder is gone.

## Review a contributor's pull request

`placido pr 602` (or `#602`, or its URL), from a Herdr pane in your project's checkout,
reviews a pull request someone else sent, in a worktree and workspace of its own
(`placido/pr-602`, with `PLACIDO_NAME` such as `goiabada-pr-602` for your setup's
stack), unattended until it has something to show you. As with `placido start`, the
review goes on in that workspace, where Herdr switches you: placido types
`placido pr 602` into its pane, and your terminal is free for the next one (`--here`
keeps it in yours).


1. **A safety scan, before anything of it runs.** An agent reads the change and the
   pull request's text, confined: it can read the change and write its own notes, and
   nothing else (claude in `dontAsk` mode with no shell, or codex in its sandbox with no
   network). It looks for code that would reach beyond the project when the setup or
   the tests run, hidden content, new dependencies, and text aimed at agents. Any
   concern stops the review until you say to go on. A change to a file agents load by
   themselves, such as `AGENTS.md`, that reads as ordinary project documentation is a
   note instead: shown, and handed to the reviewers, without stopping.
2. **Host paths.** The paths in `[pr] host_paths` run on your machine: your scripts and
   the agents' project settings, whose hooks and MCP servers start with an agent in
   the worktree. A pull request that changes them waits for your approval, and they
   are checked again before every command placido runs from the worktree, since the
   pull request's own tests can write to it. After every command and every agent the
   worktree must be clean: what changed is kept as evidence and put back, and a change
   under a host path stops the review.
3. **Setup and gates.** Your setup brings up a stack for each reviewer, while the scan
   reads when the pull request changes no host path, and after your approval when it
   does. Then the `[pr] gates`, once, for every reviewer.
4. **Reviews.** Every reviewer at once, each in a lane of its own: the pull request's
   worktree for the first, a detached worktree in the run's folder for each other, each
   with its own stack (`PLACIDO_NAME` such as `goiabada-pr-602-b`), so their test runs
   and mutations never meet. Each reviews on its own: does it solve its issue, gaps,
   bugs, security, the standards it touches (read from the source), test coverage, and
   whether the tests are real: they fail without the fix, and mutations of the fix
   (`placido mutate`) make them fail. `parallel = false` runs them one after the other
   in one worktree, with one stack.
5. **Cross-check.** Each reviewer, resumed, confirms or refutes the others' findings.
   Then the other lanes' stacks go down, since only the pull request's own worktree
   works after this; a later round brings them up again.
6. **Judge.** One review, told which findings the cross-checks settled so it verifies
   only the rest: the findings kept and who found them, what was dropped and why, a
   verdict, problems that were already there (for you only), and drafts of the comment
   and the squash message. Fixes estimated at `contributor_share` of the pull request
   or more go back to the contributor; smaller ones you make here. The agents write
   JSON, and placido renders it (`review.md`, `final.md`).

7. **Your decisions.** What the judge left to you, such as a design choice the
   contributor made or scope the issue did not settle, you decide before anything
   else, every question: the `decide` role takes you through them in its own tab, one
   at a time, with the context, the options and what each leads to, and the judge's
   recommendation. Answer as you see fit. Placido applies your answers to the final
   review (a finding fixed another way, dropped, or added, or a different verdict),
   records them in `decisions.md`, which every later agent reads, and takes the
   comment and squash message the role brought in line. Stop halfway, and the next
   `placido pr` asks again.

Then placido prints the review and a menu. Nothing happens that you do not pick, and
pushing, merging, and closing ask again: edit the comment or the squash message (in
`$VISUAL`, `$EDITOR`, or nano), fix the findings here on the contributor's branch, run
the project's last gates on the branch as it is here (the checks an issue run keeps
for after the review, such as a browser suite against a real deployment; a failure
stops the push or merge picked with them), push them (never forced, and only when
the contributor allows maintainers to), post the comment (shown in full first, as it
will go out), approve CI runs GitHub holds
for first-time contributors, squash-merge pinned to the commit the review covered, or
close. A fix is the `fix` role's, committed locally once the gates pass, then checked
by the `verify` role, another vendor by default: each finding's fix with a test that
fails without it, and the fixes for new problems. What the check leaves open goes back
to the fixer, up to `fix_rounds` times: the same session, resumed, which knows what it
did, while each check is a fresh one. The push says whether the fixes passed.
When the base branch has moved on, the fix first merges it into the branch, as a commit
of its own, so the fixes are made and gated on what will land; the `fix` role resolves
any conflict, and only the conflicts. A merge, not a rebase, since the push is never
forced. A pull request that changes host paths, or whose fix did, is fixed on its own
base instead. A fix that needs a host path changed, such as a browser test under a
folder the last gates run, shows you that change and runs none of it until you
approve it; refused, the fixer undoes it and disputes the findings that needed it.
To ask about the work, the menu opens the round's fixer, or its judge when nothing was
fixed, in a new tab of the workspace: a copy of its session, so placido's own stays as
it was, started read-only, since a change made there would skip the check a fix gets.
With neither session at hand, it opens a new agent of the `fix` role's instead, pointed
at the review's files.

`placido pr 602` again shows the menu again, or, once the contributor has pushed, starts
a new round whose reviewers check each earlier finding; typed outside its workspace, it
goes on there, or only switches you there while the review is still going. `placido close pr-602` removes
every lane's worktree and stack, putting the host paths back first (only your own
uncommitted changes stop it, not what that puts back), and deletes the
branch once the pull request is merged or closed and nothing made here is left
unpushed. Project
notes for these agents go in `.placido/notes/pr.md` (all of them), `scan.md`,
`review.md`, and `fix.md`, read from your main checkout, never from the pull request.

## Commands

| Command | What it does |
|---|---|
| `placido doctor` | Checks Herdr, the agents, their logins, billing, and email |
| `placido email-test` | Sends a test email with the current quota |
| `placido start <issue>` | Starts an issue: branch, worktree, workspace, setup |
| `placido spec` | The interview that seals the agreement |
| `placido run` | Builds the slices, runs the final checks, reviews, opens the pull request; resumes |
| `placido implement [slice]` | Builds one slice by hand |
| `placido review` | Reviews the change and delivers it, as `run` does at its end |
| `placido pr <number>` | Reviews a contributor's pull request with two reviewers and a judge, then acts as you pick; resumes |
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
placido start 01   # placido spec starts in the issue's workspace
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
