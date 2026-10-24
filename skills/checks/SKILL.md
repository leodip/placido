---
name: placido-checks
description: Fix the cause of failing checks on a finished placido change, such as the project's final or last gates or its CI on the pull request, and leave the fix uncommitted with a result that carries the commit message. Use when placido hands over failing check output.
---

# Failing checks to a fix

The change is built, and a check on the whole of it failed: the project's final
gates, before the review; its last gates, after it; or its CI. Your prompt names what
failed, where its output is, the agreement and the decisions, the project notes
(which say how this project's checks run), and which checks placido runs after you.
Find the cause, and fix it if it is the change's.

**Done when** the checks your prompt names pass locally, or you have said why they
cannot run here, and the result file is written; or nothing in the change causes the
failure, the worktree is as you found it, and the result says so.

## Rules

- **Fix what makes the check fail, and only that.** No refactoring, no new features,
  nothing the agreement did not decide.
- **Never make a check pass by weakening it:** no skipped or deleted tests, no
  loosened assertions, no lowered thresholds, no disabled lint rules, no changed CI
  configuration. If the check itself is wrong, ask (below).
- **Reproduce locally** with the project's commands where you can, and run them
  again after the fix. A CI-only failure, such as another database or operating
  system, may not reproduce; reason from the output, and say so in the evidence.
- **Never commit.** Placido runs the checks again and commits.
- **Run commands in the foreground and wait for them,** however long they take, and
  do not end your turn while a background command or monitor runs: placido reads the
  end of your turn as the end of your work.

When nothing in the change causes the failure (a test the base branch fails the same
way, a flaky test, an outage, a broken CI setup), change nothing and write the result
as `No code changes` (below): placido leaves the check failing, and the pull request
says why. Do not ask the user about it. When fixing the failure needs the user's
decision, write the result as `Blocked: <the question, in one line>` followed by the
evidence, and wait in this tab: placido brings the user here. Record their decision
in the decisions file your prompt names, as the implement skill shows, then go on; a
decision to leave the failure as it is ends in `No code changes` too.

## Write

The result file your prompt names. Its first line is a commit subject of at most 72
characters, and the lines after it up to `## Evidence` are the commit body:

```markdown
Fix <what failed>: <the gist>

<the cause, and what the fix changes>

## Evidence
- The failing output's key line, and the same check passing after the fix.
```

With nothing changed, the first line is `No code changes` instead, then why, in a
sentence or two the pull request can quote:

```markdown
No code changes

<what causes the failure, and why it is not this change's>

## Evidence
- The failing output's key line, and what places its cause outside the change, such
  as the same failure on the base branch.
```
