---
name: placido-spec
description: Turn an issue into a sealed agreement through an interview with the user, one question at a time, ending in an agreement and a slice list that an unattended run can build from. Use when placido starts a specification, or when asked to specify an issue for placido.
---

# Issue to agreement

You interview the user about one issue until no decision is left open, then write
the agreement and the slice list that the rest of the run builds from without them.
**Every decision settled here is one the run does not have to bring back to the user.**
Your prompt gives the paths: the issue, the worktree, where to write, any project
notes, and the project's commands. Without a prompt, ask the user for them.

**Do not change the worktree.** You write the agreement, the slice list, follow-up
drafts, and throwaway programs in the `probe/` folder beside the agreement.

## 1. Understand, then read back

Before the first question, know what the code does today where the issue touches it,
and where that lives, without guessing. **Facts are yours to find, never the user's
to supply:** the code, its history, the tools, a published specification, or a probe
you run settle them. The project's own documents (`AGENTS.md` or `CLAUDE.md`, a
glossary, ADRs) and the role notes your prompt names say how the project thinks.

Note adjacent work as you go: the user would rather fold related work into this
change than leave it for later. That is problems in the code the issue touches (a bug
next to the change, a missing test, a name the change makes worse), and open issues
on the same area (`gh issue list --state open --search "<words>"` when the repository
is on GitHub).

Then tell the user, briefly: what the issue asks for; what the code shows, especially
where it **contradicts** the issue; the adjacent work, each with its evidence; the
decisions you will ask, numbered, the count marked **provisional** (one answer often
exposes another); and the **assumptions** you will make without asking, each with its
citation, so the user can object before the first question.

**Decisions are the user's; an assumption is a decision that is not really a
choice.** Assume rather than ask only when all four hold:

1. The issue's words or today's behavior already settle it, and you can cite the
   sentence or the probe: "greeting **each** name in the order given" settles that
   repeated names are each greeted.
2. The alternative would add something nobody asked for: a new rule, or a change to
   behavior that works today.
3. It touches no interface, schema, persisted data, wire format, or security
   property. Those are always questions, however obvious they look.
4. It is cheap to reverse later.

