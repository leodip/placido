---
name: placido-vet
description: Read a placido agreement against the code before it is sealed, and report the claims about the code that are not so and the places the agreement contradicts itself. Changes nothing. Use when placido runs its vet step at the end of an interview.
---

# Agreement against the code

An interview with the user has just produced an agreement: the decisions a run will
build on, unattended, often overnight. The interview's agent read the code to write
it, and it may have read it wrong. Once the agreement is sealed, nobody can ask the
user until a run blocks on the mistake, so a wrong claim found now saves a night.

You check the agreement; you do not redo it. The user made its choices, and they are
not yours to question: an option you would have picked differently is not a finding.
What you look for is the agreement being **wrong about the code**, or **at odds with
itself**. Your prompt names the agreement, the issue, the code, and where to write.
Change nothing in the code or the agreement.

**Done when** your findings file and the result file are written.

## What to check

1. **Every claim about today's code and behavior.** "Today X returns Y", "the setting
   is stored as Z", "the console shows W": find the code and see whether it is so.
2. **Every precedent.** A decision that does something "as", "like", or "the same way
   as" another feature does, or names an earlier issue's decision as its model: read
   how that feature actually does it, and compare it with what the decision spells
   out. A decision that copies a precedent wrongly asks two things at once, and a run
   can only build one of them.
3. **Every literal the run will build:** error codes and message keys, routes, field,
   column and setting names, status codes, defaults, file formats. Check each one
   against the conventions the code and its docs follow, and against what the
   agreement says elsewhere.
4. **The agreement against itself:** each decision against the others, and against
   the Solution, the Release notes, the Testing section, the Seams and the Slices; the
   slices in `slices.json` against the Slices section; and "Assumed, not asked"
   entries against the code they say settles them.
5. **What the slices need that nothing decided:** an interface, a schema, a wire
   format or a security property a slice must choose, which the agreement leaves
   open. A run would have to stop and ask.

Read the project's own instructions first (`AGENTS.md` or `CLAUDE.md`, its standards,
its ADRs, and the role notes your prompt names), so you judge by its conventions.

## What is not a finding

- A choice you disagree with, where the agreement is right about the code.
- Wording, style, or how the agreement is organized.
- A problem in today's code the agreement does not touch.
- Anything you could not check. Say what you could not check under `## Not checked`
  instead.

Weigh by consequence: report what would make a run build the wrong thing, or stop to
ask. When unsure whether something is wrong, read more code; report it only when you
can show it.

## Write the findings

Write the file your prompt names, in this shape:

```markdown
# Agreement check: <issue>

## Findings

### V1 <one line: what is wrong>
- **Where:** the agreement's section and decision number, quoted briefly.
- **The code:** what it actually does, with file paths and lines, quoted.
- **Why it matters:** what a run would build or stop on.
- **Correction:** what would make the agreement right. Say whether it keeps the
  decision as the user meant it (a fact corrected) or changes it (a question for the
  user), and when the agreement can be read two ways, which reading the code and the
  rest of the agreement support.

## Not checked
- What you could not verify, and why. Leave the section out when there is nothing.
```

With nothing found, `## Findings` says "None." and the check is done.

Number the findings V1, V2, … in order of consequence. Then write the result file
your prompt names: one line with the number of findings.
