---
name: placido-fix
description: Answer every finding of a review round on a placido change, fixing, disputing, or deferring each, and leave the fixes uncommitted with a result that carries the commit message. Use when placido fixes review findings, or when asked to fix the findings of a placido review.
---

# Findings to fixes

A reviewer has reviewed the change, and you answer **every** finding of the round.
You are a fresh session: the agreement, the findings, and the code are all you need.
Your prompt names the agreement, the review (`review.md` and `findings.json`), the
worktree, the commands and gates, and where to write.

## 1. Read

Read the agreement in full, the decisions the user made during the run if your
prompt names any (each overrides the agreement where they differ), then the review.
The agreement and the decisions are the contract: a finding asking for something they
decided against is a dispute, not a fix.

## 2. Answer each finding

For each finding, exactly one answer:

- **fixed:** you changed the code so the finding no longer holds, with a test that
  would catch it coming back where the finding is about behavior.
- **disputed:** the finding is wrong; say why, with evidence: the agreement's
  decision, a test run, the code. The reviewer judges disputes in the next round.
- **deferred:** out of this change's scope (the agreement excluded it, or it is
  separate work), and not blocking. Say why it is out of scope; "too much work" is
  not a reason, since the user wants in-scope work done in this change. It becomes a
  follow-up, so describe it fully enough to file.
- **escalated:** only the user can settle it, and they chose to leave it for the
  pull request (see "Asking the user" below).

Rules:

- **Blocking findings are fixed, disputed, or escalated, never deferred.**
- **A blocking or significant security finding is fixed or escalated, never
  disputed or deferred.** The run does not settle security on its own judgment.
- **Fix what the finding says, and only that.** No unrelated refactoring, and no
  changes to what the agreement decided.
- **Tests first where a finding is about behavior:** a test that fails before the
  fix and passes after.
- **Never commit.** Placido runs the gates and commits.

**Done when** every finding has an answer and the gates your prompt lists pass.

### Asking the user

When a finding needs the user's decision, such as a fix the agreement rules out, or
a serious security finding you cannot fix, ask instead of escalating blindly. Write
your result file as:

```markdown
Blocked: <the decision the user must make, in one line>

<the finding, what you found, the options you see, and your recommendation>
```

Placido brings the user to your tab. **Then wait:** the user answers here and may ask
you things first. Once they have decided, append the decision to the decisions file
your prompt names (`## D<n> · <your step folder's name> · <date>`, then
`**Question:**`, `**Decision:**`, `**Why:**`), act on it, and carry on: answer the
finding as fixed, disputed, or deferred under the decision, or as escalated if the
user wants it left for the pull request.

## 3. Write

Write `resolutions.json` where your prompt says:

```json
{
  "resolutions": [
    {"id": "R1-1", "status": "fixed", "note": "what changed, and the test that covers it"},
    {"id": "R1-2", "status": "disputed", "note": "why the finding does not hold, with evidence"}
  ]
}
```

Then write the result file your prompt names. When you changed code, its first line
is a commit subject of at most 72 characters and the lines after it, up to
`## Evidence`, are the commit body, as in a slice:

```markdown
Fix review round 1: <the gist>

<what the fixes change and why>

## Evidence
- The gates' passing run, and each new test failing before its fix.
```

When you changed no code, because every finding is disputed, deferred, or
escalated, the first line is `No code changes` instead.

**Done when** `resolutions.json` answers every finding and the result file is written.
