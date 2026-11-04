# wscontext — stage 1 feature description

Requirements for a person deciding whether these are the right ones. Technical
design is a later document:
[build-plan-wscontext-phase-1.md](build-plan-wscontext-phase-1.md).

## 1. Purpose

Every Claude Code session in a repository is recorded without anyone having to
remember to do it, every commit can be traced back to the sessions that
produced it, and the operator can see what has been collected and how sessions
behave. Stage 1 delivers the data everything later stages search, plus the
baseline numbers later stages are measured against.

## 2. What we are building

An installable Claude Code plugin with one command-line tool, `ctx`. Once the
plugin is enabled and a repository is initialized, sessions are captured and
redacted automatically, commits carry a link to their sessions, the operator
can ask why a line of code exists, inspect and remove captured sessions, share
history with other machines and teammates through Git when they choose to, and
read statistics on all of it.

## 3. Scope of stage 1

**In.**

- Installing, initialising, checking and removing wscontext.
- Automatic, redacted capture of sessions, including subagent sessions.
- Linking commits to sessions, including after amend and rebase.
- "Why does this code exist" for a file, line or range.
- Listing, inspecting and forgetting captured sessions.
- Opt-in history sharing between machines and teammates.
- Statistics on collected data, Claude Code turns, `ctx` calls and capture
  health.

**Out.**

- Searching past sessions by text or meaning (stage 2).
- The automatic brief at session start, the agent skill and the CLAUDE.md
  triggers for search (stage 2). Stage 1 installs a CLAUDE.md block that
  mentions only "why".
- The code graph, "where is it defined" and "what does this affect" (stage 3).
- Decision records and distillation (stage 4).
- Verification on a real Windows machine (Finalize).

## 4. Installation and setup

**The operator can see:**
- Whether wscontext is enabled for the current repository, which plugin version
  is active, and whether its environment is ready.
- A health check that reports, per item, whether Claude Code hooks fire, Git
  hooks are installed and chained, the store is reachable, and sync is
  configured — with the fix for each failure.

**The operator can change:**
- Enable the plugin for themselves or for the whole team by adding the plugin
  to the committed project settings by hand; wscontext never edits them.
- Initialize a repository: install Git hooks without replacing existing ones,
  create the committed configuration file, and add a short CLAUDE.md block.
- Uninstall from a repository: remove hooks and the CLAUDE.md block while
  keeping captured data.

**Deliberately cannot:**
- Have wscontext modify a repository just by installing the plugin.
  Initialization is always an explicit step, because a plugin install must not
  alter repositories the operator did not choose.

A teammate who has not enabled the plugin is unaffected: no hook runs and every
commit succeeds as before.

## 5. Session capture

**The operator can see:**
- Every session in the repository, from any worktree, with its start and end,
  branch, author, files touched and subagents.
- The redacted transcript of any captured session.

**The operator can change:**
- Add redaction rules for project-specific secrets in the committed configuration.
- Set how long raw transcripts are kept; session and commit records are kept
  regardless.

**Deliberately cannot:**
- Turn redaction off. Nothing unredacted is ever stored, because stored history
  may later be shared.
- Ask the agent to record anything. Capture never depends on the agent's
  cooperation.

Capture covers sessions started from the terminal, the VS Code extension or any
other client that runs Claude Code. Other AI assistants are not captured.

## 6. Commit tracing

**The operator can see:**
- In ordinary Git history, which sessions produced each commit, on any Git host.
- For a commit made with no active session, that it has no link.

**The operator can change:**
- Nothing per commit. Linking is automatic for every commit made while a
  session is active in that worktree, whether by the agent, the terminal or an
  IDE's source-control panel.

**Deliberately cannot:**
- Block or delay a commit through wscontext. A failure in tracing loses the
  link, never the commit.

Amended and rebased commits keep their link.

## 7. Why does this code exist

**The operator and the agent can see:**
- For a file, a line, a line range or a named symbol: the commits that last
  changed it, the sessions behind each commit, and the prompts that led to the
  change, in a short answer that fits comfortably in the agent's context.
- When a line has no linked session, the commit alone, marked as such.
- When the line has uncommitted changes, that fact.

**The operator can change:**
- How much is shown: a compact default, a limit, or full detail.

**Deliberately cannot:**
- Receive retrieved text as instructions. Answers are presented as quoted
  historical data, because old transcripts may contain text that would
  otherwise steer the agent.

## 8. Reviewing captured data

**The operator can see:**
- A list of sessions filtered by branch, author, date or file.
- Any session in full — prompts, replies, tool calls and results — page by
  page, or as one preview per turn; any subagent's transcript on its own.
- Which sessions would be sent on the next share.

**The operator can change:**
- Forget a session: remove it and its links from the local store and from any
  history not yet shared.

**Deliberately cannot:**
- Silently remove a session that has already been shared. Forgetting a shared
  session requires an explicit rewrite of the shared history, and the tool says
  so.

## 9. History sharing

**The operator can see:**
- Whether sharing is configured, which remote it uses, and how far local
  history is ahead of or behind it.

