# Import of past work — feature description

Requirements for a person deciding whether these are the right ones. Technical
design is a later document, `build-plan-import-phase-1.md`, kept local and not
in the repository.

## 1. Purpose

wscontext is often set up in a repository that already has history. Before
setup, Claude Code sessions ran, commits were made and design documents were
written. Today wscontext sees none of this. `ctx find` and `ctx why` start
empty, and only the code graph reads the old commits. With this stage, the
operator can bring in the part of that past that is still available, after
seeing what it is.

## 2. What we are building

One command, `ctx import`, in two steps. The first step finds past work and
lists it. It writes nothing. The second step imports what the operator accepts.

There are three kinds of past work:

- **Past sessions:** Claude Code transcripts of this repository that are still
  on this machine and that wscontext has not captured.
- **Past commits:** the commits those sessions made.
- **Workspace documents:** Markdown files in the repository that record
  decisions or design.

Imported sessions and their commit links are stored and shared like live
ones. Documents stay where they are, in the repository. Their sections become
searchable.

## 3. Scope

**In.**

- Finding and importing past sessions (§ 4).
- Linking commits to imported sessions through the sessions' own `git commit`
  output (§ 5).
- Making chosen Markdown documents searchable through `ctx find` (§ 6).
- A preview, then a choice of all, part or nothing (§ 7).
- Later runs that offer only what is new (§ 8).

**Out.**

- Sessions that Claude Code has already deleted. They are gone for good.
- Sessions on another machine. Each developer imports on their own machine.
  With sync on, the imports are then shared.
- Agents other than Claude Code.
- Links guessed from time, author, branch or changed files.
- `AI-Session` trailers on past commits. Adding them would rewrite commits
  that may already be published.
- Decision records made from documents. That is stage 4.
- Documents in formats other than Markdown, and documents that are not
  tracked by Git.

**When.** After the Alpha exit and before Beta. At Beta a second developer sets
up wscontext on a shared repository that is already in progress.

## 4. Past sessions

**The operator can see:**

- In the preview, each past session found: its date, its title (the first
  prompt, as `ctx find` shows it), its number of prompts, and the commits it
  made (§ 5). When the session ran in a subfolder or in a worktree, the
  preview names that folder.
- A session is found when Claude Code still keeps its transcript on this
  machine, and the session ran in the repository, in a folder inside it, or
  in one of its current worktrees.
- When Claude Code will delete the oldest session found, from its cleanup
  period. The operator then knows how soon the oldest sessions are lost.
- A transcript that wscontext cannot read, for example from an old Claude Code
  version, is listed as unreadable with its date. It is not imported.
- After import, the session appears in `ctx sessions`, `ctx show`, `ctx find`,
  `ctx why`, `ctx impact` and the briefs, like a live session, and is marked
  as imported. Its subagents are imported with it.
- An imported session's author is the Git identity of the person who imports
  it.

**The operator can change:** nothing beyond the choice in § 7.

**Deliberately cannot:**

- See an imported session's figures that only live capture records: hook
  timings, whether search was on, and the tokens its briefs added. The
  imported session keeps what its transcript holds: prompts, replies, tool
  calls, the files it edited, and tokens where the transcript has them.
- Import a session that wscontext already holds. That includes a session
  captured live, one imported earlier, and one a teammate imported and shared
  through sync.
- Import a session that is still in use. It is offered once it has been idle
  longer than the window after which wscontext finalizes an abandoned session.
- Find a session that ran in a worktree that has since been removed, when that
  worktree was outside the repository folder. Nothing then ties the session
  to this repository.

## 5. Past commits

**The operator can see:**

- In the preview, under each session, the commits it made, with their short
  SHA and subject.
- A commit is linked to an imported session only when that session, or one of
  its subagents, ran the `git commit` whose output names the commit. An
  `--amend` later in the same session links the new commit the same way.
- When two imported sessions name the same commit, the commit is linked to the
  session whose output came first. This happens when a session was resumed
  into a new one that repeats its history.
- A count of the commits on the current branch that have no session, before
  and after the import.
- After import, a linked commit shows its session in `ctx why`, and `ctx find`
  lists it next to that session. The code graph links the session to the
  functions the commit changed.

**Deliberately cannot:**

- Link a commit by time, author, branch or changed files. A commit made by
  hand, or by a session that Claude Code has deleted, stays unlinked.
- Link a commit the repository no longer has, such as one made in another
  clone and never fetched.
- Follow a commit that was rewritten after its session ended, by a rebase or
  by a squash merge on the Git host. The link stays on the commit the session
  made, as it does for live capture.
- Change a commit that already has a link.
- Add an `AI-Session` trailer. `git log` shows no session for a past commit;
  `ctx why` does.

## 6. Workspace documents

**The operator can see:**

- In the preview, the Markdown files tracked by Git, in two groups:
  - **Suggested:** files in the docs folder, and files whose name points to
    decisions or design, such as `adr`, `architecture`, `design`, `decision`
    or `rfc`.
  - **Other:** every other tracked Markdown file, except `CLAUDE.md` files,
    which Claude Code already loads.
- After import, `ctx find` lists matching document sections next to sessions
  and commits. Each result shows the file, the heading, a link to the line
  where the section starts, and the matching text. `ctx find --kind` can ask
  for documents only.
- A section is the text under one heading, down to the next heading.
- Changes to a chosen document count the next time `ctx find` runs: new text
  is found, and removed text is no longer found. The version searched is the
  one in the working folder, as for the code graph.

