---
name: placido-implement
description: Build one slice of a sealed agreement, tests at the agreed seams first, leaving the change uncommitted with a result that carries its commit message and evidence. Use when placido implements a slice, or when asked to implement one slice of a placido agreement.
---

# Slice to change

You build **one slice** of a sealed agreement in the worktree. The agreement is the
contract: what it decided is settled, and what it did not decide is not yours to
decide. Your prompt names the agreement, the slice, the worktree, whether tests come
first, the commands you may run, and the slice's gates. Without a prompt, ask the user
for them.

## 1. Read

Read the agreement **in full**: the decisions, the seams, the testing notes, and every
slice, so you know what this slice must not do as well as what it must. Read the
decisions the user made during the run, if your prompt names any: each one overrides
the agreement where they differ. Then read the
project's knowledge where it exists: `AGENTS.md` or `CLAUDE.md`, `GLOSSARY.md` (use its
words in names and tests), the ADRs for the area you touch, and the role notes your
prompt names.

**Done when** you can say what this slice delivers, which seams prove it, and what the
later slices will add, so you do not build them now.

## 2. Detail the slice

The agreement keeps slices coarse on purpose; you detail this one against the code as
it is now. Find where the change lands, what calls it, and the existing tests at the
agreed seams to follow. Facts are yours to find: read the code and the history
(`git log -S`) rather than guessing.

**Done when** you know every place the change touches.

## 3. Tests first, at the seams

When your prompt says tests come first, write the slice's tests **before** the code:

- **Only at the agreed seams,** through public interfaces, never against internals.
- **Expected values from an independent source:** a literal from the agreement, a
  worked example. An assertion that recomputes the expected value the way the code
  does passes by construction and proves nothing.
- **No mocks of the project's own internals;** mock only what crosses a boundary
  you do not own.

Run them and confirm they **fail for the right reason**: an assertion about the
missing behavior, not an import error or a typo. Keep that output for your evidence.
Tests that pass before the code exists are testing nothing; fix them first.

When tests do not come first, write them after the code, to the same rules.

**Done when** the slice's tests exist and, when tests come first, fail for the
right reason.

## 4. Build

Write the code until the slice's tests pass. Run narrow tests often, and before you
finish, run every gate your prompt lists for this slice: placido runs them again
after you, and the slice is committed only when they pass.

- **Stay in the slice.** Do not build later slices, refactor beyond what this slice
  needs, or fix unrelated defects; note anything worth doing under Follow-ups.
- **The agreement decides.** When the code shows the agreement cannot work as
  written, or needs a decision it does not contain, **stop**: do not choose for the
  user. Write a blocked result (below) instead.
- **Run commands in the foreground and wait for them,** however long they take. Do not
  end your turn while a background command or monitor is running: placido reads the
  end of your turn as the end of your work.
- **Never commit,** amend, push, or switch branches. Placido commits the slice after
  checking it, using the message in your result.

**Done when** the tests pass and every gate passes.

## 5. Mutations

Tests that went red before green prove they fail while the code is missing; they do
not prove they catch code that is **wrong**. A mutation checks that: break one piece
of the new code on purpose and see whether a test notices.

```
placido mutate <file> --replace "<exact text>" --with "<broken text>" -- <narrow test command>
```

The text must match once in the file. The tool runs the tests, restores the file
exactly, and records the outcome for placido.

- **Aim at the riskiest behavior of this slice:** conditions, boundaries, error paths,
  security checks, and the behaviors the agreement's decisions are about. A mutation
  of trivial code tells nothing.
- **Make each mutation a plausible bug:** flip a comparison, drop a branch, change a
  boundary or a constant, skip a check.
- **Killed** means a test failed: good. **Survived** means no test noticed: add or
  strengthen a test and mutate again, or, when the mutation does not change behavior,
  say why in your evidence.
- Run them with **narrow tests**, the ones that should catch it, not the whole suite.

When your prompt asks for no mutations, skip this part.

**Done when** your prompt's number of mutations are killed and every survivor is
fixed or explained.

## 6. Result

Write the result file your prompt names, in this shape:

```markdown
<commit subject: imperative, at most 72 characters>

<commit body: what the slice changes and why, in a few lines of prose>

## Evidence
- **Red:** the failing test run before the code, trimmed to the failures.
- **Green:** the passing run of the slice's gates.
- **Mutations:** each one, what it broke, and killed or survived; why any survivor
  does not change behavior.

## Follow-ups
- Anything worth doing that this slice did not, or "None".
```

Everything above `## Evidence` becomes the commit message, so it describes the change,
not your process. If tests did not come first, the Red line says so.

When you cannot go on because the agreement cannot be followed, or only the user
can decide, ask instead of guessing. Write the result as:

```markdown
Blocked: <the question the user must answer, in one line>

<what you found, with the evidence, and the options you see, with your recommendation>
```

Placido brings the user to your tab and keeps your question. **Then wait:** the user
answers here, in this conversation, and may ask you things first. Once they have
decided:

1. Append the decision to the decisions file your prompt names, as the next entry:
   `## D<n> · <your step folder's name> · <date>`, then `**Question:**`,
   `**Decision:**` (the user's answer, in their words where you can), and `**Why:**`.
2. Finish the slice under the decision, and write your result again, as normal.

A decision overrides the agreement where they differ; never edit the agreement.
Leave your changes in place throughout. Placido checks the result, runs the gates,
and commits.

**Done when** the result file is written.
