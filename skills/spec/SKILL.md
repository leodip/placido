---
name: placido-spec
description: Turn an issue into a sealed agreement through an interview with the user, one question at a time, ending in an agreement and a slice list that an unattended run can build from. Use when placido starts a specification, or when asked to specify an issue for placido.
---

# Issue to agreement

You interview the user about one issue until no decision is left open, then write
the agreement and the slice list that the rest of the run builds from without them.
**Every decision settled here is one the run does not have to bring back to the user.**

Your prompt gives you the paths: the issue, the worktree, where to write, any
project notes, and the project's commands. Without a prompt, ask the user for them.

## 1. Gather

Read the issue in full, including its comments. Then read the project's knowledge,
where it exists: `AGENTS.md` or `CLAUDE.md`, `GLOSSARY.md`, the ADRs in `docs/adr/`
for the area the issue touches, and the role notes your prompt names.

Explore the code the issue touches until you can say what it does today. **Facts are
yours to find, never the user's to supply:** anything the code, the history
(`git log -S`, `git blame`), the tools, or a published specification can answer, you
look up. "Why is it like that" is usually in a commit message. When a claim is
cheap to settle by running something, run it; keep throwaway programs in the
`probe/` folder next to the agreement, never in the worktree.

**Do not change the worktree.** Specifying writes nothing but the agreement, the
slice list, and probes.

**Look for adjacent work while you read.** The user would rather fold related work
into this change than leave it for later, so note two kinds:

- **Problems in the code the issue touches:** a bug next to the change, a missing
  test, a confusing name the change will make worse.
- **Open issues on the same area:** when the repository is on GitHub, search them
  (`gh issue list --state open --search "<words>"`) and read the likely ones.

**Done when** you can describe the current behavior the issue is about, and where
it lives, without guessing, and you have your list of adjacent work.

## 2. Read back

Tell the user, briefly: what the issue asks for; what the code shows, especially
where it **contradicts** the issue; the adjacent work you found, each with its
evidence; the decisions you are about to ask, numbered,
with the count marked **provisional**; and the **assumptions** you will make
without asking, each with its citation, so the user can object before the first
question. Answering one decision routinely exposes another, and saying so now makes
a later addition read as the process working.

**Decisions are the user's; an assumption is a decision that is not really a
choice.** You may assume rather than ask only when all four hold, and ask otherwise:

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

**Done when** the user has seen the read-back.

## 3. Interview

Ask **one question at a time**, and wait for the answer before the next. Batching
hides dependencies: an early answer can invalidate a question already asked.

Ask in plain text in the conversation, never through a question tool or menu: the
user needs the context and your recommendation in front of them, and a menu shows
only the options.

Each question has this shape:

```
**Question <n> of <count> (provisional): <title>**

**Context:** what the thing is in the product's terms, where the user meets it,
and what happens today. The user has not read the code you just read.

**Options:** each option on the same axes, in the same order, and a line for what
does not differ between them.

**Recommendation:** your answer first, then the reasoning.
```

Rules, in order:

1. **Root of the dependency tree first.** Ask what other questions depend on before
   the questions that depend on it.
2. **Number as you go, and explain every change to the count:** "question 3 showed
   the cache is shared, so this is now 6".
3. **Hunt the questions the run would otherwise bring back.** Walk the design for
   anything touching an interface, a schema or persisted data, a wire format, or a
   security property, and ask it now. An unasked one stalls the run later.
4. **When an answer invalidates an earlier assumption, say so at once:** name the
   conflict and re-ask.
5. **When a question rests on a false premise, withdraw it,** give the verified
   facts, and ask the narrower question that remains.
6. **Ask adjacent work as work.** Each adjacent problem and open issue you found is a
   question: fold it in, draft it as a follow-up, or drop it, with its size and your
   recommendation. The user prefers folding in when it is small and close; recommend
   that unless it would change the nature or the risk of this change. What is folded
   in is part of the agreement: decide it, slice it, and list it under `## Folded in`.
7. **Offer the escape.** The user may skip the remaining questions; then say plainly
   that the agreement cannot be sealed while any decision is open.

Write each decision into `agreement.md` **as it is settled**, with its reasoning and
the rejected alternative, so the interview survives an interruption.

**Seams** and **slices** are decisions too. When there is a real choice in either,
it is a question like any other. Otherwise propose them in your final summary.

**Done when** no decision is open.

## 4. Seams

A **seam** is an observable boundary where tests show the behavior: a command's
output, a public function, an HTTP endpoint, a stored record. Choose **as few as
possible, as high as possible**, preferring seams the project already tests at; the
ideal number is one. Tests at seams survive refactoring; tests of internals do not.

For each seam, record what it observes and why it was chosen over the alternatives.

## 5. Slices

Split the work into **slices**: tracer bullets, each a narrow but complete path
through every layer the change touches, verifiable on its own, and small enough to
build in one fresh session.

- **Refactoring first:** "make the change easy, then make the easy change". A
  preparatory refactor is its own slice, blocking the slices that need it.
- **Blockers are only real constraints:** slice B is blocked by slice A only when B
  cannot be built or verified before A exists.
- **Wide refactors are the exception to vertical slicing:** a rename or retype that
  breaks many call sites at once goes expand, migrate in batches, contract, each its
  own slice.
- **Gates:** every slice runs the project's slice gates. Give a slice extra gates,
  from the commands in your prompt, when it needs more proof, such as data-layer
  tests for a slice that changes storage.

Slices stay coarse: a title, what it delivers, its blockers, its gates. Each
implementer details its own slice against the code as it is then.

## 6. Write and hand over

Write `agreement.md` in this shape, describing behavior rather than procedure, with
no file paths or line numbers, since those go stale:

```markdown
# Agreement: <issue title>

Issue: <link or file>

## Problem
The problem from the user's point of view.

## Solution
The solution from the user's point of view.

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

Then write `slices.json`, the same slices for placido to read:

```json
{
  "slices": [
    {"id": 1, "title": "…", "delivers": "…", "blocked_by": [], "gates": []},
    {"id": 2, "title": "…", "delivers": "…", "blocked_by": [1], "gates": ["data"]}
  ]
}
```

`id`s count up from 1; `blocked_by` names earlier ids; `gates` lists extra gates by
command name, beyond the project's slice gates.

Finally, write the result file your prompt names: one line saying the agreement and
slices are written, with the number of decisions and slices. Placido then checks the
files and seals the agreement. If it reports problems, fix the files and write the
result file again.

**Done when** both files are written, the result file says so, and the user has a
two-line summary: what will be built, in how many slices.
