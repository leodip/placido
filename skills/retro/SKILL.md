---
name: placido-retro
description: Read a finished placido run and suggest improvements to the project so the same trouble does not come back, turning mistakes into automated checks and judgment calls into written standards. Suggests only; never changes the project. Use when placido runs a retro, or when asked to look back on a placido run.
---

# Run to lessons

A run is over, and the user asked what it teaches. You read what happened and
suggest changes to the **project**, so the next run goes better without anyone
remembering this one. You change nothing: the user decides what to adopt. Your
prompt names the run folder, the issue folder, the code, and where to write.

## 1. Read

- `summary.md` first: the slices, the decisions, the review, and what went wrong
  along the way.
- The event log, `events.jsonl`: failed attempts (`slice.failed`), rejected results
  (`result.rejected`, with the problems placido found), nudges, fallbacks, questions
  (`agent.question`), and how long each step took.
- The steps that went wrong, in `steps/`: their `prompt.md`, `result.md`, `gates/`
  logs, and `mutations.jsonl` (survivors especially). Read the transcript only when
  the rest does not explain what happened.
- Every review round: `review.md`, `findings.json`, and the fixer's
  `resolutions.json`, including disputes and how they were judged.
- The issue folder: the agreement, and `decisions.md`, the questions the user had
  to answer.
- The project's standing knowledge: `AGENTS.md` or `CLAUDE.md`, `CODING_STANDARDS.md`,
  `CONTRIBUTING.md`, `GLOSSARY.md`, the ADRs, the linters' and type checkers'
  configuration, `.placido/config.toml`, and the role notes in `.placido/notes/`.

## 2. Find the lessons

Look for trouble that a change to the project would prevent, cheapest fix first:

1. **A mistake caught late that a tool could catch early.** A review finding, a
   rejected result, or a failing gate that a lint rule, a type check, a test, or a
   gate would have stopped at once. Suggest the exact rule or check.
2. **A judgment call made more than once, or made differently by different agents.**
   Write it down: a coding standard, a glossary entry, or an ADR, with the text.
3. **A question the user had to answer** that the project's documents could have
   answered. Add the answer where agents read it.
4. **An agent that went the wrong way** (nudged, failed attempts, wrong seam,
   a slice too big for one session): a role note, an `AGENTS.md` line, or a
   `.placido/config.toml` change, such as a gate or a command's `about`.

Skip what went fine, one-off accidents no rule would prevent, and anything already
written down that an agent simply missed; for those, say so in one line at most.

Problems with placido itself (its prompts, checks, or flow) are not the project's:
list them separately so the user can report them.

## 3. Write

Write `retro.md` where your prompt says:

```markdown
# Retro: <issue>

<one paragraph: how the run went, and the one change that would help most>

## Suggestions

### 1. <the change, as an imperative>
- **Because:** what happened, with the evidence (step, file, finding id).
- **Where:** the file to change, or the tool's configuration.
- **Change:** the exact rule, text, or setting to add.
- **Prevents:** what the next run would do differently.

## For placido
- <a problem with placido itself, with the evidence>, or "None".
```

Order suggestions by how much trouble each would have saved. No suggestions is a
valid retro: say the run went cleanly and why.

Then write the result file your prompt names: one line, the number of suggestions.

**Done when** `retro.md` and the result file are written, and nothing in the
project was changed.
