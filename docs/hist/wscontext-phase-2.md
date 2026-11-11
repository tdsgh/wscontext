# wscontext — stage 2 feature description

Requirements for a person deciding whether these are the right ones. Technical
design is a later document, `build-plan-wscontext-phase-2.md`, kept local
and not in the repository.

## 1. Purpose

The agent and the operator can ask "has this been done or discussed before?"
and get the right past session or commit in one call, and every new session
starts already knowing the recent work that bears on it. Stage 2 turns the
history stage 1 captures into something the agent actually reaches for, and
measures whether it does.

## 2. What we are building

A text search over every captured session and linked commit in a repository,
`ctx find`; a short brief of relevant prior work handed to the agent when a
session starts; agent instructions that say when to search; statistics on the
search index and on how often the agent uses `ctx`; a setting that switches the
brief and search off for comparison runs; and a fixed set of questions that
scores search quality. Alongside, two stage-1 gaps close: sessions that end
without a clean shutdown are finalized, and the volume of stored data is
measured, trimmed to what wscontext reads, and kept under control — settled
before Alpha starts sharing history between machines.

## 3. Scope of stage 2

**In.**

- Text search over past sessions and commit messages, with filters.
- Rebuilding the search index from captured history.
- The session-start brief.
- The `ctx` skill and the CLAUDE.md block extended with search triggers.
- Finalizing sessions that never sent a clean end.
- Stored data volume: measuring it, trimming what is stored to what wscontext
  reads, size warnings, local retention pruning, and shared history kept out
  of plain clones of the sync remote.
- A setting that switches the brief and search off.
- A 20-question eval set and a command that scores it.
- Statistics on the index and on agent adoption of `ctx`.

**Out.**

- Search by meaning rather than words, and decision records (stage 4).
- The code graph, "where is it defined" and "what does this affect"
  (stage 3).
- The on/off task benchmark itself (stage 3); stage 2 provides only the switch.
- Search across repositories.
- Purge of shared sessions, which rewrites shared history (Finalize).
- Re-trimming sessions captured before this stage.

## 4. Searching past work

**The operator and the agent can see:**
- For a topic in plain words: the past sessions and commits that discuss it,
  most relevant first, each with its date, branch, author, the matching
  passage and the command that opens it in full. A matching commit comes
  right after the session that produced it.
- Matches from what was asked, what the agent replied, and commit messages.
- Results narrowed by branch, author, date, file touched, or kind (session or
  commit).
- Sessions from the current session's own earlier turns, and sessions synced
  from other machines, as soon as they are captured — a search never misses
  history captured before it ran.
- When the answer may be incomplete — history not yet fetched from the sync
  remote, or a session still running — that fact, on the answer.

**The operator can change:**
- How much is shown: a compact default, a limit, or full detail, as for
  `ctx why`.
- Rebuild the index from captured history at any time; deleting it loses
  nothing.

**Deliberately cannot:**
- Find text that redaction removed. The index is built from redacted history
  only.
- Find a forgotten session. Forgetting removes it from search at once.
- Receive results as instructions. Matches are quoted historical data, as in
  stage 1.

## 5. The session brief

**The agent can see, at session start:**
- A short brief of prior work relevant to where it is starting: recent
  sessions on the current branch with what they set out to do and the commits
  they produced, and sessions that touched files with uncommitted changes.
- Nothing, when there is no relevant prior work — no empty heading.

**The operator can see:**
- The exact brief a session starting now would receive, on demand.

**The operator can change:**
- Turn the brief off, separately from search.

**Deliberately cannot:**
- Get a brief longer than a fixed budget of about 500 tokens. A larger brief
  costs every session whether or not it helps.
- Have session start wait on the brief. If it cannot be built within the hook's
  time budget, the session starts without it.

The brief is given at a new session and after clear and compaction; a resumed
session already holds its context and gets none.

## 6. Telling the agent when to search

**The agent can see:**
- In the repository's CLAUDE.md block: when to run `ctx find` and when to run
  `ctx why`, as triggers only.
- In the `ctx` skill: the full command reference, including search, and how to
  read its output.

**The operator can change:**
- Upgrade an initialized repository's block by running initialization again;
  their own text around the block is kept.

**Deliberately cannot:**
- Have the block grow into a manual. Low adoption is answered by sharper
  triggers or a better brief, measured in § 10.

## 7. Sessions that never end cleanly

**The operator can see:**
- A session whose conversation was closed without a clean end — a closed
  VS Code conversation, a terminal that is gone — listed with its final
  transcript, counts and turns, and shared like any other once idle past the
  staleness window.
- Whether a listed session is running, finished, or finalized after going
  idle.

**The operator can change:**
- The staleness window, already in the configuration.

