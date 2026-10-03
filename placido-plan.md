# Placido

## Context

This repository contains the Goiabada GBD AI workflow, including the specification
skill, execution and review skills, and a Python driver that coordinates work from
a GitHub issue to a pull request. Its core ideas include agreeing on intent before
execution, independent review, evidence for completion, durable handoffs, and
bounded autonomous work with explicit handling of human decisions.

We are exploring Herdr as the foundation for a new workflow. Herdr provides
terminal workspaces, panes, agent orchestration, and a CLI that agents and scripts
can use. Its documentation is indexed at
[herdr.dev/llms.txt](https://herdr.dev/llms.txt); the initial general review used the
stable 0.9.3 documentation.

GBD provides ideas and lessons for this work. The new workflow has room to adapt,
simplify, and improve its architecture, roles, session lifetimes, review cadence,
and coordination. The work is currently exploratory, with design and implementation
details open for later discussion.

## Goal

Design and build a new AI workflow using agent orchestration through Herdr and
reusable skills, with scripts where needed. Carry forward the strongest ideas from
GBD while taking advantage of Herdr's capabilities and allowing a different
implementation.

The workflow should be generic and easy to apply to any project. It should
accommodate each project's build, tests, development environment, and conventions
while keeping the core process reusable.

Aim for a flexible, reliable, understandable, and maintainable workflow that makes
agent collaboration visible, grounds progress in evidence, preserves context
across sessions, and brings meaningful decisions back to the user.

## GBD workflow in concepts

GBD moves a task through an attended phase, where the user and agent agree on the
work, and an unattended phase, where agents carry it out. The agreement connects
these phases and gives execution a durable reference for scope and completion.

### 1. Prepare an isolated task environment

The user brings the project's main branch up to date and creates a dedicated
worktree for the issue. In GBD, `gbd-worktree add <issue>` also prepares the task's
development and test environment.

The concept is to begin from a known baseline and give the task its own working
area, so its changes and test activity can proceed independently of other work.

### 2. Discover and agree on the work with the user

The user opens an agent in that worktree and starts the specification workflow
for an issue, currently `/gbd-spec <issue>` in Claude Code. The agent reads the
issue and discussion, investigates the existing code, and grounds the proposed
work in what the project actually does.

An interview resolves questions about desired behavior, scope, constraints, and
tradeoffs. The answers become an agreement describing the goal, decisions,
design, affected areas, and verification expectations.

The agreement also identifies **seams**: observable boundaries where tests can
demonstrate the required behavior, such as a public function, API endpoint, or
storage boundary. In current GBD, the user reviews both the design and these seams
and asks for changes until they reflect the intended work.

### 3. Approve, seal, and hand off

In current GBD, the agent seals the agreement after the user confirms the design
and seams. The seal marks the agreed scope and decisions as ready for unattended
execution. The intended Herdr behavior is recorded below under agreed changes.

The user exits the interactive agent session. The handoff is the recorded
agreement; later sessions can act on it without relying on the interview's chat
history. New decisions that fall outside the agreement can still bring the work
back to the user.

### 4. Plan execution and review the plan

The user explicitly starts unattended execution, currently with
`gbd-drive <issue>`. The run turns the agreement into an ordered implementation
plan with stages, dependencies, and verification work. Each stage should leave a
usable, compiling checkpoint.

An independent reviewer checks whether the plan serves the agreed goal and covers
the necessary changes and tests. Findings are resolved before implementation
proceeds. GBD opens a draft pull request early, providing a place for progress and
questions throughout the run.

### 5. Implement and verify stage by stage

The run implements each stage, adds or updates tests and documentation, performs
the applicable checks, records the result, and commits and pushes the work.
Later stages are detailed as execution reaches them and the earlier code exists.

Progress depends on recorded work and verification. The driver coordinates
sessions, checks whether stages may advance, and bounds the run. It handles waits
and interruptions outside the individual work sessions. Individual stage review
is available, while the usual flow described here reviews the completed change
as a whole.

### 6. Review the completed change and close the run

An independent reviewer examines the whole implementation against the agreement,
including behavior and interactions across stages. Findings are resolved and
recorded, with further review where needed within the run's limits.

The closing pass consolidates what changed, verification results, review outcomes,
deviations, and follow-up work. GBD marks the pull request ready for human review
and checks the final CI result. The successful deliverable is a reviewed and
verified pull request; closing the original issue remains the user's decision.

### Feedback and durable memory throughout the workflow

GitHub carries the human-visible account of the work through the issue and pull
request. In the current implementation, ongoing stage progress updates the draft
pull request, unresolved decisions become questions for the user, and the closing
account explains the result and review findings.

Detailed working records live under `C:\Code\gbd`, organized by issue and run.
They include the agreement, plan, stage logs, review exchanges, execution state,
and run logs. They live outside the disposable worktree, so later sessions can
resume the work and its history remains available after the checkout is removed.

The reusable concept is to keep both a concise account for the user and enough
durable evidence for agents to continue and explain their work. Commands, tracker
integration, storage location, and project tooling are choices of the current GBD
implementation; the Herdr workflow can adapt them to serve different projects.

## Agreed changes

### Produce and seal the agreement automatically after the interview

The interview is where the user answers questions and settles decisions. Once
the interview is complete and all questions are answered, the agent produces the
agreement and seams from those answers and seals the agreement automatically.
It asks for no separate approval of either the agreement or the seams.

If producing these artifacts reveals an unresolved question, the agent returns
to the interview to resolve it. Once resolved, it completes and seals the artifacts
without adding an approval step.

## Design

The decisions in this section were confirmed with the user in an interview on
2026-10-01, one question at a time. The session log records each answer.

### Start small and grow from real runs

Placido starts as the smallest workflow that runs end to end and grows only
when a real run shows a need. GBD grew to about 24,000 lines (11,200 in the
driver, 5,600 in tests, 4,500 in skills) over 254 commits, much of it from
visibility and robustness fixes added one incident at a time. Placido avoids
that by following these rules:

- **Herdr owns** agent launch, waiting, state, visibility, worktrees, and
  notifications. Placido never rebuilds them.
- **Add code only after a failure seen in a real run**, never for a failure
  that has only been imagined.
- **Project specifics live in configuration and project files**, not in placido
  code.
- **Scripts do mechanical work; AI does judgment.** Sequencing, git, running
  checks, and file checks are script work. Interviewing, breaking work into slices,
  implementing, and reviewing are agent work.
- **Keep AI turns few and prompts small.** Run time comes from AI turns, not
  from lines of script. Prompts pass pointers to files rather than copies of
  their content.
- **Fix the environment, not the driver.** Lessons from runs become project
  checks, coding standards, or documentation where possible.
- **Automate the tests for everything testable.** Every piece of supporting
  code gets automated tests, using Python's standard `unittest`. Pure logic,
  such as configuration resolution, outcome classification, the event log, and
  run state, is tested directly. Herdr and git interactions are tested against
  a named Herdr test session and the sandbox repository. Each step's validation
  is automated wherever possible; manual checks are kept for what only a person
  can judge, such as how the interview feels.
- **GBD is a source of lessons, not code to port.** Before adding a feature,
  check which GBD problem it solved and whether that problem still exists with
  Herdr.

The target size for the core coordinator is a few hundred lines at first and
under about 1,500 lines when complete.

### Roles of Herdr, the placido script, and the agents

- **Herdr** is a terminal multiplexer for agents. It provides workspaces, tabs,
  and panes; recognizes agents and their `working`, `idle`, `done`, `blocked`,
  and `unknown` states; and exposes a CLI that scripts and agents can call.
  Herdr has no built-in orchestrator.
- **The placido script** (Python, standard library only) conducts each run. It
  decides the next step, asks Herdr to start and prompt agents, waits, checks
  that the expected result files exist, runs the project's checks, commits,
  enforces limits, and resumes after interruption. A script is predictable, costs
  nothing to run, and survives restarts, which an orchestrating agent would not.
  The script is built as small composable commands (`placido next`, `step`,
  `check`, `commit`, `status`), and `placido run` is a short loop over them. An
  orchestrator agent that the user can talk to can be added later by having it
  call the same commands, without a rewrite.
- **Agents** do the judgment work in visible panes: the interview,
  implementation, and review. There is no separate planning role: at the end of
  the interview, the specification session writes the agreement together with a
  coarse list of slices (title, what each delivers, and what blocks it) and
  seals both. Each implementer details its own slice when it starts, against the
  code as it exists then. A plan review can be added later as an option if real
  runs show breakdowns need checking. Each role step uses a fresh session; continuity
  comes from the run folder rather than chat history. The exception is the
  reviewer, whose session is resumed across the rounds of one review, as GBD
  measured roughly 94% cache hits for resumed review rounds.
- **Results pass through files.** Agents write their output to the run folder.
  Herdr's `idle` state shows that an agent has stopped, not that it finished the
  work; the result file is the evidence of completion.

### Git during a run

- A run works on the worktree's own branch, created by `placido start` (for
  example `placido/42-token-expiry`), never on `main`.
- The placido script makes one commit per slice, and one for each review fix
  step, only after the project's check passes. Agents change files and write
  their result; they do not commit, so a failing check can never be committed.
- Commit messages come from the slice's result file and reference the run.
- Nothing is pushed until the GitHub step exists. This is a matter of build
  order: the sandbox has no remote, pushing belongs with the draft pull request
  and progress updates designed in step 13, and an early version cannot publish
  half-working branches by mistake. The expected behavior from step 13 is GBD's:
  push after each slice's commit and open a draft pull request early. Squashing
  for the final pull request remains the user's choice at merge time.

### Skills usable by hand

Every placido skill also works in an ordinary agent session, without the
script. The script only sequences the skills. Matt Pocock's skills repository
warns that process-owning frameworks take away control and make process bugs
hard to resolve. Hand-usable skills keep placido debuggable: when a run goes
wrong, the failing step can be rerun manually and observed.

### Packaging as a Herdr plugin

Placido is a CLI first. It is later packaged as a Herdr plugin through a
`herdr-plugin.toml` manifest, because the whole Herdr CLI is the plugin API
and plugins can be written in any language. The plugin can add:

- actions bound to keys or menus, such as "start placido run" in the current
  workspace;
- a link handler that starts a specification when the user ctrl+clicks a GitHub
  issue URL;
- an event hook on `worktree.created` that runs the project's setup command,
  since `herdr worktree create` has no setup hook of its own;
- a popup pane with the run status;
- installation with `herdr plugin install leodip/ai/placido`, or
  `herdr plugin link` during development.

Plugins have no storage API, and Herdr's event history is not durable. Placido
keeps its own state in the run folder.

### Configuration in one file

A project's only placido-specific file is `.placido/config.toml`, committed
with the project. Every key is optional and has a built-in default.

```toml
[project]
base     = "main"
setup    = "scripts/stack up"     # runs when a worktree is created
teardown = "scripts/stack down"   # runs when a run is cleaned up (step 10)

# The project's command menu: offered to agents in their prompts, usable as gates.
[commands.unit]
run   = "scripts/stack exec go test -short ./..."
about = "Go unit tests, about 1 minute. Narrow with packages: scripts/stack exec go test -short ./internal/oauth/..."

[commands.data]
run   = "scripts/stack exec make test-data"
about = "Data-layer tests against all four database engines, about 10 minutes."

[commands.lint]
run = "scripts/stack exec make lint"

[gates]
slice = ["lint", "unit"]          # after each slice, before its commit
final = ["unit", "data"]          # before the final review

[roles.spec]
agent  = "claude"
model  = "opus"            # Opus 5.5, already a 1M-token context window
effort = "xhigh"

[roles.implement]
agent  = "claude"
model  = "opus"            # Opus 5.5
effort = "high"

[roles.review]
agent  = "codex"
model  = "gpt-6.1-sol"
effort = "max"
tier   = "fast"            # codex only: "fast" (2x speed, more usage) or "default"

# Tried in order when a step is refused.
[[fallback]]
agent  = "codex"
model  = "gpt-daybreak-blue-latest"   # OpenAI's cyber-permissive model; no fast tier
effort = "max"

[[fallback]]
agent  = "pi"
model  = "deepseek/deepseek-v4.1-flash"   # any OpenRouter model id
effort = "xhigh"

[implement]
test_first = true    # write the seam tests first and see them fail; false allows tests after the code

[review]
plan_rounds  = 0     # 0 turns the plan review off
final_rounds = 3     # most rounds for the whole change; rounds after the first must be earned

[limits]
stage_attempts = 3
quota_wait     = "2h"  # wait this long for a quota reset before falling back
```

Effort uses one scale for every role (`low`, `medium`, `high`, `xhigh`, `max`),
and placido translates it to each agent's own flag. `tier` sets codex's
service tier (`-c service_tier=…`) and is passed explicitly, so the global
`~/.codex/config.toml` never decides it; claude and pi ignore it. A role can
override the fallback chain.

Resolution rules, as built in step 2:

- `setup` and `teardown` default to none, and there are no commands or gates
  by default; the example shows Goiabada-like values.
- `[commands.<name>]` defines the project's command menu. Names use lowercase
  letters, digits, and dashes; `run` is required and `about` tells agents what
  the command covers, how long it takes, and how to narrow it. The menu is
  general: tests, lint, formatting, mock generation, migrations, or anything
  else agents may need. Placido puts it in every implement and review prompt
  as "Commands for this project".
- `[gates]` names the commands placido runs itself: `slice` after each slice,
  before its commit, and `final` before the final review. A gate naming an
  undefined command is an error. At step 5 each slice may also name extra
  commands that prove it, such as `data` for a slice touching the data layer.
- `setup`, `teardown`, the gates, and every agent's tab get the run's
  variables: `PLACIDO_ISSUE` (`439-rate-limiter-counting-half-moves`),
  `PLACIDO_ISSUE_NUMBER` (`439`, empty for local issue files), `PLACIDO_NAME`
  (`goiabada-439`, unique per issue and usable as a compose project name),
  `PLACIDO_WORKTREE`, and `PLACIDO_RUN_DIR`. A project's own scripts use them
  to find the run's resources, such as its Docker stack.