**The operator can change:**

- Add or remove documents, by running the import again or by editing the list
  in the repository's configuration.

**Deliberately cannot:**

- Keep the choice private. The list of chosen documents is part of the
  committed configuration, so every clone searches the same documents, with
  or without sync. The operator commits it like any other change.
- Copy documents into wscontext's history. They travel with the repository.
- See document sections in the brief at session start or in the brief on the
  first prompt. Those stay about past sessions. Documents are found through
  `ctx find`.
- Turn sections into decision records that know which decision replaced
  which. That is stage 4.

## 7. Choosing and importing

**The operator can see:**

- `ctx import` shows the preview and writes nothing: nothing in wscontext's
  store, its history, its search index or the configuration. Like every `ctx`
  command, it adds one line to the `ctx` call log.
- The preview numbers each item. Sessions come first, each with its commits,
  then documents.
- After an import, a summary: sessions imported, commits linked, documents
  added. With sync on, it says that sessions and links go out with the next
  push.
- `ctx init` in a repository that has past work ends with one line saying how
  much it found and the command that shows the preview. It imports nothing.

**The operator can change:**

- Import everything offered, one kind only, chosen items, or nothing.
- Decline items. Declined items are remembered on this machine and not
  offered again. The import can list them again on request.
- Ask Claude Code to import past work. The agent shows the preview and asks
  which items to import before it imports anything.

**Deliberately cannot:**

- Import a commit link without its session. A session is imported together
  with its commit links.
- Undo a whole import in one step. An imported session is removed like any
  other, with `ctx forget`. A document is removed from the list.
- Import into a repository where wscontext is not set up. The command says to
  run `ctx init` first.

## 8. Later runs

**The operator can see:**

- A later run offers only what is new: sessions that are not imported or
  declined, and documents that are not chosen or declined.
- A run right after an import offers nothing, and says so.

## 9. Cross-cutting requirements

- Imported sessions and commit links take the same path as live capture. They
  pass through the redactor, then go into the store, the shared history and
  the search index. With sync on, they are shared by the next `git push` or
  `ctx sync`. The import itself pushes nothing.
- Document text passes through the redactor before it enters the search index.
- The import never changes Claude Code's transcripts, the repository's
  commits, or its branches.
- Imported sessions are counted apart in `ctx stats`. Adoption, turn and hook
  figures stay those of live sessions.
- The setting that turns search off for the benchmark also leaves documents
  out of search.
- An interrupted import leaves each session either fully imported or not at
  all. A rerun imports the rest.
- An import can run while Claude Code sessions and Git hooks are active in the
  same repository. Neither loses data.
- Imported sessions follow the same retention as live ones, by their own start
  date.
- Finding transcripts works on macOS, Linux and Windows.

## 10. Feasibility

- **Already proven.** On 2026-11-12 a one-off script linked 50 of 51 unlinked
  commits of this repository through the `git commit` output of stored
  sessions. The transcript adapter, the redactor and the capture worker
  already turn a transcript into a stored session. On 2026-11-18, past
  transcripts that wscontext had not captured were still on disk for two of
  the three private repositories in Alpha.
- **New but straightforward.** Finding the transcripts of a repository, its
  subfolders and its worktrees. Building a session record from the transcript
  alone. Document sections as a new kind of search result. Remembering
  declined items.
- **Known costs.** Most past commits stay unlinked: those made by hand, and
  those whose session Claude Code has already deleted. A repository set up
  after months of work gets back about its last month of sessions. Document
  sections compete with sessions in `ctx find`. How they rank against
  sessions is untested. This repository's own `docs/` is the first test.
  Transcripts from older Claude Code versions may be unreadable.
- **Blocked.** Nothing.

## 11. Open questions

None.

## 12. Beyond this stage

- Decision records made from document sections, with their status (stage 4).
- Links to commits rewritten after their session, matched by the content of
  the change.
- Importing transcripts from a folder the operator names, such as a backup or
  a copy from another machine.
- Documents in other formats.
- Document sections in the briefs.

## 13. Notes for implementation planning

Working notes, not requirements.

The one-off link-recovery script from 2026-11-12, kept with the acceptance
scripts, holds the matching rule for commits. It assumes the sessions are
already in the store and the branch is fixed, so it is a starting point, not a
library to call.

Live capture builds a session record from hook state as well as from the
transcript. Planning has to decide which fields an imported session fills from
the transcript alone. The branch and the working folder are on every
transcript line; the commit checked out at the start is not. Marking a session
as imported is a change to the record that readers of older records must
accept.

Claude Code names a transcript folder after the working folder's path. A
subfolder or a worktree gets its own transcript folder, and the folder name
loses characters, so the name alone cannot prove which repository a session
belongs to. The working folder recorded inside the transcript can. The
preview also needs Claude Code's cleanup period, which can be set in its user
settings.

Documents are a new kind of row in the search index, read from the working
folder. The index already re-indexes a whole session when its source changes.
A document can follow the same rule per file. Planning decides where the list
of chosen documents lives in the configuration, and where declined items are
remembered.

Tests and the scripted acceptance run must never read from or write to the
operator's real Claude Code folder. The place transcripts are read from needs
an override. The gate needs a throwaway repository with transcripts made
before setup. These can be recorded by headless runs, which spend API usage,
or built from the transcripts already recorded for the eval corpus.
