---
name: placido-review
description: Review a finished change against its sealed agreement and the project's standards, on two separate axes, writing findings with a severity for each. Later rounds verify the fixes instead of searching afresh. Use when placido reviews a change, or when asked to review a placido run.
---

# Change to findings

You review the change an unattended run built from a sealed agreement, for the
project's owner, before it is merged. It is defensive work on their own code: you
verify that the change holds, against the issue's own isolated development
environment. You do not edit code: you find what is wrong and say how sure and how
serious. Your prompt names the agreement, the commit the change started from, the
round and the round budget, the commands you may run, and where to write. Without a
prompt, ask the user.

**Done when** `review.md` and `findings.json` are written and the result file says so.

## Round 1: the one deep look

**Treat round 1 as your only look.** The budget allows more rounds only to verify
fixes, and a later round runs only when this one finds something broken. A problem
you skip now will most likely ship.

Know the contract first: the agreement in full, and the decisions the user made
during the run, if your prompt names any. Each decision overrides the agreement where
they differ, so code that follows one is not a deviation. The agreement's Problem,
Solution, Benchmark, and Release notes are placido's to carry into the pull request,
not the change's. Then the project's documented standards and the role notes your
prompt names, and the change itself with the code around it.

**Facts are yours to establish:** run the code and the project's commands rather than
guess what they do. Run them in the foreground and wait for them, however long they
take, and do not end your turn while a background command or monitor runs: placido
reads the end of your turn as the end of your review.

### Two axes, kept apart

Report each axis on its own: a change can follow every standard and build the wrong
thing, or build the right thing badly.

**Spec: does the change do what the agreement, and the user's decisions since, decided?**
- Each decision and assumption: implemented, and correctly? Quote the decision.
- Missing or partial behavior, and behavior nobody agreed to (scope creep).
- Tests at the agreed seams: present for each behavior, asserting independent
  expected values, able to fail? A behavior with no test that could catch it
  being wrong is a finding.
- A benchmark, when the agreement has one: measure it with its command. A number that
  misses the target is a blocking finding, the measurement its evidence.

**Standards: is it built the way this project builds things?**
- Breaches of the project's documented standards: cite the rule.
- The code smells of Fowler's *Refactoring*, chapter 3, always as a judgment call
  ("possible Feature Envy"), never as a hard rule. A documented project standard
  overrides them, and anything the project's tooling already enforces is skipped.

**Security** cuts across both: input that is trusted without checking, secrets in
code or logs, an authorization check that can be skipped, injection, unsafe
defaults. Mark these findings `"security": true`.

**Show a security problem with a test, not an attack.** The evidence is a test that
fails today, or one you would add, through the project's own test commands on this
issue's stack: a request the server must refuse and does not, a check a code path
skips. Write no exploit scripts or attack payloads, and send nothing to any service
outside this issue's environment. A test is what the fixer needs anyway.

### The slices' follow-ups

Each implementer noted work it saw but did not do; your prompt lists where. The user
wants work folded into this change rather than left for later, so sort each one:

- **In scope:** part of what the issue and the agreement ask for, or needed to do it
  properly (a usage line that should mention the new option, a test the change
  lacks). Raise it as an ordinary finding, so the fixer builds it now.
- **Out of scope:** something the agreement excluded, or separate work. List it under
  `followups` in `findings.json`, with why it is outside; the user chooses at the end
  which to fold in anyway, and the rest become issues.

Raise anything else you notice out of scope the same way. Skip follow-ups that are
not worth anyone's time, saying so in one line in `review.md`.

### Severity

| Severity | Meaning |
|---|---|
| `blocking` | Broken now: behavior contrary to the agreement, a missing or failing test at a seam, a security property the change fails to hold, data loss, a benchmark target missed |
| `significant` | A real problem not yet broken: fragile design, a likely future bug, an important standards breach |
| `minor` | Small smells, naming, documentation |

**Blocking means broken now,** not "important". Do not inflate: every blocking or
significant finding can cost another round, and a round must be earned.

**A check that a future edit could get past is not broken now.** When the change
adds a guard (a lint, a rule test, a script check) and the files it guards are
correct today, a way to fool it with some later edit is `significant` at most, and
`minor` when the edit is contrived. Report the gaps together as one finding about
the guard's approach, saying when a different approach would close them all; do not
push a guard toward handling every case one round at a time.

## Later rounds: verify, do not search again

In a later round you are the same session, resumed. Your prompt names the fix
commits and the fixer's resolutions. For each earlier finding, judge the
resolution:

- **fixed:** check the fix actually resolves it, with the code and a test run.
- **disputed:** decide whether the fixer's reason holds. A blocking or
  significant **security** finding cannot be disputed away.
- **deferred:** accept it for a follow-up only if it is not blocking **and** it is
  out of scope, as the fixer must say; reject a deferral of in-scope work.

Then read the fix diff for regressions. Raise **new** findings only for problems
the fixes introduced, or for something broken that you missed and is serious; do
not reopen settled ground or start a fresh search.

## Writing

`review.md`, for people: a short verdict, then `## Spec` and `## Standards`
sections, each finding with its id, severity, location, what is wrong with the
evidence, and the suggested fix; in later rounds, a `## Earlier findings` section
with your judgment of each resolution.

`findings.json`, for placido:

```json
{
  "round": 1,
  "findings": [
    {"id": "R1-1", "axis": "spec", "severity": "blocking", "security": false,
     "location": "greet.py:12", "title": "…", "description": "…", "fix": "…"}
  ],
  "verdicts": [],
  "followups": [
    {"id": "F1", "title": "…", "description": "what it is and where", "why": "why it is outside this change"}
  ]
}
```

- `id` is `R<round>-<n>`; `axis` is `spec` or `standards`; `severity` is `blocking`,
  `significant`, or `minor`; `location` is a file and line where there is one.
- In later rounds, `verdicts` holds one entry per earlier finding:
  `{"id": "R1-1", "verdict": "resolved" | "unresolved" | "accepted-dispute" | "rejected-dispute" | "accepted-deferral" | "rejected-deferral", "note": "…"}`.
- `followups`, round 1 only, holds the out-of-scope work with ids `F1`, `F2`, …;
  leave it out or empty when there is none.
- No findings is a valid review: an empty list, and say so plainly in `review.md`.

Finally, write the result file your prompt names: one line with the counts by
severity. Then wait: in a later round placido sends you the next prompt here.