- Setting a role's `agent` without a `model` picks that agent's default model
  (`opus`, `gpt-6.1-sol`, or `deepseek/deepseek-v4.1-flash`).
- `tier` defaults to `default`, which codex sends as a real tier, except for
  the reviewer, which defaults to `fast`.
- A `[[fallback]]` entry needs an `agent`; its effort defaults to `high`.
  `fallback = []` turns the fallback off, and `[[roles.<role>.fallback]]` gives
  one role its own chain.
- Unknown keys, agents, efforts, tiers, wrong types, and negative counts are
  errors that name the key, so a typo never passes silently.
- `placido config` looks for `.placido/config.toml` in the current folder and
  its parents, and prints the resolved settings with each role's exact agent
  command.

### Supported agents

Placido supports three agents. Herdr starts and manages all of them with
`herdr agent start <name> --kind <kind> --pane <id> -- <agent arguments>`;
the model and effort flags are passed through after `--`.

| Agent | Models | Account | Model flag | Effort flag |
|---|---|---|---|---|
| claude | opus, fable, sonnet | Anthropic subscription | `--model` | `--effort low…max` |
| codex | GPT models | OpenAI subscription | `-m` | `-c model_reasoning_effort=…` |
| pi | Any OpenRouter model | OpenRouter API key | `--provider openrouter --model` | `--thinking off…max` |

Billing guards:

- claude and codex always run on their subscriptions. `placido doctor` warns
  when an `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` could switch either agent to
  paid API billing.
- pi always names the `openrouter` provider explicitly. GBD learned that
  without an explicit provider, pi can silently route a model through a paid
  OpenRouter path.

Permissions (decided 2026-10-02): agents run unattended with every approval
bypassed, as GBD did, so a run never stalls on a permission prompt. claude gets
`--dangerously-skip-permissions`, codex gets
`--dangerously-bypass-approvals-and-sandbox`, and pi has no approval system.
The trade-off, accepted knowingly, is that an agent can run any command as the
user. Two startup dialogs remain, researched on 2026-10-02 in the agents' GitHub
issues:

