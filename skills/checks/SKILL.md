---
name: placido-checks
description: Fix the cause of failing checks on a finished placido change, such as the project's final gates or its CI on the pull request, and leave the fix uncommitted with a result that carries the commit message. Use when placido hands over failing check output.
---

# Failing checks to a fix

The change is built and reviewed, and a check on the whole of it failed: the
project's final gates, or its CI. Your prompt names what failed, where its output
is, the agreement and the decisions, and which checks placido runs after you.

## 1. Read

Read the failing output first, all of it: the first error is usually the cause and
the rest its echo. Then read the code it points at, and the agreement and decisions
where the failure touches what they decided.

## 2. Fix the cause

- **Fix what makes the check fail, and only that.** No refactoring, no new features,
  nothing the agreement did not decide.
- **Never make a check pass by weakening it:** no skipped or deleted tests, no
  loosened assertions, no lowered thresholds, no disabled lint rules, no changed CI
  configuration. If the check itself is wrong, stop and ask (below).
- **Reproduce locally** with the project's commands where you can, and run them
  again after the fix. A CI-only failure, such as another database or operating
  system, may not reproduce; reason from the output, and say so in the evidence.
- **Never commit.** Placido runs the checks again and commits.

When the failure is not the change's fault (a flaky test, an outage, a broken CI
setup) or fixing it needs the user's decision, write the result as
`Blocked: <the question, in one line>` followed by the evidence, and wait in this
tab: placido brings the user here. Record their decision in the decisions file your
prompt names, as the implement skill shows, then go on.

## 3. Write

Write the result file your prompt names. Its first line is a commit subject of at
most 72 characters, the lines after it up to `## Evidence` are the commit body:

```markdown
Fix <what failed>: <the gist>

<the cause, and what the fix changes>

## Evidence
- The failing output's key line, and the same check passing after the fix.
```

**Done when** the checks your prompt names pass locally, or you have said why they
cannot run here, and the result file is written.
