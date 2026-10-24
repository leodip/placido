---
name: placido-implement
description: Build one slice of a sealed agreement, tests at the agreed seams first, leaving the change uncommitted with a result that carries its commit message and evidence. Use when placido implements a slice, or when asked to implement one slice of a placido agreement.
---

# Slice to change

You build **one slice** of a sealed agreement in the worktree, and leave it
uncommitted for placido to check and commit. The agreement is the contract: what it
decided is settled, and what it did not decide is not yours to decide. Your prompt
names the agreement, the slice, the worktree, whether tests come first, the commands
you may run, and the slice's gates. Without a prompt, ask the user for them.

**Done when** the slice's tests pass at the agreed seams, every gate your prompt lists
passes, the mutations your prompt asks for are killed, and the result file is written.

## Before you build

Know what this slice delivers, which seams prove it, and what the later slices will
add, so you do not build them now: read the agreement in full, every slice included,
and the decisions the user made during the run, if your prompt names any (each
overrides the agreement where they differ). Use the words of the project's glossary in
names and tests. The agreement keeps slices coarse on purpose: detail this one against
the code as it is now, and find every place it touches rather than guess.

## Rules

- **Stay in the slice.** Do not build later slices, refactor beyond what this slice
  needs, or fix unrelated defects; note anything worth doing under Follow-ups.
- **The agreement decides.** When the code shows it cannot work as written, or needs a
  decision it does not contain, **stop**: do not choose for the user. Write a blocked
  result (below) instead.
- **Never commit,** amend, push, or switch branches. Placido commits the slice after
  checking it, using the message in your result.
- **Run every gate your prompt lists before you finish:** placido runs them again
  after you, and commits only when they pass.
- **Run commands in the foreground and wait for them,** however long they take, and
  do not end your turn while a background command or monitor runs: placido reads the
  end of your turn as the end of your work.

## Tests

When your prompt says tests come first, write the slice's tests before the code and
see them **fail for the right reason**: an assertion about the missing behavior, not
an import error or a typo. Keep that run for your evidence. Otherwise write them
after the code. Either way:

- **Only at the agreed seams,** through public interfaces, never against internals.
- **Expected values from an independent source:** a literal from the agreement, a
  worked example. An assertion that recomputes the expected value the way the code
  does passes by construction and proves nothing.
- **No mocks of the project's own internals;** mock only what crosses a boundary
  you do not own.

## Mutations

Tests that went red before green prove they fail while the code is missing, not that
they catch code that is **wrong**. A mutation checks that: break one piece of the new
code on purpose and see whether a test notices.

```
placido mutate <file> --replace "<exact text>" --with "<broken text>" -- <narrow test command>
```

The text must match once in the file; the tool runs the tests, restores the file
exactly, and records the outcome for placido. Aim at the riskiest behavior of the
slice (conditions, boundaries, error paths, security checks, what the decisions are
about), each mutation a plausible bug, run with the narrow tests that should catch
it. **Killed** means a test failed: good. **Survived** means no test noticed: add or
strengthen a test and mutate again, or say in your evidence why the mutation does not
change behavior. When your prompt asks for no mutations, skip this.

## Benchmark

When your prompt names the agreement's benchmark and this slice can move the number,
measure it with its command before you change anything and again at the end. When it
falls short of what this slice should achieve, try another approach within the slice
rather than stopping at the first. Report both values in your evidence.

## Result

Write the result file your prompt names, in this shape:

```markdown
<commit subject: imperative, at most 72 characters>

<commit body: what the slice changes and why, in a few lines of prose>

## Evidence
- **Red:** the failing test run before the code, trimmed to the failures.
- **Green:** the passing run of the slice's gates.
- **Mutations:** each one, what it broke, and killed or survived; why any survivor
  does not change behavior.
- **Benchmark:** <before> → <after>, when your prompt names one.

## Follow-ups
- Anything worth doing that this slice did not, or "None".
```

Everything above `## Evidence` becomes the commit message, so it describes the change,
not your process. If tests did not come first, the Red line says so.

When you cannot go on because the agreement cannot be followed, or only the user can
decide, ask instead of guessing. Write the result as:

```markdown
Blocked: <the question the user must answer, in one line>

<what you found, with the evidence, and the options you see, with your recommendation>
```

Placido brings the user to your tab and keeps your question. **Then wait:** the user
answers here, in this conversation, and may ask you things first. Once they have
decided, append the decision to the decisions file your prompt names, as the next
entry: `## D<n> · <your step folder's name> · <date>`, then `**Question:**`,
`**Decision:**` (the user's answer, in their words where you can), and `**Why:**`.
Then finish the slice under the decision and write your result again, as normal. A
decision overrides the agreement where they differ; never edit the agreement.
