---
name: placido-retro
description: Read a finished placido run, or a project's last few runs together, and suggest improvements to the project so the same trouble does not come back, turning mistakes into automated checks and judgment calls into written standards. Suggests only; never changes the project. Use when placido runs a retro, or when asked to look back on placido runs.
---

# Run to lessons

A run is over, and the user asked what it teaches. You read what happened and
suggest changes to the **project**, so the next run goes better without anyone
remembering this one. You change nothing: the user decides what to adopt. Your
prompt names the run folder, the issue folder, the code, and where to write; or,
for a retro on the project's last few runs, each run with its issue folder.

**Done when** `retro.md` and the result file are written, and nothing in the project
was changed.

## Where the run's story is

- `summary.md`: the slices, the decisions, the review, and what went wrong.
- `events.jsonl`: failed attempts (`slice.failed`), rejected results
  (`result.rejected`, with the problems placido found), nudges, fallbacks, questions
  (`agent.question`), and how long each step took.
- `steps/`: each step's `prompt.md`, `result.md`, `gates/` logs, and `mutations.jsonl`
  (survivors especially). A transcript only when the rest does not explain it.
- Each review round's `review.md`, `findings.json`, and the fixer's `resolutions.json`.
- The issue folder: the agreement, and `decisions.md`, the questions the user had to
  answer.

Compare it with what the project already writes down for agents: its standards and
glossary, its ADRs, its linters' and type checkers' configuration,
`.placido/config.toml`, and the role notes in `.placido/notes/`.

## The lessons

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

## Several runs

Read each run as above, then look across them first: trouble that came back in two
runs or more, even in different forms, is what a change saves most, and is what one
run alone could not show. Rank it above what happened once. An earlier retro of a
run, when the prompt names one, says what was already suggested: check whether the
project took it up, and if the same trouble came back anyway, say why the
suggestion did not hold rather than make it again. Cite the run with each piece of
evidence (`867-ci-hits-docker-hub-s`, step, finding id).
Problems with placido itself (its prompts, checks, or flow) are not the project's:
list them separately so the user can report them.

## Write

`retro.md` where your prompt says:

```markdown
# Retro: <issue, or "the last N runs of <project>">

<one paragraph: how the run went (or the runs, and what came back across them), and
the one change that would help most>

## Suggestions

### 1. <the change, as an imperative>
- **Because:** what happened, with the evidence (run, step, file, finding id).
- **Where:** the file to change, or the tool's configuration.
- **Change:** the exact rule, text, or setting to add.
- **Prevents:** what the next run would do differently.

## For placido
- <a problem with placido itself, with the evidence>, or "None".
```

Order suggestions by how much trouble each would have saved. No suggestions is a
valid retro: say the run went cleanly and why. Then write the result file your prompt
names: one line, the number of suggestions.