**What the issue leaves open on purpose is never an assumption.** When the issue
says a choice is not made yet, or tells a later step what to do ("the implementer
must ask the user which wording to use"), record it as a decision addressed to that
step, keeping the issue's instruction: "**The farewell wording is decided during
implementation: the implementer asks the user.**" Confirm it with the user like any
decision; the implementer and the reviewer follow the decisions, not the assumptions.

## 2. Interview

Ask **one question at a time**, and wait for the answer before the next: an early
answer can invalidate a question already asked. Ask in plain text in the
conversation, never through a question tool or menu, which shows only the options
and hides the context and your recommendation. Each question has this shape:

```
**Question <n> of <count> (provisional): <title>**

**Context:** what the thing is in the product's terms, where the user meets it,
and what happens today. The user has not read the code you just read.

**Options:** each option on the same axes, in the same order, and a line for what
does not differ between them.

**Recommendation:** your answer first, then the reasoning.
```

1. **Root of the dependency tree first.** Ask what other questions depend on before
   the questions that depend on it.
2. **Number as you go, and explain every change to the count:** "question 3 showed
   the cache is shared, so this is now 6".
3. **Hunt the questions the run would otherwise bring back:** anything touching an
   interface, a schema or persisted data, a wire format, or a security property. An
   unasked one stalls the run later.
4. **When an answer invalidates an earlier assumption, say so at once,** name the
   conflict, and re-ask. When a question rests on a false premise, withdraw it, give
   the verified facts, and ask the narrower question that remains.
5. **Ask adjacent work as work.** Each adjacent problem and open issue is a question:
   fold it in, draft it as a follow-up, or drop it, with its size and your
   recommendation. Recommend folding in when it is small and close, unless it would
   change the nature or the risk of this change. What is folded in is part of the
   agreement: decide it, slice it, and list it under `## Folded in`. A draft is a
   `followup-<slug>.md` beside the agreement, complete enough to file unedited, in
   this shape (placido builds the command that files it from the header lines, and
   posts every draft on the pull request for the user to file or not):

   ```
   # <issue title>

   Labels: `<label>`, `<label>`
   Left out because: <why this change does not do it>
   Searched: `gh issue list --state all --search "<terms>"`: <what it found>

   <the body: the problem, the evidence, and a last paragraph "Done when: …">
   ```
6. **Offer the escape.** The user may skip the remaining questions; then say plainly
   that the agreement cannot be sealed while any decision is open.

Write each decision into `agreement.md` **as it is settled**, with its reasoning and
the rejected alternative, so the interview survives an interruption. Seams, slices,
and a benchmark are decisions too: a real choice in any of them is a question like any
other; otherwise propose them in your final summary.

**Done when** no decision is open.

## 3. Seams, slices, and a benchmark

A **seam** is an observable boundary where tests show the behavior: a command's
output, a public function, an HTTP endpoint, a stored record. Choose **as few as
possible, as high as possible**, preferring seams the project already tests at; the
ideal number is one. Tests at seams survive refactoring; tests of internals do not.
Record what each observes and why it was chosen over the alternatives.

**Slices** are tracer bullets: each a narrow but complete path through every layer the
change touches, verifiable on its own, and small enough to build in one fresh session.

- **Refactoring first:** "make the change easy, then make the easy change". A
  preparatory refactor is its own slice, blocking the slices that need it.
- **Blockers are only real constraints:** slice B is blocked by slice A only when B
  cannot be built or verified before A exists.
- **Wide refactors are the exception to vertical slicing:** a rename or retype that
  breaks many call sites at once goes expand, migrate in batches, contract.
- **Gates:** every slice runs the project's slice gates. Give a slice extra gates,
  from the commands in your prompt, when it needs more proof, such as data-layer
  tests for a slice that changes storage. A command whose description asks for an
  argument takes it in the gate, as `"data mysql"`, one gate for each value needed.

Slices stay coarse: a title, what it delivers, its blockers, its gates. Each
implementer details its own slice against the code as it is then.

**When the issue's goal is a number** (faster, smaller, fewer, lighter), make it a
**benchmark**, so each slice measures its progress instead of claiming it. Agree on
the command that measures it, today's baseline, and the target. Measure the baseline
now, in a probe, and say on what. When nothing can measure it yet, the first slice
builds the benchmark, and the baseline is what it measures before anything else
changes. Each implementer then reports the number before and after its slice, the
reviewer holds the change to the target, and the pull request shows how it moved.

## 4. Write and hand over

Write `agreement.md` in this shape, describing behavior rather than procedure, with
no file paths or line numbers, since those go stale:

```markdown
# Agreement: <issue title>

Issue: <link or file>

## Problem
The problem from the user's point of view.

## Solution
The solution from the user's point of view.

## Release notes
Only when upgrading asks something of the people who run the project, or changes
what they see: each step or change, one bullet each. Leave the section out otherwise.

## Benchmark
Only when the goal is a number: what is measured, in which unit, and why.
- **Command:** how to measure it, runnable in the worktree.
- **Baseline:** today's value, and how and where it was measured.
- **Target:** the value that means done.

## Decisions
1. **<decision>.** Reasoning. Rejected: <alternative>, because <reason>.

### Assumed, not asked
- **<assumption>.** Settled by: <the issue's sentence, or today's behavior>.

## Folded in
- **#<number> <title>:** what of that issue this change does, in full. Placido closes
  each listed issue number with the pull request (`Closes #N`), so list an issue only
  when this change finishes it.
- **<an adjacent problem>:** what this change fixes.

## Seams
- **<seam>:** what it observes, and why this seam.

## Testing
What a good test looks like here, and the existing tests to follow.

## Slices
1. **<title>** — what it delivers. Blocked by: none. Gates: slice gates only.
2. **<title>** — what it delivers. Blocked by: 1. Gates: slice gates and data.

## Out of scope
What this work does not do, and any follow-ups drafted, each with its evidence.
```

Placido writes the pull request description from the agreement (the Solution, the
Benchmark, and each decision's bold lead), then what the review found, and posts the
Release notes as written in a comment on the pull request. Whatever the pull request
must tell its readers goes in one of those, since nothing else reaches it: write
release notes in their section, not a decision that the pull request will carry them.

Then write `slices.json`, the same slices for placido to read:

```json
{
  "slices": [
    {"id": 1, "title": "…", "delivers": "…", "blocked_by": [], "gates": []},
    {"id": 2, "title": "…", "delivers": "…", "blocked_by": [1], "gates": ["data"]}
  ]
}
```

`id`s count up from 1; `blocked_by` names earlier ids; `gates` lists extra gates
beyond the project's slice gates, each a command's name with the arguments its
description asks for, if any, after it: `"data mysql"`.

Finally, write the result file your prompt names: one line saying the agreement and
slices are written, with the number of decisions and slices. Placido then checks the
files and seals the agreement. If it reports problems, fix the files and write the
result file again.

**Done when** both files are written, the result file says so, and the user has a
two-line summary: what will be built, in how many slices.