- Claude Code's one-time Bypass Permissions warning cannot be skipped by flag
  (anthropics/claude-code #25503); accepting it once stores
  `skipDangerousModePermissionPrompt: true` in `~/.claude/settings.json`, and
  `placido doctor` fails until it is there.
- Claude Code asks once per new git root whether to trust the folder; parent
  folders stopped counting in v2.1.232, `--dangerously-skip-permissions` does
  not skip it (#36342), and no setting does (#45298 is open). Each issue's new
  worktree asks once. Decided (option B, over asking the user or writing
  `~/.claude.json`, which claude rewrites constantly): placido answers yes
  itself, only when the screen shows exactly that question, moving the
  selection and pressing Enter only once `❯` is seen on "Yes, I trust this
  folder".
- Codex also asks to trust new folders, even with its bypass flag
  (openai/codex #14345): "Trust this folder? Codex can read, edit, and run
  files here", with `›` on "1. Trust and continue". Its trust applies to the
  repository root, so it asks once per project rather than once per issue.
  Placido answers it the same way.
- Codex reviews its hooks before running them ("1 hook needs review", footer
  `t trust all · enter review · esc close`); Herdr's integration hook needs
  trusting again whenever it changes. Decided (option A, over answering it by
  hand after each Herdr update): placido's codex agents get
  `--dangerously-bypass-hook-trust`, so the review never stops a run and the
  hook that reports the session ID always runs. The user's own codex sessions
  still ask.
- Codex notes a warning whenever it gets `-c` settings, which placido always
  passes: "Running without the shared background server", since it then runs
  on its own instead of on its shared app server. It shows only as a status
  line ("press F2 to view") and blocks nothing; if the user opens the panel,
  placido waits for it to close rather than typing into it.

Any other dialog, whether Herdr reports it `blocked` or shows dialog text on the
screen, makes placido save the screen, notify the user, and wait.

Checked on 2026-10-02 with a hello-world call to each agent, all on the
configured models and efforts:

- claude: `--model opus` resolves to `claude-opus-5-5` with a 1,000,000-token
  context window, so the `[1m]` suffix GBD used is not needed.
- codex: the model id is `gpt-6.1-sol` (272k context). Its efforts are `low`,
  `medium`, `high`, `xhigh`, `max`, and `ultra`, which adds automatic task
  delegation and is left out of placido's scale. Codex reports
  `Logged in using ChatGPT`, so it runs on the subscription. The user's global
  `~/.codex/config.toml` sets `service_tier = "fast"`, which spends the
  subscription faster; placido makes the tier a role setting (`tier`) and
  passes it explicitly.
- pi: `--provider openrouter --model deepseek/deepseek-v4.1-flash
  --thinking xhigh` answers, with no API key variables set in the shell.

### Review loop

The review loop follows lessons recorded in GBD's README: a round must earn its
successor; blocking means broken now; one deep review beats many shallow ones;
the reviewer is resumed while the implementer is fresh; and security findings
are never settled by the run on its own judgment.

- **Findings.** The reviewer writes `findings.json` for the script and
  `review.md` for the user. Each finding has an axis (Standards or Spec), a
  severity, a location, a description, and a suggested fix.

  | Severity | Meaning |
  |---|---|
  | blocking | Broken now: wrong behavior against the agreement, a failing or missing test at a seam, a security hole, data loss |
  | significant | A real problem that is not broken yet: fragile design, a likely future bug, an important standards breach |
  | minor | Small smells, naming, documentation |

- **Fixing.** A fix step, using the implement role's settings unless
  `[roles.fix]` overrides them, runs in a fresh session and answers every
  finding as **fixed** (with the check passing), **disputed** (with a reason),
  or **deferred** (recorded as a follow-up issue). The reviewer judges disputes
  in the next round. Security findings at blocking or significant severity
  cannot be disputed away: they are fixed or escalated to the user.
- **Starting a new round.** Round 1 always runs. Another round runs only when
  the previous round had blocking or significant findings, the fixer changed
  something (tests count: on 2026-10-02 the rule "production code only" left a
  rewrite of #463's test-code deliverable unverified) or disputed one, and the
  round budget remains. Minor-only findings are fixed or deferred without a new round.
- **Later rounds.** The same reviewer session is resumed in its pane, which
  keeps cache hits high, and verifies each earlier finding, its resolution, and
  the fix diff for regressions instead of starting a fresh search. The first
  round's prompt states the round budget and asks the reviewer to treat it as
  its only look.
- **Budget exhausted.** Open blocking findings and unresolved disputes are
  escalated to the user with a notification and marked "needs your decision" in
  the summary. Open significant and minor findings go to a follow-up list.
- **Plan review.** Off by default (`plan_rounds = 0`). When enabled, the reviewer
  checks the agreement and slices before implementation; the specification role
  fixes the slices, and any change to an agreed decision becomes a question for
  the user.
- **Failing checks are not review rounds.** When checks fail after a fix, the
  fixer retries within its attempt limit.

### Outcomes and fallback

The fallback exists mainly for **refusals**: providers' safety systems
sometimes flag reviews of Goiabada, an OAuth2/OIDC server, as cybersecurity
risks. The user has applied to the Daybreak and OpenAI cyber programs, which
should make refusals rarer. Following GBD's `gbd/events.py`, each step that
fails is classified by outcome:

| Outcome | Signals (from GBD) | Response |
|---|---|---|
| Refusal | "flagged for possible", "cybersecurity risk", "usage policy", "trusted access", Claude's `model_refusal` event | Move to the next entry of the fallback chain immediately, notify the user, and log the refusal reason |
| Quota | "usage limit", "session limit" | Wait for the subscription to reset, then continue; fall back only if the wait would exceed `quota_wait` (default 2 hours), skipping chain entries on the same subscription |
| Transient | HTTP 429, 502, 503, 529; "overloaded" | Retry on the same agent after a pause |
| Auth | "not logged in", "invalid api key" | Stop and notify the user |
| Unknown | No result file and no recognizable signal | Retry once in a fresh session; if it fails again, stop and notify the user |
| Poor work | Failing checks or review findings | The normal fix loop; never the fallback |

The fallback is automatic, because asking first would stall an unattended run.
Each fallback is logged with its cost, and `placido report` shows totals,
including how often reviews are refused.
The fallback chain is an ordered list. The default tries codex's
`gpt-daybreak-blue-latest` first: it is OpenAI's cyber-permissive model, runs on
the same ChatGPT subscription at no extra cost, and answered a test call on
2026-10-02. If it refuses too, the chain moves on to DeepSeek through
OpenRouter, a different provider whose safety system judges the work afresh.
When every entry refuses, the run stops and notifies the user. After a
fallback, the role stays on the entry that worked until the end of that review
loop, since the same content would likely be refused again.

GBD's run logs hold four real refusals, all from OpenAI's safety system on
`gpt-6.1-sol` reviews, through pi on both the `openai-codex` and `openrouter`
providers: "This content was flagged for possible cybersecurity risk…
confirm that the access_programs.cyber parameter is set to the appropriate
tier, and note that some cybersecurity requests are still limited, even when
Daybreak is on." pi recorded it as a `message_end` event with
`stopReason: "error"` and the text in `errorMessage`. None of the native
claude or codex session files contains a refusal, so no sample exists yet for
codex running natively; step 9 needs one, or a sample built from codex's
documented error events. The codex model list also offers
`gpt-daybreak-blue-latest`, which suggests the account has Daybreak access.

Detection is the main open technical question. GBD read these signals from the
structured output of headless sessions; placido's agents run interactively in
Herdr panes. The plan is to read the end of each agent's transcript file, with
the pane screen as a backup. Step 9 verifies it, with automated tests on sample
transcripts, because the fallback depends on it. No refusal from codex running
natively has been seen yet; rather than simulate one, the first real refusal in
practice becomes the test sample.

### Project knowledge

Placido's skills are generic. Project knowledge comes from conventional files,
following the layering in Matt Pocock's skills, so a project set up for those
skills also works with placido:

| File | Content | Read by |
|---|---|---|
| `AGENTS.md`, with `CLAUDE.md` containing `@AGENTS.md` | Sparse project facts, mostly pointers to other documents | Every agent, automatically |
| `GLOSSARY.md` | The project's domain language | All roles |
| `docs/adr/` | Architecture decisions and their reasons | Specification and implementation, for the area touched |
| `CODING_STANDARDS.md` | Judgment-call rules | Only the reviewer |
| Project skills, such as `.claude/skills/run-tests` | Tool knowledge, such as how to run the project's tests | Agents, when relevant |

Claude Code reads `CLAUDE.md` and Codex reads `AGENTS.md`, so keeping one source
in `AGENTS.md` gives every agent the same facts. Missing files are fine: the
skills proceed without them.

Optional **role notes** at `.placido/notes/<role>.md` are an escape hatch for
knowledge that fits none of these files. Placido adds a role's notes to that
role's prompt only. The valid role names are the configuration roles:

| Notes file | Reaches |
|---|---|
| `.placido/notes/spec.md` | The specification interview, including the agreement and slices |
| `.placido/notes/implement.md` | Each implementer session |
| `.placido/notes/fix.md` | Each session that fixes review findings |
| `.placido/notes/review.md` | The reviewer, for both the plan review and the final review |

Placido's user documentation lists these roles, and `placido doctor` warns about
any notes file whose name is not a known role, so a typo cannot silently drop
knowledge.

`placido init` works like Matt Pocock's setup skill: an agent explores the
repository, presents what it found with a recommended answer for each section,
confirms with the user, and writes `.placido/config.toml`.

### Ideas from Matt Pocock's skills

Matt Pocock's skills repository (MIT licensed) shapes placido's skills. A few
primitives (grilling, test-driven development, the code-review smell baseline,
and codebase design) are copied into placido with credit rather than depended
on, because his plugin updates automatically and could change placido's
behavior without notice.

| Idea | In placido |
|---|---|
| **Grilling:** interview as a design tree, asked in rounds of numbered questions with recommended answers; facts are the agent's job and decisions are the user's; done when no open question remains | The specification interview. "No open question remains" is the trigger for sealing the agreement automatically |
| **Seams:** test only at agreed seams, as few and as high as possible | Kept from GBD, with the "fewest, highest" rule added |
| **Specification and agent-brief format:** problem, solution, decisions, testing decisions, out of scope; behavior rather than procedure; testable acceptance criteria; no file paths | The agreement format |
| **Tracer-bullet tickets:** vertical slices sized for one fresh context window, with blocking edges; refactor first; expand and contract for wide refactors | Stages become slices with blockers; independent slices may later run in parallel panes |
| **Context pointers:** pass pointers to files rather than copies | Short prompts |
| **Two-axis review:** Standards (repository rules plus a code-smell baseline) and Spec (does the change do what was agreed), as separate parallel reviews | The review role |
| **Test-driven development:** red, then green, one vertical slice at a time, through public interfaces | A lighter "test-first at the seams": for each slice the implementer first writes tests at the agreed seams only, confirms they fail, then implements until they pass, recording the evidence. No micro-cycles and no refactoring step, which belongs to review. The point is proof that tests actually test the behavior, since agents writing tests after the code tend to write tautological ones. Switchable with `[implement] test_first` |
| **Setup skill:** explore, present findings with recommendations, confirm, write | `placido init` |
| **Retro:** turn mistakes into automated checks and judgment calls into coding standards | A closing step that suggests project improvements |
| **PR template:** smallest visual summary, before and after evidence, merge danger | The closing summary and pull request body |
| **Push right and brief:** ask the user once, late, with a decision-ready brief | How placido brings questions to the user |
| **Context hygiene:** keep the interview in one context; start each implementation step fresh; stay within about 150,000 tokens | Fresh session per role step |
| **Writing for agents:** leading words, a completion criterion on every step, progressive disclosure, positive instructions, no restating of what the environment already says | The house style for placido's skills |

### Observability

Placido records each run in enough detail to judge afterwards how well it went
and to diagnose failures. Run folders live under `~/placido/runs/`, outside every
repository and worktree, so they survive worktree removal, stay easy to browse,
and allow reports across projects. They are not committed to git. The
`PLACIDO_RUNS_DIR` environment variable can move them. Old runs are deleted by
hand for now. Each issue and run has a folder:

```text
~/placido/runs/<project>/<issue>/
  agreement.md              # sealed agreement and slices, shared by every run of the issue
  <run-id>/
    run.json                # configuration snapshot, tool versions, base commit
    events.jsonl            # one timestamped line per event, in order
    steps/
      04-implement-slice-2/
        prompt.md           # what the agent was asked
        result.md           # what the agent produced
        transcript.jsonl    # copy of the agent's own session log
        screen.txt          # the pane's last screen
        check.log           # output of the project's check command
    summary.md              # the closing account
```

- **The event log** is written only by the placido script: step starts and
  ends, agent, model, effort, pane, outcomes, fallbacks with reasons, checks with
  exit codes and durations, commits, and waits for the user.
- **Agent transcripts.** Herdr does not keep transcripts, but its
  integrations report each agent's native session ID, visible as
  `agent_session` in `herdr agent get`. Placido uses that ID to find the agent's
  own session log and copy it into the step folder, because agents delete old
  sessions after a while. Herdr's `agent read --lines N` can also scroll back
  through an idle agent's screen history, which serves as a backup.
- **`placido report`** reads the run folders without changing them and
  summarizes one run (timeline, durations, retries, review rounds, fallbacks,
  checks, tokens and cost from transcripts, including cache reads and writes) or
  trends across runs, such as how often reviews are refused.
- **Live view:** the placido pane prints events in readable form, Herdr's
  sidebar shows status tokens such as `stage 2/4 · reviewing`, and Herdr
  notifications announce fallbacks and questions.

### Closing agent tabs without losing anything

Each step opens a tab for its agent, and finished tabs pile up (four after the
first sandbox tests). A tab is closed only once everything the agent produced
is safely in the step folder, in this order:

1. The agent is idle, with its result written or the step otherwise ended.
2. The agent is told to exit (`/exit` for claude and codex), so its session
   log is complete and flushed.
3. Placido copies the transcript and saves the screen, then verifies the
   copies: the transcript is not empty and matches its source's size.
4. Only then does placido close the tab, logging `tab.closed`.

If any check fails, such as a transcript that cannot be found, placido keeps
the tab open, logs `tab.kept` with the reason, and notifies the user. Kept tabs
are the evidence for diagnosing the failure. The reviewer's tab stays open
across the rounds of one review, since its session is resumed, and closes after
the last round. The run's workspace closes when the run is cleaned up, together
with `teardown` and the worktree, and only after the same checks for every
step. To be built with steps 7 to 10.

### Herdr capabilities used

| Capability | Use in placido |
|---|---|
| Workspaces, tabs, panes | One workspace per issue; agents in visible panes |
| `worktree create --branch --base` | Task isolation, replacing `gbd-worktree`. One call creates the branch and checkout under `~/.herdr/worktrees/<repo>/<branch-slug>` (Herdr's `[worktrees] directory`), opens it as a workspace, and groups it under the repository's workspace, opening that too if needed |
| `agent start`, `prompt`, `wait`, `read` | Every role |
| Integrations for claude, codex, pi | Native session IDs for resume after a Herdr restart and for finding transcripts. For Claude Code and Codex, lifecycle state still comes from Herdr's screen detection |
| `notification show` | Questions, fallbacks, and completion |
| Pane and workspace metadata tokens | Run status in the sidebar, as a `placido` token reported under source `placido` (`setting up`, `ready`, `setup failed`) |
| `pane run` and `pane wait-output` | Not used yet: a way to run commands visibly in a pane if running them in the background proves too opaque |
| Plugin `worktree.created` hook | Not used yet: Herdr's example plugin bootstraps new worktrees this way; placido runs setup itself so the result is logged |
| Event subscriptions | Reacting to state changes instead of polling |
| Plugins | Actions, link handler, setup hook, status popup, distribution |
| Named sessions | Isolated testing of placido: `HERDR_SESSION=placido-test herdr server` runs a headless server, and `HERDR_CONFIG_PATH` gives it its own worktree directory |
| Detach, reattach, SSH, browser | Runs continue while the user is away |

## Open questions to re-evaluate

### Isolating agent runs (raised 2026-10-02, decide before step 14)

Agents run with every approval bypassed, so an agent can do anything the user
can. Before the first real Goiabada runs, re-evaluate how to limit that:

- **A container per agent** looks like a poor fit. Herdr starts the real agent
  binary and checks that it owns the pane, and its integrations report back
  through Herdr's socket. Subscription logins and transcript folders would
  have to be mounted in. Goiabada's tests start Docker stacks of their own,
  which needs Docker-in-Docker or the Docker socket, and the socket is
  effectively root on the host. Network, the worktree, and the logins stay
  exposed regardless.
- **A dedicated environment for the whole setup** (a second WSL distro, a VM,
  or a container reached over SSH, shown in the local Herdr window through its
  connecting-machines feature) protects the user's home, other repositories,
  and keys at the cost of a one-time setup. Placido itself would not change.
- **The agents' own sandboxes** (Claude Code's sandbox, using bubblewrap on
  Linux, and codex's) might restrict commands without containers; how they
  combine with bypass mode and with Docker-based tests is untested.

The leaning is toward the dedicated environment, or the agents' sandboxes if a
test shows they cope with Docker.

Decided on 2026-10-02, for the first run: accept the risk for now. Issue #331
runs on the user's own WSL as it is, watched live, and isolation is
re-evaluated after the first run. A dedicated environment changes nothing in
placido, so it can come later without rework.

## Implementation plan

Placido is built in small steps. Each step adds one capability and has a test that
must pass before the next step begins. Development runs against a sandbox at
`~/code/placido-sandbox`: a tiny Python project in its own local git repository
with no remote, kept outside this repository because placido creates worktrees,
branches, and commits in it. Python matches placido itself, needs no setup or
dependencies, and `python3 -m unittest` runs in well under a second. Placido
stays language-neutral: a project's language shows up only in its `setup`,
`check`, and `test` commands, and Go is exercised for real on Goiabada at
step 14. A reset script restores a known starting commit so automated tests
and trial runs start clean. The sandbox's issues are local markdown files
(`issues/NN-slug.md`), so it stays offline; real projects use GitHub issues
(decided on 2026-10-02).

The steps follow the order of the workflow itself, and each capability is built
and validated on its own before the next one starts. This favors seeing each part
work step by step over reaching a complete run early (decided on 2026-10-01).

| # | Step | Validation |
|---|---|---|
| 0 | **Skeleton and doctor.** The `placido` CLI and `placido doctor`, which checks herdr, the three agents, their logins and billing guards, the Herdr integrations, and whether it runs inside Herdr. | `placido doctor` reports everything green. |
| 1 | **Run folder and logging.** Create a run folder with `run.json` and append events to `events.jsonl`. | A trial command produces a run folder and a complete, readable event log. |
| 2 | **Configuration.** Read `.placido/config.toml` with built-in defaults; `placido config` shows the resolved settings. | Roles resolve to the expected agents, models, efforts, and agent arguments. |
| 3 | **Sandbox and task workspace.** Create the sandbox repository with a local issue file. `placido start <issue>` creates a worktree and a Herdr workspace and runs the setup command. | The worktree and workspace exist, setup succeeds, and every action appears in the event log. |
| 4 | **Starting an agent.** Start a role's agent in a pane through Herdr with its model and effort, prompt it, wait, and read its result file. Record the native session ID and copy the transcript into the step folder. | The result file has the expected content, the transcript is copied, and the events are logged. |
| 5 | **Specification interview.** `placido-spec`: the grilling interview that writes and seals the agreement, seams, and coarse slices automatically once no question remains. | An interview on a sandbox issue produces a sealed agreement with slices. |
| 6 | **Implementing one slice.** An implementer details and builds one slice; placido runs the check command and commits. | One commit, with the check output recorded as evidence. |
| 7 | **All slices and resume.** Work through the slices in blocker order; save state so an interrupted run continues. | A run killed midway resumes correctly. |
| 8 | **Review.** Two-axis review with severity-gated rounds, a resumed reviewer, and a fix step. | Findings, resolutions, disputes, and the round decisions are recorded. |
| 9 | **Outcomes and fallback.** Classify failed steps from transcripts and screens; fall back on refusal, wait on quota, retry transient errors. | Simulated refusal, quota, and transient outcomes each get the right response. |
| 10 | **Questions and closing.** Pause with a notification when an agent is blocked or needs a decision; write the closing summary and retro suggestions. | A forced question notifies the user and the run waits; the summary is complete. |
| 11 | **Report.** `placido report` for one run and across runs. | The report matches the event logs. |
| 12 | **Herdr plugin.** Manifest, actions, link handler, setup hook, sidebar status. | Placido runs from a key binding and a GitHub link. |
| 13 | **GitHub.** Pushing the branch, a draft pull request, progress updates, ready for review, CI check. Reading GitHub issues moved to step 3. | A full sandbox run ends with a pull request. |
| 14 | **Real project.** Run placido on Goiabada or another project. | A real issue becomes a reviewed pull request. |

## Session log

### 2026-10-01

- Discussed the context and goal: build a generic Herdr workflow inspired by GBD,
  with freedom to adapt and improve it rather than reproduce it one for one.
- Reviewed Herdr's documentation and the existing GBD workflow.
- Created this document with the context, goal, and conceptual description of
  GBD's interview, agreement, handoff, planning, implementation, review, and logs.
- Agreed that the new workflow will produce the agreement and seams and seal
  automatically once the interview resolves all questions, without extra approval.
- Installed Herdr 0.9.3 in Ubuntu on WSL 2 and confirmed it is available in the
  `leodip` login shell.
- Chose Tailscale SSH Console for browser access from the locked-down work machine,
  where installing the Tailscale client is not possible.
- Installed Tailscale 1.102.4 inside Ubuntu, enabled Tailscale SSH, and authorized
  the Linux device as `ubuntu-wsl` in the existing tailnet.
- Verified that Ubuntu is online and that a browser SSH session successfully
  connected as `leodip` and opened a terminal.
- The user tested Herdr successfully in the browser SSH Console from Windows.
- Considered Herdr installed and accessible; remote access setup is done.
- Decided to build the workflow incrementally, in small steps that are each tested
  before moving on.
- Named the workflow **placido** (Portuguese *plácido*, calm). The name is free on
  npm and PyPI, and no AI-agent or workflow tool with that name was found. Rejected
  candidates included lucid, brigade, and cordel, which existing agent tools
  already use.
- Created the project folder at `~/code/ai/placido`, so it is source-controlled in
  this repository alongside `goiabada`.
- Moved this document from the repository root to `placido/placido-plan.md`.
- Agreed on the incremental implementation plan and recorded it above.
- Measured GBD at about 24,000 lines and agreed that placido stays small: Herdr
  owns session handling, and code is added only after failures seen in real runs.
- Reviewed Herdr's documentation in depth and found plugins, metadata tokens,
  event subscriptions, native session IDs, and named sessions useful for placido.
- Reviewed Matt Pocock's skills repository and recorded the ideas placido borrows.
- Chose three agents: claude (Anthropic subscription), codex (OpenAI
  subscription), and pi (OpenRouter API key) as the fallback.
- Installed Herdr's claude (v10), codex (v8), and pi (v9) integrations. Confirmed
  that claude uses the claude.ai subscription, codex is logged in with ChatGPT,
  pi's OpenRouter provider is ready, and no API keys are set in the environment.
- Learned that the fallback exists mainly for safety refusals of Goiabada
  reviews, and designed outcome-based handling following GBD's `gbd/events.py`.
- Designed run observability: event log, step folders, copied transcripts, and
  `placido report`.
- Recorded the design and a start-small implementation plan above.
- Started the interview to confirm the design, one question at a time.
- Q1 (build order): the user chose to build the parts in workflow order, each
  validated on its own (worktree creation, logging, and so on), rather than
  reaching a thin end-to-end run early. The implementation plan was reordered.
- Q2 (conductor): the user considered an orchestrator agent, which would allow
  talking to it during a run, then chose a Python script using only the standard
  library, with agents doing the judgment work. The script still leans on Herdr
  for everything GBD's driver had to build itself: launching and waiting for
  agents, visibility, worktrees, notifications, and resume. It is built as
  composable commands so an orchestrator agent can be layered on later. The user
  added that everything testable in placido's supporting code gets automated
  tests.
- Q3 (sessions and results): agreed on a fresh agent session per step, with
  results passed through files, as a starting point to adjust if needed. The user
  asked about token efficiency: the shared prefix (system prompt, tools,
  `AGENTS.md`) stays cached between back-to-back steps; only the step's own
  context is re-read, while a long session would re-send its whole growing
  history on every turn. `placido report` will measure input, cache-read, and
  cache-write tokens per step from the transcripts so this can be verified.
- Q4 (planning): agreed that the specification session writes the agreement
  and a coarse list of slices and seals both, following Matt Pocock's practice
  of keeping the interview and the breakdown in one context. There is no
  separate planning role or plan review; implementers detail each slice when
  it starts.
- Q5 (skills): agreed that every placido skill works on its own in an ordinary
  agent session, with explicit inputs and a documented result file; the script
  only adds sequencing, checks, logging, and limits around the skills.
- Q6 (packaging): agreed to build placido as a plain CLI first and add the
  Herdr plugin wrapper at step 12, once the commands are stable. Until then,
  `placido start` runs the project's setup command itself.
- Q7 (configuration): agreed on one committed file per project,
  `.placido/config.toml`, with built-in defaults for everything, including the
  agent, model, and effort choices. A user-level file can be layered underneath
  later if personal overrides are ever needed.
- Q8 (default models): the user chose claude Opus 5.5 at xhigh for the
  specification, claude Opus 5.5 at high for implementation, codex gpt-6.1-sol at
  max for review, and pi with deepseek/deepseek-v4.1-flash through OpenRouter as
  the fallback. The values in the configuration example are these defaults; the
  fallback keeps GBD's xhigh effort. The exact codex model id and whether to use
  Opus with the 1M-token context are verified in step 0.
- Q9 (review loop): agreed on severity-gated review rounds (blocking,
  significant, minor), a fix step that answers each finding as fixed, disputed,
  or deferred, a resumed reviewer session across rounds, a final review budget
  of 3 rounds, and the plan review off by default, all configurable under
  `[review]`. See the review loop section above.
- Q10 (outcomes): agreed on outcome-based handling: refusal falls back to pi
  immediately with a notification; quota waits up to 2 hours (the user's choice)
  before falling back; transient errors retry; auth problems stop the run; unknown
  failures retry once. A cross-subscription fallback (claude to codex and back)
  stays a possible later tweak.
- Q11 (project knowledge): agreed on conventional files (`AGENTS.md`,
  `GLOSSARY.md`, `docs/adr/`, `CODING_STANDARDS.md`, project skills) plus optional
  role notes at `.placido/notes/<role>.md`. The user asked for the valid role
  names to be well documented; `placido doctor` warns about unknown names.
- Q12 (Matt Pocock's skills): agreed to copy and adapt the needed primitives
  into placido with credit under the MIT license, rather than depend on his
  automatically updating plugin. The user is not a fan of TDD for human
  development; agreed to try a lighter "test-first at the seams" as the default,
  switchable with `[implement] test_first`, and judge it from real runs.
- Q13 (run folders): agreed on `~/placido/runs/<project>/<issue>/<run-id>/`,
  outside every repository and worktree, movable with `PLACIDO_RUNS_DIR`.
- Q14 (git): agreed that the script commits once per slice and per fix step
  after a passing check, agents never commit, and nothing is pushed until the
  GitHub step.
- Q15 (sandbox): agreed on a tiny Go project at `~/code/placido-sandbox` with
  local issue files and a reset script.
- Completed the interview; the design is confirmed.

### 2026-10-02

- Made hello-world calls to claude (Opus 5.5, low), codex (gpt-6.1-sol, max)
  and pi (DeepSeek V4.1 Flash through OpenRouter, xhigh); all three answered.
- Confirmed that `opus` already has a 1M-token context window, so no `[1m]`
  suffix is needed.
- Confirmed the codex model id `gpt-6.1-sol`, its efforts `low` to `max` (plus
  `ultra`), and its ChatGPT login; found `service_tier = "fast"` in the global
  codex config.
- Found four real refusals in GBD's logs, all from OpenAI on `gpt-6.1-sol`
  through pi; none in native codex or claude transcripts. Decided to wait for a
  real native codex refusal rather than build a sample.
- Made `tier` a role setting, defaulting to `fast` for the codex reviewer.
  `gpt-daybreak-blue-latest` has no fast tier (codex drops it with a warning).
- Turned the fallback into an ordered chain: codex `gpt-daybreak-blue-latest`
  (answered a test call), then pi with DeepSeek. `quota_wait` moved to
  `[limits]`.
- Step 0 built: `bin/placido` (Python, standard library only), `placido doctor`
  with twelve checks, 26 `unittest` tests, and a `~/.local/bin/placido`
  symlink. Logins are read from `claude auth status` (must be `claude.ai` and
  `firstParty`), `codex login status` (must be ChatGPT), and
  `pi auth check --provider openrouter --json`. Billing fails on
  `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `OPENAI_API_KEY`, or
  `CODEX_API_KEY`. Outside a Herdr pane, doctor warns but does not fail.
- Changed the sandbox from Go to Python, so the whole project uses one
  language. Go is first exercised on Goiabada at step 14.
- Step 1 built: `placido/runlog.py` creates
  `<runs>/<project>/<issue>/<run-id>/` (run ID like `2026-10-02T153012`, with a
  `-2` suffix on a clash), writes `run.json` with the placido and tool versions,
  appends events to `events.jsonl` as `{"ts", "event", ...fields}` and closes
  the file after each line, and creates numbered step folders. Events echo live
  as `HH:MM:SS  event  key=value`. `placido log [run]` reprints a run, the
  latest by default, and `placido trial` is scaffolding until `placido start`.
  48 tests pass.
- The user ran `placido doctor` inside Herdr pane `w7:p1` with every check
  green, closing step 0, and ran `placido trial` and `placido log`, closing
  step 1.
- Added `placido runs`, which lists runs newest first with their outcomes;
  `placido log` accepts a run by the name it prints.
- Found `"service_tier":"default"` in codex's own request logs, so `default` is
  a real tier that placido can pass to override the global `fast`.
- Step 2 built: `placido/config.py` and `placido config`, with the resolution
  rules recorded under Configuration in one file. 94 tests pass.
- Agreed to check https://herdr.dev/llms.txt before each Herdr-related step.
  For step 3 it showed the worktree defaults, grouping, metadata tokens, the
  `HERDR_SESSION` and `HERDR_CONFIG_PATH` variables for isolated tests, and the
  `worktree.created` plugin hook; recorded under Herdr capabilities used.
- Step 3 built:
  - The sandbox template lives in `placido/sandbox/template` (`greet.py`, its
    tests, `.placido/config.toml`, and issues `01-shout-flag` and
    `02-many-names`). `placido/sandbox/reset.sh` closes the Herdr workspaces of
    the sandbox's worktrees, removes the worktrees, and rebuilds
    `~/code/placido-sandbox` with one commit tagged `start`. A fixed author and
    date give that commit the same hash on every reset, until the template
    changes (`3459ff0` at first, `fa09c75` after the command menu).
  - `placido start <issue>` takes an issue in `issues/` by name or unique
    prefix, refuses when the branch `placido/<issue>` already exists (resume is
    step 7), records the repository, config, base, base commit, and branch in
    `run.json`, has Herdr create the worktree and workspace, runs `setup` in the
    worktree with its output in `setup.log`, and reports `setting up`, `ready`,
    or `setup failed` in the sidebar. A failed setup ends the run as failed,
    keeps the worktree, and sends a Herdr notification.
  - `placido/herdr.py` wraps the herdr CLI; `placido/proc.py` holds the
    command runner shared with doctor. 115 tests pass, and `start` was tried
    against a separate Herdr session, including a failing setup.
- Decided that `placido start` reads GitHub issues now rather than at step 13,
  since real work starts from issues such as
  `https://github.com/leodip/goiabada/issues/439`; pushing and pull requests
  stay in step 13. `placido/issues.py` accepts a GitHub URL, `owner/repo#N`,
  or a bare number (`439` or `#439`) when the repository's `origin` is on
  GitHub, and reads it with `gh issue view`. Otherwise the argument names a
  local `issues/*.md` file, which is how the sandbox, with no remote, keeps
  working. A GitHub issue's ID is its number and a slug of its title, such as
  `439-rate-limiter-counting-half-moves`, skipping `[bracketed]` prefixes and
  filler words. An issue from another repository than `origin`, or a closed
  one, is refused. The issue's title, link, labels, body, and comments are
  saved as `issue.md` in the run folder, so a run keeps what it worked from.
  Checked live against Goiabada issue 439 in all four forms. 132 tests pass.
- The user ran `placido start 01` in the sandbox from their own Herdr session:
  workspace `w8` opened on a worktree under `~/.herdr/worktrees/`, setup
  passed, and the run reached `run.ready`, closing step 3.
- Step 4: Herdr's docs on agent automation and session state showed
  `agent start` needs an idle shell pane and returns `agent_not_ready` when a
  dialog blocks startup, `agent prompt --wait` tracks states rather than turns,
  and `agent get` reports the native session as `agent_session` (pi reports a
  file path; claude and codex report an ID). A live test in a separate Herdr
  session found that claude stops at its folder-trust question in a new folder,
  even though `~/code` is trusted. Launching agents with bypassed approvals was
  then refused by this Claude Code session's safety check, so the live test of
  step 4 is left to the user.
- Decided on full bypass of agent approvals (option A over bounded automation
  and asking every time); recorded under Supported agents.
- Step 4 built: `placido/step.py` runs one role's step. It writes `prompt.md`
  (the task plus where to write the result), opens a tab in the run's
  workspace, starts the agent there as `<role>-<pane>` with its model, effort,
  and bypass flags, prompts it to read `prompt.md`, waits for `working` and
  then for `idle`, `done`, or `blocked`, and treats `result.md` as the evidence
  of completion, nudging once if it is missing. A blocked agent, at startup or
  during work, gets its screen saved, a notification, and a wait for the user.
  Afterwards it saves the screen and copies the native transcript into the
  step folder. `placido step <role> <task>` runs a step in the project's active
  run, from the main checkout or the worktree. 155 tests pass, with Herdr and
  the agent played by a fake.
- Raised whether Docker could isolate runs; recorded under Open questions to
  re-evaluate, to decide before step 14.
- Decided how a generic placido starts per-run infrastructure such as
  Goiabada's Docker stacks: the project owns scripts, called through `setup`
  and `teardown` with `PLACIDO_*` variables (option A, over a Herdr
  `worktree.created` hook or built-in Docker Compose support). Goiabada's
  compose logic from `gbd-worktree` would move into a script in its repository.
- Replaced `check` and `test` with a general command menu, `[commands.<name>]`
  with `run` and `about`, and `[gates]` naming which commands placido runs
  after each slice and before the final review (option A), because Goiabada's
  unit, data, and integration tests, and later Playwright, differ widely in
  cost. Agents see the menu in their prompts; slices may add gates at step 5.
  The run's `PLACIDO_*` variables reach `setup` and every agent tab. 163
  tests pass.
- First live step, by the user: claude stopped at its one-time Bypass
  Permissions warning (not the folder-trust question). Herdr first reported it
  `blocked`, then about 28 seconds later reported it idle with the dialog still
  showing, so placido sent the prompt, whose Enter chose the default "No,
  exit". Claude quit, and the nudge was typed into the bare shell.
- Fixed: before typing, placido now requires the agent to be alive, idle for
  two seconds, and free of dialog text on its visible screen ("Enter to
  confirm", "Esc to cancel"); a dialog gets the user and placido polls until
  it is gone. An agent that quits ends the step as `agent-exited`, with a
  notification, and nothing is typed into the shell. Screens are read from the
  pane, so they survive the agent. 166 tests pass.
- The saved screens showed the first live step met the trust question first,
  which the user answered, and only then the bypass warning.
- Researched both dialogs and codex's trust prompt; findings and the decision
  to have placido answer Claude Code's trust question (option B) are recorded
  under Supported agents. Added the `claude bypass` check to `placido doctor`.
  171 tests pass.
- The user accepted the bypass warning (`placido doctor` all green) and reran
  the step: claude summarized the issue correctly in `result.md`, the session
  ID was reported, and the transcript was copied. No trust question appeared,
  since the reset reuses the worktree path the user had already trusted.
- The transcript showed a premature nudge: waiting for "working, blocked, idle,
  or done" right after the prompt returned at once on the still-idle agent,
  so placido nudged an agent in the middle of its work. Fixed by waiting only
  for `working` or `blocked` after a prompt, and by re-checking the result
  after the agent has really stopped. 172 tests pass, the new one failing on
  the old code.
- The claude step reran cleanly with no nudge. The codex step stopped at
  codex's trust question; placido notified the user, as it should for a
  dialog it did not yet know. Added that question to the ones placido
  answers. 173 tests pass.
- The user trusted codex's folder and Herdr's hook. A step run by a placido
  process started before the dialog fixes ended `no-result`, its prompt lost
  in codex's dialogs; its transcript was also missing, because Herdr learns
  codex's session ID from that hook, which cannot run until trusted. A
  warning line ("press F2") turned out to be status only; its panel opened
  because the user pressed F2, so placido now leaves it to the user.
- Rerun with codex (gpt-6.1-sol, max): success in 37 seconds, a correct
  summary, the session reported, and the transcript copied. With the earlier
  claude runs, this closes step 4.
- Decided to launch placido's codex agents with
  `--dangerously-bypass-hook-trust` (option A).
- Cleaned up the sandbox with `reset.sh`, which closed the workspace and its
  four agent tabs. Agreed that placido closes a step's tab only once the
  agent has exited and its result, screen, and transcript are verified in the
  step folder; recorded under Closing agent tabs without losing anything.
- Step 5 decisions: the spec agent asks one question at a time, each with
  context, options, and a recommendation (option A, over Matt Pocock's rounds
  of numbered questions and over leaving it to the agent), as GBD did; and it
  hands over `agreement.md` for people plus `slices.json` for placido, which
  checks that both name the same slices (option A, over a strictly formatted
  markdown section or JSON alone).
- Step 5 built:
  - `skills/spec/SKILL.md`, a generic skill usable by hand: gather (facts
    looked up, never asked; the worktree left untouched; probes beside the
    agreement), read back with a provisional count, the interview in plain
    text rather than a question menu, seams (fewest, highest), coarse
    tracer-bullet slices with blockers and extra gates, then `agreement.md`
    (Problem, Solution, Decisions, Seams, Testing, Slices, Out of scope; no
    file paths) and `slices.json`.
  - `placido spec` runs it as an interactive step: an idle agent without a
    result is waiting for the user, so placido shows "waiting for your answer"
    in the sidebar, notifies once, and never nudges. The prompt names the
    skill, the issue, the read-only worktree, where to write, the role notes,
    the slice gates, and the command menu.
  - Placido checks the output (sections present; slice ids counting up;
    blockers only on earlier slices, so no cycles; gates that are project
    commands; every slice title in the agreement) and sends any problems back
    to the agent, up to three times, before sealing: `seal.json` records the
    time, the run, and a hash of each file, so a later edit is detected.
  - Steps gained an `interactive` mode and result checks. 193 tests pass.
- Live interview on sandbox issue 02 by the user, about 5 minutes: the agent
  probed today's behavior (probe kept beside the agreement), asked four
  questions one at a time, raised the count with a reason ("the count is now
  4"), asked a nearby defect (empty names) as work, and noticed issue 01
  changes the same code. The output passed placido's checks first time and
  was sealed: 5 decisions, 1 slice. The user found the interview good. This
  closes step 5.
- The agent recorded two assumptions "since you didn't object". Matt Pocock's
  grilling allows none (every choice goes to the user, kept cheap by
  recommended answers in rounds). Decided (option A, over asking every
  assumption or leaving it unruled): assumptions are allowed only for what is
  not really a choice, when the issue's words or today's behavior settle it,
  the alternative adds what nobody asked for, it touches no interface, schema,
  persisted data, wire format, or security property, and it is cheap to
  reverse; each is listed in the read-back and under "Assumed, not asked" with
  its citation.
- Asked whether roles can mix agents, such as codex for the interview and for
  implementation: they always could, per role in `.placido/config.toml`.
  Added manual one-off overrides, `--agent`, `--model`, and `--effort` on
  `spec`, `implement`, and `step`, used only when typed and logged as
  `agent.override`.
- Weighed mutation testing on GBD's own record: 1,739 mutations over about
  18 issues, 96% killed and 67 survivors, about 25 test runs per find. Red
  before green proves a test can fail while the code is missing, not that it
  catches wrong code, so mutations add something; but full runs cost too much.
  Decided (option A, over GBD's exhaustive mutations and over none): targeted
  mutations, as Google applies them to changed lines, `[implement] mutations`
  killed per slice (default 2, 0 turns them off), aimed at the riskiest
  behavior, survivors fixed or explained.
- Step 6 built:
  - `skills/implement/SKILL.md`: read the whole agreement, detail the slice
    against today's code, tests first at the seams (independent expected
    values, no mocks of the project's own internals, confirmed failing for the
    right reason), build within the slice, mutations, and a result whose top is
    the commit message, followed by evidence and follow-ups; or a `Blocked:`
    result when the agreement cannot be followed, rather than deciding for the
    user. Never commit.
  - `placido mutate <file> --replace <old> --with <new> -- <test command>`:
    the text must match once, the file is restored byte for byte whatever
    happens, and the outcome is recorded in the step's `mutations.jsonl`
    through `PLACIDO_STEP_DIR`, which every agent tab now gets.
  - `placido implement [slice]` needs a sealed, unchanged agreement, picks the
    next slice whose blockers are committed, and checks the result: subject of
    at most 72 characters, an evidence section, HEAD unchanged (the agent did
    not commit), changes present, enough killed mutations, then the slice gates
    plus the slice's own, run by placido with logs in `gates/`. Problems go
    back to the agent up to three times. Placido then commits with the result's
    message and `Placido-Run` and `Placido-Slice` trailers. A blocked slice is
    not committed and notifies the user. 226 tests pass.
- Live implementation of sandbox issue 02's slice by the user, 67 seconds:
  six tests failed before the code (the agent explained the two that already
  passed), a seven-line change to `greet.py` plus README and tests, four
  mutations aimed at the agreement's decisions, all killed with narrow tests,
  the unit gate green, and a commit whose message describes the change, with
  the run's trailers. This closes step 6.
- The agent ran one mutation twice to see the tool's output; placido now
  counts only distinct killed mutations, so a repeat cannot make up the count.
- Step 7 test issue: sandbox issue 03, greetings in other languages (`--lang`
  es and pt, an error for unknown languages, `GREET_LANG` as the default that
  `--lang` overrides, README), chosen to need several slices with real
  blockers. The sandbox's starting commit is now `646c2ff`.
- Step 7b built (tab closing, as agreed): after a step, placido asks the agent
  to quit (`/exit` for claude, `/quit` for codex and pi), copies its
  transcript and checks the copy is complete, then closes the tab. A tab whose
  agent does not exit, or whose transcript is missing or short, is kept with a
  `tab.kept` reason and a notification. `keep_open` leaves the reviewer
  running.
- Step 7a built: `placido run` builds every remaining slice in blocker order.
  The run's state is its event log: a slice is done when committed, and an
  attempt that started without ending was interrupted. Resuming stops the
  interrupted agent (interrupting its turn first, then saving its screen and
  transcript), sets its changes aside in a git stash, and starts the slice
  again. A failed attempt is stashed and retried up to `stage_attempts`; a
  blocked slice stops the run. A lock file keeps two placido processes off one
  run. `placido implement` shares the same code. 243 tests pass.
- Live spec of sandbox issue 03: sealed with 5 decisions, an "Assumed, not
  asked" section following the new rule, and 2 slices (slice 2 blocked by 1).
  Placido's check rejected the first output because slice titles differed from
  the agreement's only in markdown formatting; the agent fixed it in 14
  seconds. The title check now ignores emphasis and code marks. The spec tab
  closed safely: the agent exited, its 630 KB transcript was copied and
  verified, then the tab closed.
- First `placido run` on issue 03: both slices built, gated, committed, and
  their tabs closed after verified transcript copies, in under three minutes;
  the code behaves as agreed (`¡Hola, Ada!`, `GREET_LANG`, `--lang` winning,
  exit 2 for an unknown language). The user found no moment to interrupt: the
  live view printed nothing while an agent worked, so 81 seconds looked like
  one.
- Resume, live: the user reset, started issue 03 again (its sealed agreement
  survives in the issue folder, so no new interview), and pressed Ctrl+C
  during slice 1. The next `placido run` stopped the old agent, copied and
  verified its transcript, closed its tab, stashed the half-done changes, and
  restarted slice 1.
- Fixed: Ctrl+C printed a Python traceback; it now says to run the command
  again to resume, and exits 130. The live view prints a progress line every
  30 seconds while an agent works (screen only, not the event log), and
  stopping a working agent logs `agent.interrupted`. 246 tests pass.
- Ctrl+C now stops the slice's agent at once (interrupt, transcript, tab,
  stash) instead of leaving it working until the next resume.
- A third interruption ended the run as "gave up": interruptions counted as
  failed attempts. Meanwhile an agent left running by older code had finished
  slice 1 on its own, and the resume stashed that finished work, with its
  transcript "missing" because Herdr forgets an exited agent's session.
- Fixed: interruptions no longer count as attempts; the session is logged as
  soon as the agent has started, so the transcript is found after it exits;
  and an interrupted attempt that had finished is salvaged: if its result
  passes every check, including mutations and gates, placido commits it
  rather than redo it. `slice.start` records HEAD for that check. 251 tests
  pass.
- Recovered the sandbox run by hand: restored the finished slice 1 from its
  stash and committed it through placido's checks (result, mutations, gate),
  as the new salvage now does by itself, and copied its transcript.
- `placido run` then built only slice 2, printed progress every 30 seconds,
  and finished: 2 of 2 slices committed, the agent exited, its transcript
  verified, its tab closed. This closes step 7.
- Fixed: the session was logged twice and only after the agent's first turn;
  it is now logged once, as soon as the agent works. 252 tests pass.
- Asked about parallelism. Several slices of one issue at once: not supported,
  since they share one worktree (edits, gates, and commits would mix); real
  support needs a worktree per slice and merges. The user dropped the idea.
  Several issues at once, in one project or several: supported by design (a
  branch, worktree, workspace, run folder, and lock per issue), with three
  fixes made now:
  - Run selection: inside an issue's worktree, a command acts on that issue's
    run; in the main checkout, on the only active run, and with several it
    refuses and lists them rather than guess the newest. Runs are matched by
    the repository's full path, so projects sharing a folder name never mix.
  - Notifications name the issue: "placido · 439-…: slice 2 is blocked".
  - Per-issue infrastructure stays the project's: `PLACIDO_NAME` (such as
    `goiabada-439`) names each issue's Docker stack apart.
  Parallel agents share the subscriptions' quotas; waiting on quota is step 9.
  258 tests pass.
- Step 8 built, from the design agreed in the interview (Q9):
  - `skills/review/SKILL.md`: round 1 as the one deep look; Spec and
    Standards reviewed apart (Fowler's smell baseline as judgment calls, the
    project's rules winning); security flagged across both; blocking means
    broken now. Later rounds verify each earlier finding's answer and the fix
    diff rather than search afresh. Output `review.md` and `findings.json`.
  - `skills/fix/SKILL.md`: a fresh session answers every finding as fixed,
    disputed, deferred, or escalated; blocking findings are never deferred,
    and serious security findings are fixed or escalated, never disputed or
    deferred. Output `resolutions.json`; placido checks, gates, and commits
    with a `Placido-Review-Round` trailer.
  - A `fix` role that follows the implement role unless `[roles.fix]` is set.
  - `placido review`, also run by `placido run` once every slice is
    committed. The reviewer's tab stays open and its session is resumed for
    later rounds. A further round needs serious findings and either a
    production fix (a changed file other than tests and documentation) or a
    dispute on a serious finding to judge; the latter was added because a
    disputed blocking finding with no code change would otherwise never be
    judged. Fixes in the last round are reported as not verified. What the
    budget leaves open goes to the user (blocking findings, undecided
    disputes on them, escalations) or to follow-ups. The account is written
    to the run's `review.md`. 278 tests pass.
- The user confirmed the added rule (a dispute on a serious finding earns a
  round) and the principle behind it: when in doubt, prefer one more review
  round to letting an error slip through.
- First live `placido review` refused to guess between three "active" runs:
  runs whose worktrees the sandbox reset had removed were never ended (closing
  is step 10). A run now counts as active only while its worktree exists and no
  newer run has taken over the same worktree path.
- Live `placido review` on sandbox issue 03: codex (gpt-6.1-sol, max) passed
  it in one round in under four minutes, with no findings. Its review.md is
  thorough: each decision and assumption quoted and checked against code and
  tests, 24 extra probes run, and the Standards axis noting the project has no
  documented standards. A transcript unchanged since the step ended is no
  longer copied and logged a second time when the reviewer's tab closes.
- The fix path proven live too, on a planted bug (commit 0fd6530 dropped the
  GREET_LANG lowercasing and its test): codex flagged it blocking in round 1,
  a fresh claude fixed it test-first in 41 seconds (commit 55a74dd with the
  review-round trailer), and the resumed codex session, same tab and same
  session id, verified it resolved in round 2. Step 8 is done.
- The review summary now tells what became of each finding (its severity,
  axis, location, title, then each answer and verdict, such as "fixed in
  55a74dd → resolved (round 2)"), and each round in a line, instead of counts
  alone.
- Step 9 started. Checked Herdr's docs: agent state is only idle, working,
  blocked (codex may also be unknown), with no error state, so failures are
  classified from the agent's transcript and screen. No local transcript holds
  a refusal, quota, or overload sample from any of the three agents (the "529
  overloaded" and "terminated" texts first taken for pi samples come from
  pi's own docs).
- Q1 (refusal mid-slice): the user chose a clean restart. The partial changes
  go to a git stash as for any failed attempt, the fallback agent builds the
  slice from the start, and a refusal does not count against
  `stage_attempts`.
- Q2 (reviewer refused in a later round): the user chose to have the fallback
  take over the same round, its prompt pointing to the earlier rounds'
  `review.md`, `findings.json`, `resolutions.json`, and fix commits, verifying
  as the skill says; the round budget is unchanged. A refusal in round 1 means
  the fallback does round 1 from scratch.
- Researched how each agent records a failed turn (sources: codex-rs at
  rust-v0.160.0, the bundled JS in claude 2.1.287, pi-mono at 581e7ba):
  - codex: `Error` events are not persisted, but the terminal error is copied
    into the persisted `task_complete` event as `payload.error` {message,
    codex_error_info}. `codex_error_info` names the class: `cyber_policy`,
    `bio_policy`, `invalid_prompt`, `misalignment_policy_violation` (refusal),
    `usage_limit_exceeded` (quota), `context_window_exceeded`,
    `server_overloaded`, `internal_server_error`, `http_connection_failed`,
    `response_too_many_failed_attempts`, `unauthorized`, `other`. A plain 401
    arrives as `http_connection_failed` and needs the message text. The quota
    reset is the epoch `resets_at` of the last persisted `token_count` rate
    limits; the message's "try again at 3:41 PM" is a fallback. Codex retries
    streams 5 times itself; the session stays alive after an error.
  - claude: an assistant entry with `isApiErrorMessage: true`, an `error`
    field (`rate_limit`, `authentication_failed`, `billing_error`,
    `invalid_request`, `server_error`, …) and the text. A refusal has
    `message.stop_reason: "refusal"` and "API Error: …'s safeguards flagged
    this message"; quota reads "You've hit your session limit · resets 3pm
    (tz)" with `quotaLimits` holding the epoch; context reads "Prompt is too
    long". Claude retries 10 times itself.
  - pi: a message with `stopReason: "error"` and a free-text `errorMessage`;
    a retried failure stays in the file followed by a `context_edit`, so only
    an error without one is final. pi retries 3 times itself.
  - Not yet seen live: OpenRouter's 402 and moderation texts, and claude's
    exact JSONL field casing. Real failures, when they happen, become test
    samples.
- Q3 (context window): all three agents auto-compact in interactive mode, so
  a full context normally compacts and carries on. The overflow error only
  comes when compaction cannot save the turn. The user chose: retry once in a
  fresh session of the same agent (a reviewer with the Q2 handover, a slice
  restarted cleanly as a failed attempt), then stop and notify. `placido
  doctor` checks that claude's auto-compact is on (`autoCompactEnabled` and
  the `DISABLE_AUTO_COMPACT` and `DISABLE_COMPACT` variables).
- Defaults chosen for step 9 without a question: after a quota wait, the same
  session continues with a short prompt, since nothing about the content was
  judged; a transient error, which reaches placido only after the agent's own
  retries, gets up to 3 more tries in the same session after pauses of 2, 5,
  and 10 minutes; after a fallback, implementation stays on the fallback
  agent for the rest of the run's slices, as the reviewer does for the rest
  of its review.
- Step 9 built:
  - `placido/outcomes.py` reads why an agent's last turn failed: codex's
    `task_complete` error and category, claude's API Error entry, and pi's
    final error that was not retried, with the screen as a backup. Kinds:
    refusal, quota (with the reset time), transient, auth, context, error.
    Run over all 32 local transcripts, it reported no false failures.
  - A step that stops without its result checks for a failed turn before
    nudging. Quota within `quota_wait` is waited out (at most twice per step)
    and transient errors get the three pauses, each followed by a prompt to
    carry on in the same session; any other failure ends the step with its
    kind and an `agent.failed` event.
  - `placido/fallback.py` holds a role's chain and the agent in use, rebuilt
    from `agent.fallback` events so a resumed run stays where it was. Quota
    skips entries on the same subscription (claude: Anthropic, codex:
    ChatGPT, pi: OpenRouter).
  - The driver stashes the attempt and moves on after a refusal or quota
    (not counted against `stage_attempts`), stops on auth (`logged-out`),
    and gives up on a slice whose context overflows twice. The review loop
    does the same for the reviewer and the fixer; a fresh reviewer in a later
    round gets the handover prompt, and each review starts again on the
    configured agents (`review.start`).
  - `placido doctor` checks claude's auto-compact. 358 tests pass.
- Closed a gap between `placido run` and `placido implement`, at the user's
  request. The scenario: `placido run` is refused by claude on slice 2 and
  moves the implement role to codex daybreak-blue, which builds it; slice 3
  then fails three times for an ordinary reason and the run gives up; the
  user retries slice 3 by hand with `placido implement 3`. Before the fix,
  `placido implement` ignored the move in the log and started claude again,
  which would likely be refused again. Now:
  - Both commands get the implement agent from `driver.implement_chain`,
    which reads the run's `agent.fallback` events, so they always agree on
    the agent the issue is on. `--agent`, `--model`, or `--effort` on
    `placido implement` still wins, as a deliberate one-off choice.
  - Both answer a failed attempt with `driver.after_failure`: the attempt's
    changes go to a git stash (before, `placido implement` left them in the
    worktree, where a retry would mix them with its own), a refusal or quota
    moves the role to the next agent and logs it for later commands, and a
    logout or a repeated context overflow stops.
- Live check of step 9 on sandbox issue 02 (the user reset the sandbox and
  started the issue again; its sealed agreement from the morning was reused,
  so `placido spec` rightly refused to redo it): `placido doctor` all green
  with the new `claude compact` line; `placido run` built and committed the
  slice in 63 seconds and passed review in one round in under two minutes,
  with `review.start` logged and no `agent.failed`, `agent.retry`, or
  `agent.fallback` events. The happy path is unchanged by the new failure
  handling. Real refusal, quota, and transient samples are still awaited;
  the first one seen live becomes a test sample. Step 9 is done.
- Step 10 started. Checked Herdr's docs: `workspace close` closes only
  Herdr state, and `worktree remove` runs `git worktree remove` (with
  `--force` for a dirty checkout) and never deletes the branch.
- Q1 (blocked slice): the user chose to answer in the agent's tab. Placido
  keeps the blocked agent open, notifies, and waits as the spec interview
  does; the user answers there, with follow-ups if needed, and the agent
  finishes the slice with its context intact. The decision is recorded in
  `decisions.md` in the issue folder, beside the sealed agreement, which
  stays untouched; later slices and the reviewer read it, so the reviewer
  does not flag the decision as a deviation. If placido is stopped while
  waiting, resuming restarts the slice.
- Q2 (review findings that need the user): the user chose to handle the
  fixer's escalations like a blocked slice: placido keeps the fixer's tab
  open, notifies, and waits; the user decides there, the fixer applies the
  decision (recorded in `decisions.md`), and the next round verifies it.
  Escalations left when the round budget runs out go to the summary and, from
  step 13, the pull request, for the user to settle when reviewing it; the
  run neither waits nor adds rounds beyond the budget.
- Q3 (cleanup): the user chose cleanup on request. When a run finishes,
  placido writes the summary and logs `run.end`, and tears nothing down, so
  the branch can be tried before merging. `placido close` (in the issue's
  worktree, or naming the issue) runs `teardown`, removes the worktree
  (refusing with uncommitted changes unless `--force`; the branch stays),
  closes the workspace, and logs `run.end` for a run that never got one. The
  sandbox reset uses it instead of removing worktrees behind placido's back.
  The Herdr plugin (step 12) can make it a sidebar action.
- Q4 (summary and retro): the user chose the summary always and the retro on
  request. Placido writes `summary.md` from the event log and the run's files
  at the end of every run, with no agent (it becomes the PR body in step 13).
  `placido retro` runs an agent that reads a run and suggests project
  improvements (mistakes into automated checks, judgment calls into
  standards), never applying them; a retro across runs may come with step 11.
- Step 10 build order: (a) questions, in the implementer's and the fixer's
  tabs, with `decisions.md`; (b) `summary.md` and `run.end` when the run's
  work is done, a run staying selectable until closed; (c) `placido close`,
  used by the sandbox reset; (d) `placido retro`.
- Step 10a built (questions):
  - An implementer or fixer that needs the user writes a `Blocked:` result.
    `Step` takes an `ask` callback: when the result holds a question, placido
    keeps it as `question-<n>.md` in the step folder (`agent.question`),
    notifies "<step> asks you", shows "question for you" in the sidebar, and
    waits, as in the spec interview, until the agent writes its result again
    (`agent.answered`). The tab stays open throughout.
  - `placido/decisions.py`: `decisions.md` in the issue folder. The agent
    appends `## D<n> · <step> · <date>` with Question, Decision, Why; the
    slice and fix checks reject a result until every question of the step
    has its decision. Implement, fix, and review prompts name the file, and
    the skills say a decision overrides the agreement where they differ.
  - The old path, where a blocked slice stopped the run with `slice.blocked`,
    is gone; `slice.blocked` stays readable in old logs. The fixer's
    `escalated` answer now means the user chose to leave a finding for the
    pull request.
- Step 10b built (summary and run end):
  - `placido/summary.py` writes `summary.md` from the log and the run's files:
    state, issue, branch, time; each slice with its commit and failed
    attempts; this run's decisions; the review's account; the slices'
    follow-ups; and what went wrong on the way (fallbacks, failed turns,
    quota waits, retries, kept tabs). `placido run` and `placido review`
    write it whenever they finish; a stopped run's state says how to resume.
  - `run.end` is logged when the run's work is done: the review passed or
    ended escalated (or the slices are done with no review configured). A
    failed review or a stopped run gets none, since it can resume.
  - A run stays selectable after `run.end`, until `run.closed` (logged by
    `placido close`), so its branch can be tried, reviewed again, or
    cleaned up.
- Step 10c built (`placido close`):
  - Probed on an isolated Herdr server: `worktree remove --workspace` removes
    the checkout and closes the workspace, keeps the branch, and refuses a
    checkout with changes (`dirty_worktree_requires_force`).
  - `placido close [issue]` (the worktree's run, an issue by prefix, or
    `--run`) refuses uncommitted changes, runs `teardown` (`teardown.log`,
    with the run's variables), removes the worktree through Herdr (git alone
    if Herdr lost the workspace), logs `run.end` as abandoned when the work
    was not done, then `run.closed`, and rewrites the summary. `--force`
    discards changes and goes on past a failing teardown. It refuses to run
    from a pane in the issue's own workspace, which closing would end, and
    while another placido process holds the run's lock.
  - `sandbox/reset.sh` closes each issue with `placido close --force` before
    rebuilding, so sandbox runs end properly.
- Step 10d built (`placido retro`):
  - A `retro` role, following the `spec` role (claude opus, xhigh) unless
    `[roles.retro]` sets it, since a retro is judgment about the project.
  - `skills/retro/SKILL.md`: read the summary, the log, the steps that went
    wrong, every review round, the decisions, and the project's standing
    knowledge; suggest, cheapest first, automated checks for mistakes caught
    late, written standards for repeated judgment calls, documented answers
    for questions the user had to answer, and notes or settings for agents
    that went the wrong way; problems with placido itself go in a separate
    "For placido" section. It changes nothing.
  - `placido retro` runs it as a step and keeps `retro.md` in the run folder.
    On a closed run, the agent works from the calling pane's workspace and
    the repository's main checkout. 385 tests pass.
- Added sandbox issue 04-farewell (a `--bye` flag) whose farewell wording is
  deliberately left open, with the instruction that the implementer must ask
  the user: a reliable way to test questions live.
- Step 10 is built. Next: the user's live test of a question and of closing.
- Live test of questions on sandbox issue 04 (`--bye`, wording left open):
  the spec left the wording to the implementer to ask; the implementer wrote
  the tests that did not depend on it, then asked (`agent.question`, kept as
  `question-1.md`); the user answered "See you" in its tab; the agent
  recorded D1 in `decisions.md` and finished, and placido logged
  `agent.answered`, ran the gate, and committed. The reviewer read D1 and
  even checked from the transcript that the code came after the answer.
  `run.end outcome=passed` and `summary.md` with "Decisions you made"
  followed. Fixed: a follow-up the agent wrapped over two lines became two
  broken items in the summary; wrapped list items are now joined.
- Live `placido retro` on issue 04 (claude opus xhigh, 4 minutes): a sharp
  retro. It looked beyond the run, at the 02 and 03 runs, and found the three
  interviews asked the same command-line questions (parser, usage line,
  option position, test seam), with agents reading "keeps printing the usage
  message" in opposite ways. It suggested an `AGENTS.md` in the sandbox
  template with greet.py's conventions; fixing the template's no-name test,
  which leaks a usage line into every gate and mutation log; and, for
  placido, that the spec skill should record an issue's instruction to a
  later step as a decision, not an assumption (the user had to correct the
  spec agent on exactly that).
- The user's choices on the retro: no `AGENTS.md` in the sandbox (the
  interviews are part of what the sandbox tests). Applied the other two:
  - The template's no-name test now captures both streams, so no usage line
    leaks into gate and mutation logs. It checks stdout is empty and stderr
    starts with `usage: greet.py `, rather than the exact text the retro
    proposed, so the test does not nudge agents into reading "keeps printing
    the usage message" as a frozen usage line.
  - The spec skill: what an issue leaves open on purpose, or an instruction
    it gives a later step, is recorded as a decision addressed to that step,
    never as an assumption.
- Live `placido close 04` from the sandbox's main checkout: the worktree and
  the issue's workspace were removed, the branch `placido/04-farewell` stayed,
  the run kept its `run.end outcome=passed` and got `run.closed`. (The
  sandbox has no teardown, so that part is covered by the unit tests.) Step
  10 is done.
- Next: step 11, `placido report`.
- Step 11 started. Checked what transcripts record: claude, tokens per reply
  (each reply logged several times, so counted once by message id); codex,
  running token totals (with reasoning) and the ChatGPT usage window's
  `used_percent` and reset; pi, tokens and OpenRouter's real cost per reply.
- Q1 (cost): the user chose tokens for every agent, the real dollars spent on
  OpenRouter, and, for codex, how much of the usage window each step used. No
  estimated API prices.
- Q2 (where to read it): the user chose terminal text plus `--json`:
  `placido report` for one run, `placido report --runs` for trends across
  runs (the current project's, or every project's with `--all`), and the same
  data as JSON for the plugin or later charts.
- Step 11 built:
  - `placido/usage.py` reads a step's usage from its transcript copy, counting
    only what falls inside the step's start and end, since a resumed
    reviewer's transcript holds earlier rounds: claude per reply, counted
    once per message id; codex as the difference of its running totals (its
    input includes the cached part); pi per reply with its real cost. Codex's
    usage window reports whole percent of a weekly window, so a single step
    rarely moves it; the report shows its first and last readings across a
    run instead of per step.
  - `placido/report.py` builds a run's report from its log and transcripts:
    steps (time, agent, duration, outcome, tokens with the cached share,
    notes such as "asked you", "result sent back", "resumed", a failure
    kind), slices and attempts, the review's rounds and findings, questions
    and the time waiting for the user, usage by agent, fallbacks and
    refusals. A run never closed whose worktree is gone reads "left
    (worktree gone)". `--runs` lists runs by date with totals and averages;
    `--json` gives the same data.
  - Checked against the sandbox's event logs: rounds, findings, attempts,
    and questions match. 396 tests pass.
- Live check by the user: `placido report` and `placido report --runs` in the
  sandbox give the output checked against the logs. The 14:10 issue-02 run
  reads "abandoned, closed" although its review passed: it ran before
  `run.end` existed, so the reset's `placido close` ended it as abandoned. A
  historical quirk, not a bug. Step 11 is done.
- Next: step 12, the Herdr plugin.
- Step 12 started. Read Herdr's plugin docs: a manifest of actions (bindable to
  keys), event hooks, panes (overlay, popup, split, tab, zoomed), and link
  handlers; action commands run in the background, so a long placido command
  must open a visible tab; `herdr plugin link` serves a working copy.
- Q1 (Ctrl+click on a GitHub issue link): the user does not want a link
  handler at all; dropped from the plan.
- Q2 (what the plugin gives): the user wants minor indicators and visual
  references without complication, and chose B: two read-only popups, the
  status of every issue in flight and the focused issue's report. Nothing
  that starts or deletes anything.
- Found: placido's `placido` workspace token was never visible. Herdr shows a
  custom token only when the sidebar layout names it (`$placido` in
  `ui.sidebar.spaces.rows`), and the user's Herdr config has no layout. The
  rows accept per-token styles and up to 16 rules (`starts_with`, `equals`,
  …) that set the colour, boldness, or dimness from the token's value.
- Step 12 built:
  - Statuses start with a state word (`working`, `you`, `waiting`, `done`,
    `stopped`, `ready`) and stay short for the narrow sidebar: `working ·
    slice 2/3`, `you · answer in implement-slice-2`, `waiting · quota until
    15:40`, `done · review passed`, `stopped · every agent refused`. Each is
    also kept in the run folder's `status` file. A run's end, a stop, and
    Ctrl+C now set one too.
  - `placido status` lists every issue not yet closed, across projects, with
    its status and how long since anything happened, those needing the user
    first.
  - The plugin (`herdr-plugin.toml`, `plugin/`): actions `status` and
    `report` open popups running `placido status --watch` and `placido report
    --workspace <focused> --wait`. Read-only.
  - `placido herdr` prints the Herdr config lines: a third Space row with
    `$placido`, coloured by `starts_with` rules, and keys `prefix+a` and
    `prefix+i` (free among Herdr's defaults). `--install` links the plugin,
    validates the new config with `herdr config check` on a copy, keeps the
    old file as `config.toml.before-placido`, and refuses to touch a config
    that already lays out the sidebar. Verified with Herdr: `Config: ok`.
  - `placido doctor` warns when the sidebar hides placido's status. 410
    tests pass.
- Live check: the token was set on the issue's workspace, but the sidebar
  did not show it. I wrongly concluded that Herdr draws a worktree's
  workspace as one line holding only the first row, and added a
  `placido_state` word before the name. After the user reloaded Herdr's
  config, both showed: the third row does appear under an issue's name, so
  the first screenshot had been taken before the reload. Reverted to the one
  coloured status line under the name (`ready · run placido spec`);
  `placido herdr --install` replaces the interim layout, the old token is
  cleared, and the doctor treats the interim layout as outdated.
- Live: after the reload, the issue's line under its name went from
  `working · spec` to a yellow, bold `you · answer in spec` when the
  interview waited, and `prefix+a` opened the "Placido status" popup listing
  `placido-sandbox  01-shout-flag  you · answer in spec  (just now)`.
- Fixed: after the spec sealed, the sidebar kept `working · spec`. A sealed
  spec now shows `ready · run placido run`, and a failed one `stopped · spec
  <outcome>`.
- The user decided the Herdr visuals add little and asked to bin them.
  Removed: the plugin (`herdr-plugin.toml`, the popup scripts, its
  registration), `placido herdr` and the lines it added to the user's Herdr
  config (restored to `onboarding = false`, the backup deleted), the doctor's
  sidebar check, and the `placido` sidebar token placido had reported since
  step 3 (it only existed to be displayed). Kept: `placido status`, a plain
  terminal list of every issue in progress, needing-you first, read from the
  run folders, where each run keeps its latest status in a `status` file.
  Step 12 ends here: no Herdr plugin.
- Next: step 13, GitHub.
- Step 13 started. Read GBD's GitHub flow: push after every stage, an early
  draft pull request updated as stages land, marked ready at the end, then a
  wait for the full suite (Goiabada's CI holds its database jobs back while a
  pull request is a draft), and back into draft when it is red.
- Q1 (when to push): the user chose once, at the end: after the review, push
  the branch and open the pull request, its body logging what was done.
  Nothing half-done is published; a stopped or abandoned run leaves nothing
  on GitHub.
- Follow-ups (the user prefers folding work into the same branch):
  - The spec interview looks for adjacent work, problems in the code it
    reads and open issues touching the same area, and asks the user whether
    to fold each in; what is folded in becomes part of the agreement.
  - Round 1 of the review also triages the slices' follow-ups: in scope
    becomes an ordinary finding, folded in by the fixer; out of scope is
    listed with the reason. The fixer may defer a finding only as out of
    scope, saying why.
  - When the review ends, placido lists the out-of-scope follow-ups once, in
    the `placido run` terminal with a notification, and the user picks which
    to fold in (all, none, or some). Each pick becomes a decision in
    `decisions.md`; a fix round builds them and the reviewer verifies them in
    one more round. The rest become GitHub issues, created after the pull
    request and linked from its body's Follow-ups section, not a comment.
- Q2 (CI): the user chose to open the pull request ready for review (so a
  workflow that holds jobs back on drafts, as Goiabada's does, runs its full
  suite at once), wait for CI, and on a red run give the failing jobs' logs
  to a fixer agent; placido runs the gates, commits, and pushes, up to 2
  attempts, then stops and notifies. The summary and the pull request say
  whether CI is green.
- Q3 (closing issues): the user chose `Closes #N` in the pull request body,
  for the run's issue and every issue folded in during the interview, so the
  user's merge closes them; placido never closes an issue itself, and the
  user can drop a line before merging to keep an issue open.
- Q4 (where to test): the user will pick a small Goiabada issue, so step
  13's first live run is also step 14's. Until then, step 13 is tested with a
  fake `gh`. Goiabada's `check.yml` runs on `opened`, `synchronize`,
  `reopened`, and `ready_for_review`, and skips its four database jobs only
  on drafts, so a pull request opened ready runs the full suite.
- Found while planning: `[gates] final` was configured but never run. It now
  runs once every slice is committed, before the review.
- Step 13 build order: (a) the final gates; (b) follow-up triage and the
  fold-in question; (c) push, pull request, follow-up issues, CI wait and
  fix; (d) the spec interview's adjacent work and the issues it folds in.
- Step 13a built: `placido/checks.py` runs `[gates] final` once every slice
  is committed, before the review (`final_gates.passed` or `.failed`, logs in
  the run folder's `final-gates/<attempt>/`), and a resumed run that passed
  them does not run them again. A failure goes to a fixer agent (the `fix`
  role) following the new `skills/checks/SKILL.md`: fix the cause, never
  weaken a check, ask with `Blocked:` when the failure is not the change's
  fault. Placido reruns the gates and commits with a `Placido-Check-Fix`
  trailer, up to 2 fixes; then the run stops with `stopped · final gates
  fail`. The same fix step will serve CI.
- Step 13b built (follow-ups):
  - Round 1's prompt lists each committed slice's result; the reviewer
    triages their `## Follow-ups`: in scope becomes a finding, out of scope
    goes to a new `followups` list in `findings.json` (ids `F1`…, with why it
    is outside; round 1 only, checked). The reviewer accepts a deferral only
    for out-of-scope work, and the fix skill no longer lets the fixer defer
    in-scope work for being too much.
  - When the review ends, the out-of-scope follow-ups and any deferred or
    unjudged findings are offered once in the `placido run` terminal ("all,
    none, or numbers"), with a notification and `you · fold in follow-ups?`.
    Without a terminal, none are folded in.
  - Each pick is recorded by placido in `decisions.md` ("Fold in: …? Yes"),
    becomes a significant finding for one fix round, and is verified by the
    reviewer in one more round beyond the budget. What that round leaves
    unresolved, or any serious finding it raises, goes to the user; a minor
    one becomes a follow-up. The account shows "Round N: folded in …" and a
    "Folded in at your request" section; the rest stay as follow-ups for
    step 13c to file as issues. 417 tests pass.
- Step 13c built (`placido/github.py`): after a review that passed or ended
  escalated, placido pushes the branch, opens the pull request ready for
  review (title: the issue's; body: `Closes #N` for the run's issue and each
  issue in the agreement's `Folded in` section, then the summary without its
  title or run state, and a footer naming the run), files the follow-ups the
  user did not fold in as issues (body: what, why it was left out, a link to
  the pull request), and rewrites the body with their links. It then polls
  `gh pr checks`: no checks within 3 minutes is "none", and the wait ends at
  `limits.ci_wait` (1h) as "pending". A red run's failed job logs (`gh run
  view --job --log-failed`, last 400 lines) go to the checks fixer, which
  commits; placido pushes and waits again, up to 2 fixes. `run.end` (with
  `ci` and `pr`) is logged only when CI is green, absent, or there is no
  GitHub remote ("local"); otherwise the run stops and `placido run`
  resumes the delivery, reusing the pull request and the filed issues. The
  summary gains the pull request, CI, and "Follow-up issues". 425 tests pass.
- Step 13d built: the spec skill looks for adjacent work while reading
  (problems in the code the issue touches, and open GitHub issues on the
  same area via `gh issue list --search`), reports it in the read-back, and
  asks each as work, recommending folding in what is small and close.
  Folded-in work is decided and sliced like the rest, and listed under a new
  optional `## Folded in` section, whose `#N` issue numbers become the pull
  request's `Closes` lines (only when the change finishes that issue).
- Step 13 is built. Its live test is the first Goiabada run (step 14).
- Step 14 started: Goiabada. A survey of `gbd-worktree`, Goiabada's
  `run-tests.sh`, its CI, and GBD's gates gave the setup: one compose
  project per issue from `src/.devcontainer` (no published ports), tests
  run inside the devcontainer with `devcontainer.json`'s environment, unit
  tiers, data and integration tiers per database (SQLite fastest), a lint
  tier, `schemadump`, `ownershipdump`, and mocks; CI runs the database matrix
  on pull requests that are not drafts.
- The user's choices: start every database with the stack (development
  commonly needs them all), not on demand; keep `.placido/` out of
  Goiabada's git (`/.placido/` added to its `.gitignore`, pushed to main as
  661f902f); and scope AGENTS.md's (and CLAUDE.md's) "95% confidence, ask
  follow-up questions" rule to work with a person, since it would stop an
  unattended implementer at every step (6ebd2c66, pushed to main).
- Consequence for placido: a project's `.placido/` may be untracked, so it
  exists only in the main checkout, not in an issue's worktree. Placido must
  read role notes from the main checkout, and give setup and teardown the
  main checkout's path (a `PLACIDO_REPO` variable) to find project scripts.
- The user reversed the `.gitignore` choice: `.placido/` stays in Goiabada's
  source control after all (reverted as 92ba551c, pushed to main), so each
  issue's worktree has the config, scripts, and notes, and placido needs no
  change for untracked project files.
- At the user's request: removed the "Important Note" (ask until 95%
  confident) from Goiabada's AGENTS.md and CLAUDE.md entirely (b0394311,
  pushed to main); placido's spec interview has its own asking rule, and
  implementers work from the sealed agreement. Deleted the 19 untracked
  files in the Goiabada checkout (package review output and Strix scan runs).
- Goiabada set up for placido, pushed to its main:
  - 92a446c2: a "Standards" section in AGENTS.md and CLAUDE.md asking every
    agent to read the official text of the standards a change touches
    (OAuth 2.0 and its RFCs, OpenID Connect) and follow MUST and SHOULD.
  - 6a535289: `.placido/stack.sh` (up, down, run, vet, exec, ps: one compose
    project per issue named by `PLACIDO_NAME`, every database, no published
    ports, tests run in the devcontainer as `vscode` with devcontainer.json's
    environment, which docker exec does not apply itself); `config.toml`
    (setup and teardown, a command menu with timings and how to narrow each,
    slice gates vet and unit, final gates lint, setup-wizard, data-sqlite,
    integration-sqlite; the database matrix left to CI); notes for implement
    (commands, fast loop, regeneration checklist, narrow mutation commands,
    standards), fix (follow implement; reproduce red CI on its engine),
    review (security product, standards with MUST as blocking, all four
    engines, current generated files), and spec (settle engines, migrations,
    API and security early; cite standards; seams; gates).
  - Tested on a throwaway worktree of main: `up` in about 4 minutes (image
    build) with every database answering; vet 22s, core unit 7s, data on
    SQLite 42s, a narrow integration run on the stack's MySQL 32s, lint 19s,
    all passing; `down` in 7s left no container, volume, network, or image.
- Next: the user picks a small Goiabada issue for the first live run.
- Placido moved to its own repository, github.com/leodip/placido (cloned at
  `~/code/placido`), as a snapshot without history; its history up to the
  move stays in github.com/leodip/ai. `~/.local/bin/placido` already pointed
  at `~/code/placido/bin/placido`. The old `placido/` folder matched this
  repository except for the rewritten README and the new LICENSE, so at the
  user's choice it was removed from leodip/ai with no pointer left behind
  (ead2ec5).
- The user chose Goiabada issue #331 (strip release binaries with `-s -w` in
  both Dockerfiles and `build-binaries.sh`, dev and test builds unchanged)
  for the first live run, over #410, #152, and #289. It is build-only, so its
  slices' tests and mutations must work against build checks (stripped
  binaries, traceback and `core/errs` frames kept) rather than Go seams; the
  interview settles how. The user runs every live command; placido's side is
  building, testing, and giving exact steps.
- Isolation (the open question due before step 14): the user chose to accept
  the risk for the first run, on the current WSL, and re-evaluate afterwards
  (over a dedicated WSL distro first, or testing the agents' own sandboxes).
- First live `placido start 331`: setup failed at once with exit 126.
  Goiabada's repository has `core.fileMode = false`, so the `chmod +x` on
  `.placido/stack.sh` was never recorded and git stored it as 100644; a fresh
  worktree checked it out without its executable bit. The user recorded the
  bit in Goiabada (35a77b4e, pushed to main); it was the only such file.
- The failure exposed an orphan in placido: a run whose setup failed never
  logs `run.ready`, so `placido close 331` did not find it, and `placido
  start 331` refused the existing branch, leaving only `close --run` with the
  folder spelled out, then deleting the branch by hand. At the user's request
  I cleaned up by hand (worktree and workspace removed through Herdr, the
  local branch and the run folder deleted). The user's choice (over making it
  closable only, or having start tear everything down on failure):
  - `placido close` also finds a run that never got ready (`unready` in
    `active_runs`, `find_run`, and `select_run`); every other command still
    ignores it, since its setup is not up.
  - `placido start <issue>` on an existing branch whose latest run failed
    setup, is not closed, still has its worktree, and has no commits beyond
    the base starts a new run in that worktree: `run.start` with `retry_of`,
    a fast-forward to the base (where the fix most likely landed; a fix made
    in the worktree itself also works), `worktree.created` with `reused`,
    then setup. Any other existing branch is refused, now saying that
    `placido run` resumes and `placido close` ends an issue. A failed setup's
    message says to run `placido start` again.
  - `placido status` shows only the newest run of a worktree.
  433 tests pass.
- With the bit fixed, the user's second `placido start 331` passed setup in
  46 seconds. The user then chose to start the issue over from a blank slate.
- The user restarted #331 from a blank slate. On the new worktree placido
  answered Claude Code's folder-trust question itself (`agent.trusted`), the
  first time live. The interview took 37 minutes, about 7.5 of reading before
  the first question, and sealed 4 slices: `-w` rather than the issue's
  `-s -w` (the symbol table keeps `govulncheck -mode=binary` precise; `-s`
  made it report GO-2026-5932 falsely), the setup tool moved to `-w` too, a
  `TZ` defect it found (the embedded zone database unreachable, so the images
  needed Alpine's `tzdata`), a new architecture rule against test code in
  shipped binaries, and a CI smoke run of the images. Its probes take 301 MB
  beside the agreement.
- Slices 2 and 4 change only builds, Dockerfiles, and CI, yet must kill 2
  mutations each. Not changed ahead of a real failure; watched in this run.
- Found: the interview drafted a follow-up (`followup-remove-chi.md`, chi's
  middleware linking `net/http/pprof` and `expvar` into both servers), but
  delivery files only the review's follow-ups, so the draft would have been
  dropped silently. The user's choice: when the pull request is opened,
  placido posts every `followup-*.md` beside the agreement, in full, as one
  comment on the run's issue (`issue.commented`, once per run, headings
  demoted a level), and files none of them; the user files them by hand. The
  spec skill now names the draft format.
- The user then extended it to the review's follow-ups, reversing step 13's
  filing: placido files no issues at all. The interview's drafts and the
  review's follow-ups the user did not fold in go, each in full with why it
  was left out, into one comment on the run's issue once the pull request
  exists (`followups.posted`, once per run; on the pull request for a local
  issue file), and the pull request's summary links to it. The user files
  what they want. The running `placido run` loaded its code before these
  changes, so it will still file the review's follow-ups as issues and not
  post the drafts.
- A rename in this change broke `summary.py` for about a minute in the live
  checkout, which `~/.local/bin/placido` runs. The user's running process had
  loaded its modules already, but any new command would have failed. From
  now on, placido changes during a live run are made in a separate worktree
  and merged only with the suite passing.
- `placido run` on #331: slice 1 (the `TZ` fix) committed after 13 minutes
  (181d1785), its four gates passing, after one nudge. The cause showed in the
  transcript: Claude Code ran the gates as a background command and ended its
  turn to wait, so Herdr reported it idle and placido read "stopped without a
  result". Claude Code wakes the agent itself when such work finishes.
- Slice 2's first attempt did the same with a long build pass: nudged, it
  answered "I'm still working" and ended its turn again, so the step ended
  `no-result` (attempt 1 of 3). Its `/exit` then met Claude Code's "Background
  work is running: Exit and stop tasks / Move to background / Stay", which
  placido did not know, so the tab was kept. A second attempt finished its
  work (4 mutations killed) while the user stopped the run with Ctrl+C; a
  second Ctrl+C stopped the salvage of that attempt during its gates. The
  user found the experience not good, and chose to resume as if slice 1 had
  just completed, with slice 2 built afresh.
- Fixed (the user's go-ahead):
  - Placido reads Claude Code's screen for background work: a turn footer
    ("done 6:01 PM · 1 shell, 1 monitor still running") or the status bar
    ("bypass permissions on · 1 shell"). An idle agent with background work
    and no result yet is waiting, not done: placido waits for it to wake
    (`agent.background`, `agent.background_done`, the live view saying
    "waiting on its background work") for up to an hour
    (`agent.background_limit`), then nudges as before.
  - Quitting answers "Exit and stop tasks" when the selection is seen on it
    (`agent.background_stopped`), so a finished step leaves nothing running in
    the worktree and its tab closes.
  - The implement, fix, and checks skills say to run commands in the
    foreground and never end a turn while background work runs. 450 tests
    pass.
- The user resumed #331 on the fixed code, with slice 2 built afresh: slices
  2, 3, and 4 committed with no nudge or kept tab, the final gates passed, and
  codex's review found 3 blocking problems in round 1 (the new smoke script
  not executable in a checkout, the `core.fileMode = false` trap again; rule 9
  missing frameworks behind third-party dependencies; an empty `TZ` not
  selecting UTC on Windows), all fixed in one commit and verified in round 2.
  The user folded in none of the 3 follow-ups. Placido pushed once, opened
  #460 ready for review, posted the follow-ups comment on #331, and CI went
  green in 9 minutes: `run.end outcome=passed ci=green`. The first real run
  of the whole flow, issue to green pull request.
- The user found the pull request confusing: its body was the run's summary,
  "Follow-ups" three times (the review's list, the pointer to the comment,
  the slices' raw notes), and it never said what the change does. Decided,
  one question at a time:
  - The body is written for the reviewer (`placido/prbody.py`): `Closes`
    lines, a warning when CI is not green, "What this changes" (the
    agreement's Solution), "Decisions" (each agreed decision's bold lead, and
    those made during the run), "Review" (the outcome, each finding and how it
    ended, what needs the user), and one "Follow-ups" line. The run's own
    story stays in `summary.md`.
  - Follow-ups go in a comment on the pull request (over the body, collapsed,
    and over the issue), as GBD's pull requests did, each drafted in full
    with a ready `gh issue create` command whose `--body-file` is the draft's
    body, saved in the run folder.
  - One drafting step (the fix role, `skills/followups/SKILL.md`), after the
    fold-in question, drafts every review follow-up the user did not fold in:
    title, labels from the repository, why it was left out, a duplicate
    search, and a body with evidence and "Done when:". A follow-up the
    interview already drafted is written as `Duplicate of followup-….md` and
    posted once. The worktree must stay untouched. A failed step does not stop
    delivery; the follow-ups are then posted as the review wrote them.
  - At the user's suggestion, the same agent writes a short note for each
    other open issue the change affects (`notes/<number>.md`), and placido
    posts each on its issue once the pull request exists, with a link to it
    (`issue.noted`). Agents still write nothing to GitHub themselves.
  - The interview's drafts use the same format, so both kinds get a command.
  455 tests pass.
- The user merged #460, which closed #331, and closed the issue by hand with
  three commands: `placido close 331` (teardown in 11 seconds, worktree and
  workspace removed), `git pull`, and `git branch -D` on the issue's branch
  (GitHub had already deleted the remote one). At the user's request,
  `placido close` now does all three. After the worktree goes, it pulls the
  main checkout as a fast-forward, only when it is on the base branch with no
  uncommitted changes (`base.pulled` or `base.pull_skipped` with the reason),
  then deletes the issue's branch once its work is merged: its pull request
  merged on GitHub (`gh pr view`, since a squash merge hides the merge from
  git), or the branch merged into the base (`branch.deleted` with the reason).
  An unmerged branch stays (`branch.kept`), as before, so an abandoned issue
  can be picked up again. 461 tests pass.
- At the user's request, filed #331's two real follow-ups by hand: #462
  (replace chi with the standard library, the interview's draft, which the
  review's F1 duplicated) and #463 (rule 9 reads the setup wizard with the
  `production` tag its release build does not set, F2). F3 was a note for
  #396, not an issue. The user also had every local Goiabada branch but
  `main` deleted (53, two never pushed), as garbage.
- Three fixes from the first run, the user's go-ahead:
  - A resume tidying an attempt whose agent and tab were gone overwrote its
    saved `screen.txt` with "(screen unavailable)" and kept a tab that no
    longer existed, with a notification (`tab.kept … pane wK:p5 not found`).
    Now the saved screen stays and `tab.gone` is logged, with nothing kept.
  - `placido status` adds "placido close when finished" to a finished run,
    whose worktree and stack are still up: the sandbox's 01 had sat "done"
    for three hours, which read as nothing left to do.
  - Interrupted and failed attempts are set aside as git stashes, which
    belong to the whole repository; Goiabada kept one after #331 merged.
    `placido close` drops the run's own stashes when it deletes a merged
    branch; an unmerged branch keeps its stashes. 464 tests pass.
- Posted the review's F3 as a comment on #396 at the user's request: the
  images no longer need `tzdata`, one of the constraints #396 lists against a
  distroless or scratch base.
- The user started two issues at once (#439 and #463), the first live test of
  parallel issues.
- The user asked for `placido start` to say where the worktree is, or `cd`
  there. A program cannot change its parent shell's folder, so: placido prints
  the `cd` and the next command (`placido spec`, or `placido run` when the
  agreement is already sealed), and, the user's choice after checking that
  `herdr workspace focus` works, switches Herdr to the issue's workspace once
  setup has passed (`workspace.focused`), whose pane is already in the
  worktree. Not during setup, so its progress stays visible where `start`
  ran; a failed focus is only a warning. (Checking the call by hand moved the
  user's view off #439's workspace; it was put back at once.) 468 tests pass.
- Both parallel runs ended green, the first live test of parallel issues and
  of the new delivery. #439 (PR #464): one review round, no follow-ups; the
  drafting step wrote notes for #394 and #423, which placido posted. #463 (PR
  #465): its follow-up was drafted with a filing command and posted on the
  pull request.
- #463's review exposed two problems, both fixed at the user's request:
  - Goiabada's `.placido` told agents to narrow the unit tiers with `--run`,
    which `run-tests.sh` applies to the data and integration tiers only, so a
    "single test" ran a whole module tier; the implement notes' mutation
    example had the same mistake. The config now says so and gains an `exec`
    command (one quoted command in the issue's devcontainer, from
    `src/authserver`), with `go test -count=1 -run` for one unit test; checked
    on #463's stack. Pushed to Goiabada's main as a63a5a68.
  - Round 1 found a significant flaw in #463's build-script reader; the fixer
    rewrote it, all in a `_test.go` file, and placido stopped with "the fix
    changed no production code", so the rewrite went out unverified. #463's
    deliverable was test code. Following the user's principle (when in doubt,
    one more round), any committed fix or dispute on a serious finding now
    earns a verification round, whatever files it touches.
- The two notes the drafting step wrote for #394 and #423 were accurate and
  useful, but said "#439 has landed" and "Part 16 is done" while #464 was
  still open. The followups skill now says notes are posted before the merge,
  so they say what the pull request does.
- `placido review` on #463, run again for the unverified fix: round 1 found
  three more ways to fool the guard (1 blocking, 2 significant), while the
  guarded files were all correct. I proposed a redesign and asked the user to
  paste it into the fixer's tab mid-run; the fixer finished first. The user
  rightly objected: runs are unattended, and a fix that needs the user to race
  an agent is not one. The real cause was severity calibration, which drove a
  spiral (each patch to the guard's shell reader met new bypasses). The review
  skill now says a guard that only a future edit could get past is not broken
  now (significant at most), and that bypasses are one finding about the
  guard's approach, naming a better approach when there is one, never fixed one
  case per round. Design concerns about a running issue become follow-ups or
  retro material, not mid-run interventions.
- The second review of #463 passed in round 2 with all three fixes verified,
  and CI went green; the feared spiral did not come. At the user's request
  ("fix everything related in this PR"), once the run had ended, the drafted
  follow-up was done on #465's branch itself (8305dfc5, after merging main):
  `run-tests.sh` now applies `--run` to the module and setup legs too, and a
  run whose pattern matched no test fails instead of passing empty; the
  `.placido` config and implement notes say `--run` narrows every tier
  again. Checked on #463's stack: one core test ran only its 75 cases, a
  pattern matching nothing exited 1, lint with `--run` still passed.
- The user merged #465 and closed #463 with the new `placido close`, its first
  live run: teardown in 10 seconds, worktree and workspace removed, `main`
  pulled (f1f890b → cb98676), and the branch deleted because #465 was merged.
  A first attempt was refused, correctly, because it ran from a pane of #463's
  own workspace that had been `cd`'d to the main checkout.
- The user found Herdr's sidebar confusing. Herdr labels a workspace after its
  pane's folder, so the repository's workspace, which groups the issues, read
  `placido-440-…` once its pane sat in #440's worktree, and #404 looked nested
  inside #440. Clicking "new" also opened a shell in the focused issue's
  worktree (`terminal.new_cwd = "follow"`), labeled like the issue. A `.placido`
  setting cannot change "new", which is Herdr's alone and configurable only
  globally; the user declined the global setting. Decided: `placido start`
  renames the repository's workspace to the repository's name when its label
  differs (`workspace.renamed`); a renamed label stays put. Checked against the
  user's Herdr. 471 tests pass.
- #404's workspace was closed from the sidebar (probably mistaken for the
  extra "new" workspace, both labeled after #404's folder), leaving its
  worktree, branch, and stack in place; `placido spec` then failed with
  "herdr tab create: workspace wT not found". Now every command that runs a
  step first checks the run's workspace and, when Herdr no longer knows it,
  reopens the worktree with `herdr worktree open` and records the new
  workspace as a `worktree.created` with `reopened` and `was`. 474 tests pass.
- That change reached the user's real Herdr from the test suite: a CLI test of
  `placido spec`'s refusal now passed through the workspace check, Herdr did
  not know the test's made-up workspace, and placido "reopened" the test's
  temporary repository, once per suite run: four `02-x` workspaces on deleted
  folders appeared in the user's sidebar (closed by hand). Fixed twice over:
  the check runs only when a step is about to open its tab, after a command's
  own refusals; and the suite now refuses every real herdr call
  (`tests/__init__.py` replaces the runner a `Herdr()` gets by default), so a
  test that reaches Herdr fails instead of acting on the user's session.
  475 tests pass.
- Live: `placido spec` on #404 reopened its closed workspace. Herdr's
  `worktree open` adopted the user's extra shell workspace already sitting in
  that folder (wS) as #404's linked workspace, so the sidebar settled as one
  repository with two issues. The last confusing line was the repository
  header's subtitle, the git branch of its pane's folder (#440's), which reads
  `main` again once that pane is back in the main checkout.