**The operator can change:**
- Turn sharing on by naming a remote — the code remote or a separate private
  one. The choice is committed, so teammates inherit it.
- Share on demand at any time.

**Deliberately cannot:**
- Share anything before sharing is turned on. A public repository must not
  publish transcripts by accident.
- Have sessions shared in the background. Once on, history goes out only when
  the operator pushes code or asks explicitly.

When sharing is on, a fresh clone on another machine recovers all shared history
after initialization. A rejected share — someone else shared first — merges and
retries without failing the operator's code push.

## 10. Monitoring

**The operator can see:**
- **Collected data:** sessions, transcript events, linked commits and the share
  of commits that are linked, store size, redaction hits, sharing status.
- **Claude Code turns:** per session and over time — number of turns, turn
  duration, tool calls by kind (search and read tools versus `ctx`), and token
  usage where the transcript exposes it.
- **`ctx` calls:** each call's command, query, number of results, size of the
  answer, latency and freshness flags; how often the agent used `ctx` before
  its first search or read.
- **Capture health:** hook runs, failures and latency per event.
- Any of these for one session, over a time window, as a terminal table or as
  machine-readable output.

**The operator can change:**
- The time window and the view.

**Deliberately cannot:**
- See a web dashboard. Statistics are a terminal report; dashboards can be
  built on the machine-readable output.
- See another machine's capture health or raw call log. Those stay on the
  machine that produced them; per-session counts travel with shared history.

Turn and tool-call figures are collected from the first day, so later stages
can be compared against a baseline.

## 11. Cross-cutting requirements

- **Never in the way.** No capture step can block, slow noticeably or fail a
  session, a tool call or a commit. Every failure degrades to missing data and
  is visible in capture health.
- **Private by default.** Nothing leaves the machine unless sharing is on.
  Everything stored is redacted.
- **One history per repository.** Parallel sessions in different worktrees of
  the same repository write to one shared history.
- **Rebuildable.** Anything derived can be deleted and regenerated from captured
  history and the repository.
- **Portable.** Works on macOS, Linux and Windows.
- **Prerequisites:** Git, Claude Code with plugin support, and uv.

## 12. Feasibility

- **Already proven.** Claude Code hooks receive the session ID and transcript
  location on every event; plugins can ship hooks, skills and executables and
  keep a persistent data directory; Git trailers and orphan branches work on
  every Git host.
- **New but straightforward.** Redaction with pattern rules; commit trailers
  and checkpoint records; per-session statistics from hook events.
- **Known costs.** The transcript format is internal to Claude Code and changes
  between versions, so token figures may be missing until the parser is
  updated. Hook start-up time must stay small on every tool call. The
  transcript on disk may trail the live conversation, so the final copy is
  taken at session end.
- **Blocked.** Nothing.

## 13. Open questions

1. Does context compaction remove entries from the transcript file? If not, the
   pre-compaction snapshot is unnecessary. Open. The snapshot stays on by
   default.
2. Which "active session" applies when a commit is made long after the last
   prompt — how stale may a session be and still be linked? Answered
   provisionally: 120 minutes since the session's last activity; adjustable in
   the configuration. Revisit after the two-week gate.
3. When the Git host squash-merges a pull request, are the session trailers
   kept? If not, should the squash commit be linked from the merged commits?
   Open.
4. How does Claude Code on Windows run plugin hook commands and plugin
   executables — through Git Bash, or directly? Open. Not run on Windows
   before Finalize.
5. Should commits made from a worktree with several active sessions link all of
   them, or only the most recent? Answered: all active sessions in the window
   are linked, one trailer each.

## 14. Beyond stage 1

- **Stage 2 — history search:** search past sessions and commit messages, a
  short brief injected at session start, triggers for the agent, adoption
  figures.
- **Stage 3 — code graph:** where a symbol is defined, what a change affects,
  statistics on the indexed code.
- **Stage 4 — distillation and semantic search:** decision records with
  supersession, meaning-based search.
- **Finalize:** versioned release, Windows verification, polish of removal and
  purge.

## 15. Notes for implementation planning

Built in stage 1: the package, tests and plugin files; see the build plan's
closing notes. The architecture and the recorded decisions fix the store layout, the
single command-line tool, the plugin packaging and opt-in sharing, so the build
plan needs to decide mechanisms, not direction.

The hook path has to start fast and depend on nothing outside the standard
library; that constrains how configuration is loaded and which modules hooks
may import. The launcher is the only place that sees Claude Code's plugin
environment, and must hand paths to the program explicitly.

The history-branch writer is the most delicate piece: concurrent sessions in
several worktrees, union merges with a remote, and recovery after a rejected
push all have to be correct without ever touching the working tree. It deserves
its own work package and the strongest model.

Tests need recorded hook inputs and sample transcripts as fixtures, and
throwaway local Git repositories for linking and sharing; none of this needs the
network. Tests run on macOS only in this stage; Windows readiness is checked
by inspection of the code, and the first Windows run happens in Finalize.