**Deliberately cannot:**
- Lose content when an idle-finalized session is resumed later. It continues,
  and its record and shared copy are brought up to date when it ends or goes
  idle again.

Finalization happens the next time wscontext runs in that repository after the
window — a session event, a commit or a `ctx` call. Nothing keeps running to do
it.

## 8. Stored data volume

What is stored, and how large it may grow, is settled in this stage. Shared
history only ever grows, and where two machines hold different copies of a
session the larger one is kept, so whatever shape stored sessions have when
Alpha starts sharing between machines stays on the remote.

**The operator can see:**
- For each session and for the whole store: bytes stored by kind —
  conversation, tool results, subagents, compaction snapshots — and how that
  compares with the source transcript Claude Code wrote.
- The same split for the local store and for shared history, and how both
  grew over a time window.
- A warning in the health check and the statistics when a session or the
  store passes its size limit, naming what takes the space.
- A stored transcript holding the conversation — prompts, replies, tool calls,
  tool results as far as they are kept, subagents — without Claude Code's
  internal bookkeeping (system prompt snapshots, API request records, queue
  entries, skill and tool listings).
- A plain clone or fetch of the sync remote downloads code only; shared
  history comes down through initialization and sharing alone.

**The operator can change:**
- The size limits that trigger a warning.
- How much of a long tool result is kept: all of it (the default), or the
  first N characters, marked as cut. The choice is committed, so every
  machine stores the same shape, and it applies to sessions captured after it
  changes.
- How long raw transcripts are kept on this machine, already in the
  configuration; older ones are removed locally, while session and commit
  records stay.

**Deliberately cannot:**
- Choose per machine what is trimmed. The rule is the same everywhere, so two
  machines never store different shapes of one session.
- Lose anything search, `ctx show`, `ctx why`, the brief or the turn
  statistics read, beyond the tool-result tail a repository chose to cut.
- Shrink history that is already shared. Pruning removes local copies only;
  removing shared history is a rewrite, in Finalize.
- Lose shared history when a remote that already holds it in the stage-1
  location is moved to the new one. The move happens once, and the tool says
  when it has.

Sessions captured before this stage keep their full copies. Shared history no
longer appears among the remote's branches on the Git host, and the host's
branch protection does not apply to it.

## 9. Benchmark switch

**The operator can change:**
- One setting turns the brief and search off for a repository: no brief at
  session start, and `ctx find` answers that search is switched off. Capture,
  tracing, `ctx why` and statistics keep running, so both arms of a comparison
  are measured the same way.

**The operator can see:**
- Whether the switch is on, in the health check and in the statistics for each
  session.

**Deliberately cannot:**
- Switch it per session from inside the agent. It is set before a run, not
  chosen by the agent.

## 10. Measuring search quality

**The operator can see:**
- A fixed set of 20 questions about past work, kept in the repository, each
  with the session or commit that answers it.
- On demand, a score: for each question, the rank of the expected answer, and
  how many of the 20 land in the top 3.

**The operator can change:**
- The questions and their expected answers, by editing the set.

The questions are asked against a fixed corpus: sessions recorded once by a
scripted run in a throwaway repository, redacted, and kept in this repository,
so every later gate scores the same history.

**Deliberately cannot:**
- Pass the stage gate with fewer than 15 of 20 in the top 3.

## 11. Monitoring

**The operator can see, in addition to stage 1:**
- **Index:** its size, how many sessions and commits it holds, and how far it
  trails captured history.
- **Adoption:** the share of sessions that called `ctx` at all, called
  `ctx find`, and called `ctx` before their first search or read; over time
  and per session.
- **Brief:** for each session, whether a brief was given and its size.

**Deliberately cannot:**
- See another machine's index figures. The index is rebuilt per machine;
  adoption counts travel with shared history.

## 12. Cross-cutting requirements

- **Never in the way.** The brief and indexing never delay session start, a
  tool call or a commit past the hook budget; failure means no brief or a
  slower next search, visible in capture health.
- **Private by default.** The index holds only redacted history and never
  leaves the machine.
- **Rebuildable.** The index can be deleted and regenerated from captured
  history.
- **Concurrent.** Parallel sessions can capture while another searches.
- **No new prerequisites.** Search works on the Python and SQLite that stage 1
  already requires.
- **Portable.** Works on macOS, Linux and Windows.

## 13. Feasibility

- **Already proven.** SQLite full-text search ships with Python; stage 1 hooks
  already run at session start and can return context to the agent; captured
  transcripts already separate prompts, replies and tool calls.
- **New but straightforward.** Indexing exchanges and commit messages;
  idle-session finalization on the next wscontext run; dropping bookkeeping
  entries on copy; adoption figures from the counts stage 1 already records.
