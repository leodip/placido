---
name: placido-followups
description: Draft each piece of work a placido change left out as a GitHub issue the user can file unedited, with evidence, labels, a duplicate search and when it is done, and a short note for each other issue the change affects. Use when placido hands over a review's follow-ups, or when asked to draft follow-ups of a placido run.
---

# Follow-ups to issue drafts, and notes for other issues

The change is built and reviewed. The review left some work out of it, and the user
chose not to fold it in. Each piece becomes a draft issue that the user reads in the
pull request and files with one command. Nothing you write is filed by you.

Your prompt lists the follow-ups, each with an id, what it is and why it was left
out, and names the agreement, the decisions, the review, the worktree, and any
drafts the interview already wrote.

## 1. Understand each one

Read what the review said about it, then the code it concerns, in the worktree.
Find the evidence a stranger needs: the files and lines, the behavior today, how to
see it. A follow-up the review only sketched needs this work most.

## 2. Look for duplicates

- **Already drafted by the interview:** when one of those drafts covers the
  follow-up, write its file as one line, `Duplicate of followup-<name>.md`, and
  nothing else.
- **Already an issue:** search with `gh issue list --state all --search "<terms>"`,
  twice with different terms. When an open issue covers it, still draft it, and say
  so in the Searched line, naming the issue; the user decides.

## 3. Write one draft per follow-up

Write `<id>.md` in the folder your prompt names, exactly in this shape:

```
# <issue title, as the repository's own issues are titled>

Labels: `<label>`, `<label>`
Left out because: <one or two sentences: why this change does not do it>
Searched: `gh issue list --state all --search "<terms>"`: <what it found>

<the issue's body>
```

- **Labels** come from `gh label list`; pick the ones the repository's similar
  issues use.
- **The body** stands alone, for someone who never saw this run: the problem, the
  evidence (file paths and lines are welcome here), and a last paragraph starting
  "Done when:" that says how to tell it is finished. Name the pull request or issue
  it came from only as context.
- **Plain and short.** A paragraph or two and the done-when; no headings needed.

## 4. Notes for other issues

When the change affects another open issue (it finishes part of it, removes one of
its constraints, or changes what it assumes), write a short comment for that issue
as `notes/<number>.md` in the same folder: what this change does that matters to
the issue, in two to five sentences, plain, with no file-by-file detail. Placido
posts each note on its issue once the pull request exists, with a link to it, and
before anyone merges it: say what the pull request does, never that it has landed
or that the issue is done. Write
none when no other issue is affected; never one for the issue this change closes.
Take the issues from the agreement, the review, and the follow-ups' searches.

## Rules

- **Change nothing in the worktree.** No code, no files, no commits. Placido checks
  the worktree is untouched.
- **File and post nothing.** No `gh issue create`, no `gh issue comment`: placido
  gives the user the commands and posts the notes.
- **Run commands in the foreground and wait for them.** Placido reads the end of
  your turn as the end of your work.

**Done when** every follow-up in your prompt has its file, and every affected issue
its note. Then write your result file: one line per follow-up, naming its draft or
the draft it duplicates, and one per note.