- **Known costs.** The brief has to be built inside the session-start time
  budget. Ranking by words alone misses paraphrases; the eval set measures how
  much. Twenty questions need history with twenty distinct, findable answers;
  recording the corpus spends API usage once and adds its size to the
  repository. In the one session measured so far, bookkeeping was about two
  fifths of the stored transcript, tool results about a sixth, and a
  compaction snapshot nearly doubled the session's local footprint.
- **Blocked.** Nothing.

## 14. Open questions

1. Should shared history move outside the remote's branch namespace so a plain
   clone does not download it? Answered: yes (§ 8).
2. Which captured history does the eval set search before Alpha? Answered: a
   fixed corpus recorded once by a scripted run and kept in the repository
   (§ 10).
3. Are sessions captured in stage 1 re-copied without bookkeeping, or does the
   smaller copy apply to new captures only? Answered: new captures only; a
   smaller re-copy would lose to the full copy already shared (§ 8).
4. In the off arm of a comparison, should the CLAUDE.md block's search triggers
   also be withheld, or only the brief and search answers? Answered: withheld
   too. With the benchmark switch (§ 9) off, the CLAUDE.md block is written
   without the search trigger. The health check flags a CLAUDE.md block that
   does not match the switch.
5. Is a session that went idle, was finalized, and is then resumed still linked
   to commits made after it resumes? Answered: yes. When a finalized session
   continues, it becomes active again. It is linked to commits made after it
   resumes, and it is finalized again later.
6. What is kept of a large tool result — in full, cut to a fixed size, or only
   a reference when the content can be read back from Git, such as a file
   read at a known commit? Answered: kept in full by default. The transcript
   also holds a second, structured copy of each tool result; that copy is
   dropped. A setting in the committed configuration can cut long results at a
   set number of characters and mark them as cut. The setting applies to new
   captures only.
7. Is the compaction snapshot needed at all? It is the stage-1 open question
   on whether compaction removes entries from the transcript file; the answer
   decides whether snapshots stay, become opt-in, or go. Answered: snapshots
   become opt-in and are off by default. In the one session measured, every
   entry in the snapshot was also in the final copy.
8. What share of its source transcript may a stored session take, for the
   gate, and what size limits per session and per store trigger a warning?
   Answered: for the gate, stored sessions may take at most 30% of the size of
   their source transcripts, measured over the eval corpus as a whole. A
   warning starts at 10 MB per stored session and at 1 GB per store. The local
   store and shared history are counted separately.
9. What does each entry we keep actually need? Answered: a list for each kind
   of entry. The list says what is kept, what is dropped, and what is stored
   only once per session: on the first entry that carries it, and again when
   its value changes. The list was approved at a review of real stored
   entries. Entry types and fields that the list does not name are kept.

## 15. Beyond stage 2

- **Stage 3 — code graph:** where a symbol is defined, what a change affects,
  `ctx why` on a symbol from the graph; the on/off benchmark run end to end.
- **Alpha:** daily use on the developer's own repositories, with adoption and
  hook health watched.
- **Stage 4 — distillation and semantic search:** decision records with
  supersession, search by meaning merged with text search, scored on the same
  20 questions.

## 16. Notes for implementation planning

Stage 1 left a store with redacted transcripts and normalized session and
commit records, a `ctx` output layer with budgets, data framing and freshness
lines, a call log, and statistics views; search, the brief and the eval
command all build on those. The index directory is already reserved in the
store layout. A new layer agent owns the index.

The decisive design point is keeping the index current without anything
running: each search takes in whatever was captured since the last one, and
the brief has to do the same within the session-start budget or fall back to
nothing. Idle-session finalization has the same shape — it piggybacks on the
next wscontext run — and touches the most delicate stage-1 piece, the history
branch writer, so it deserves care and the strongest model.

Dropping bookkeeping entries is a change to the transcript adapter; the list of
what counts as bookkeeping must come from real transcripts, and `ctx show` and
the turn statistics must read the same after the change. Measuring by kind
needs the same classification, so it belongs in the adapter too. Local pruning
must leave the history branch untouched and must not remove a transcript the
next share still has to send. Moving shared history
off the branch namespace touches fetch, push, initialization and the health
check, needs decision records superseding the current ones on the history
branch and sync, and needs a one-time move for remotes that already hold it.

Re-running initialization already replaces the CLAUDE.md block in place, so the
new triggers reach initialized repositories without a separate upgrade step.
The eval set and the acceptance scenarios both need captured history with known
answers. The eval corpus is recorded once, by a script in the style of the
stage-1 acceptance run, and its questions are written against what it holds;
it has to be small enough to keep in the repository.
