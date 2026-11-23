# Roadmap

The stages that take wscontext from nothing to a released plugin, each with the
gate that ends it. What exists now: [architecture.md](architecture.md).

Each stage can be used on its own. Stop as soon as the value stops growing.

## The loop for every stage

1. **Requirements.** Copy `templates/feature-phase.md` to
   `wscontext-phase-N.md` and fill it from the stage summary below. Review it
   until the requirements are right.
2. **Build plan.** Copy `templates/build-plan.md` to
   `build-plan-wscontext-phase-N.md`. Route every work package to a layer agent
   in `.claude/agents/`; add an agent only when the stage opens a new layer.
3. **Build.** `/kit:run-build-plan docs/build-plan-wscontext-phase-N.md`.
4. **Gate.** Run the stage's acceptance scenarios and any eval set
   (§ Gates and milestones). Record the result in the build plan's closing
   notes.
5. **Record.** Update `architecture.md` (mark the stage built in § Stages)
   and append ADR entries for decisions taken during the build.
6. **Archive.** When stage N+1 starts, `git mv` stage N's requirements into
   `hist/`, move its build plan there too, and fix links (see [CLAUDE.md](CLAUDE.md) § Archiving).

## Gates and milestones

- **A stage gate checks the stage's requirements, not real use.** Each
  capability section of the stage document maps to acceptance scenarios with an
  observable result, run at three levels: the offline suite; a scripted run
  against the installed plugin in a throwaway repository, driving real Git and
  headless Claude Code (`claude -p`); a short manual checklist for what a
  script cannot reach. Scripted scenarios are kept and rerun at every later
  gate.
- **Real use starts at Alpha**, after stage 3, and widens at Beta, after
  stage 4. Non-functional and integration checks (hook latency, blocked
  commits, store growth, sync between machines, team use) belong to those
  milestones, not to stage gates.
- **Value is measured by an on/off task benchmark:** a fixed task set run
  headless with wscontext's brief and search switched off, then on, compared
  through `ctx stats turns` (→ ADR-029).

## Stage 1 — Capture and trace

Plugin, capture with redaction, commit linking, opt-in history sharing, and
`ctx why`, `sessions`, `show`, `forget` and `stats`.

- **Requirements:** [wscontext-phase-1.md](hist/wscontext-phase-1.md).
- **Monitoring added:** collected data, Claude Code turns, `ctx` calls, capture
  health.
- **Acceptance run:** a scripted run of the stage-1 scenarios against the
  installed plugin, added to the build plan as its last work package.
- **Gate:** the acceptance scenarios for §4–§10 of the stage document pass on
  the installed plugin: setup (init, doctor, uninstall, chained hooks);
  capture (resume, compaction, a subagent, two concurrent sessions); tracing
  (amend, rebase, a commit with no session, an aborted message, a merge);
  `why`, `show` and `forget`; sharing between two clones through one remote;
  `ctx stats` agreeing with what the scenarios did.

## Stage 2 — History search

FTS5 index with the query-time gap check, `ctx find`, the session brief at
start, the `ctx` skill and CLAUDE.md triggers for search.

- **Requirements:** [wscontext-phase-2.md](hist/wscontext-phase-2.md).
- **Monitoring added:** index size and lag; adoption (sessions with a `ctx`
  call, `ctx` before the first search or read).
- **Stored data volume:** settled before Alpha shares history, since shared
  history is append-only and the larger copy wins on merge.
  - *Measure:* bytes per session by kind (conversation, tool results,
    subagents, snapshots, bookkeeping), local store versus history branch,
    growth over time, in `ctx stats data`.
  - *Refine what is stored:* drop Claude Code's bookkeeping entries; decide
    what is kept of large tool results; settle whether the PreCompact snapshot
    is needed; nothing `why`, `show`, `find` or the turn statistics read is
    lost. Earlier captures are not re-trimmed.
  - *Control:* a size figure per session and per store with a warning in
    `ctx doctor` and `ctx stats`; `retention.raw_days` enforced on the local
    store (records kept); shared history moves outside `refs/heads/` so a
    plain clone does not fetch it (supersedes ADR-007, ADR-008 in part
    → ADR-031).
- **Abandoned sessions:** a session that never sends `SessionEnd` is finalized
  once idle past the staleness window: final copy, counts and history commit.
- **Benchmark switch:** a configuration setting turns the brief and search off,
  for the on/off benchmark.
- **Eval set:** 20 questions about past work, each with the expected session or
  commit, kept in the repository and run by a `ctx` eval command.
- **Eval corpus:** a fixed corpus recorded once by a scripted headless run in a
  throwaway repository, redacted and kept in the repository.
- **New agent:** `py-index`.
- **Gate:** the stage's acceptance scenarios pass, the right session or
  commit is in the top 3 for at least 15 of the 20 questions, and a stored
  session is at most a set share (30%) of the size of its source transcript,
  measured on the eval corpus.
- **Gate result (2026-11-06):** passed. The offline suite was green.
  `acceptance/stage1.py` was green. `acceptance/stage2.py` passed 10 of 10
  scenarios, none of them manual. The VS Code checklist passed.
  `ctx eval --min 15` put the right result in the top 3 for 20 of 20 questions.
  On the eval corpus, stored sessions took 15.0% of the size of their source
  transcripts. Deviations are listed in the build plan's closing notes.

## Stage 3 — Code graph

tree-sitter symbols, co-change from `git log`, session links, then
name-matched `calls`; `ctx code`, `ctx impact`; `ctx why #symbol` on the graph.

- **Requirements:** [wscontext-phase-3.md](hist/wscontext-phase-3.md).
- **Monitoring added:** files, symbols and edges by language; build time; stale
  files.
- **New agent:** `py-graph`.
- **Benchmark:** the on/off task set, kept in the repository and run headless.
- **Gate:** the acceptance scenarios for `ctx code`, `ctx impact` and
  `ctx why #symbol` pass, and the benchmark runs end to end.
- **Gate result (2026-11-11):** passed. The offline suite was green.
  `acceptance/stage3.py`, `stage2.py` and `stage1.py` were green against the
  installed 0.3.0 plugin, and the manual checklist passed. The benchmark ran
  end to end: 20 sessions, 10 tasks per arm. `ctx eval --min 15` put the right
  result in the top 3 for 20 of 20 questions after tool names left the index.
  Deviations are listed in the build plan's closing notes.

## Alpha — own projects

Stages 1 to 3 in daily use on the developer's own repositories, with sync on
between two checkouts of one repository.

- **Started (2026-11-11):** plugin 0.3.4 is installed for the user. Four
  repositories capture sessions: this one and three private ones. This
  repository is public, so sync stays off here and its session history is
  never pushed. Sync is tested with two checkouts of one private repository
  on one machine, through its remote. Sessions count toward the exit from
  this date.
- **Watch:** `ctx stats hooks` (failures, p95 against the budget), blocked
  commits, store and history-branch growth, sync between the two checkouts;
  whether the agent runs `ctx why` before changing code it did not write.
- **Value:** the on/off benchmark shows fewer search-type tool calls and tokens
  per task with wscontext on. The stage 3 run did not show it: the story
  repository is about 940 lines and holds its own decisions in a file. At
  Alpha the benchmark runs on this repository, with tasks that only past
  sessions or many files can answer, 3 runs per arm.
- **Value result (2026-11-12, plugin 0.3.4):** 10 tasks, 3 runs per arm, 60
  sessions. Both arms hold the same store of past sessions, so the off arm
  can search the raw transcripts with grep.

  | | off | on |
  |---|---|---|
  | Runs done, of 30 | 27 | 30 |
  | Search-type tool calls (per-task means, summed over 10 tasks) | 60.3 | 56 |
  | `ctx` calls (same) | 4 | 18.3 |
  | Tokens (per-task means, summed over 9 tasks) | 7,041,200 | 6,909,840 |

  The three failed runs in the off arm are all history tasks. One task is
  left out of the token figure: four of its sessions hit the runner's
  300-second limit (3 off, 1 on). Its check passed, so they count as done,
  but Claude Code reported no turns, tokens or cost for them. The result is
  not judged here. Any change it suggests goes into stage 4.
- **Sync result (2026-11-19, plugin 0.3.5):** passed in both directions. The
  test used two checkouts of one private repository on one machine, with its
  GitHub remote. The first checkout pushed 15 sessions. The second checkout
  received them with `ctx init`. Then a session and a commit made in the
  second checkout reached the first one through its throttled fetch.
  `ctx sessions`, `ctx find`, `ctx show` and `ctx why` gave the same answers
  in both checkouts. Claude Code sessions used a read-only deploy key: their
  background fetches succeeded, and GitHub refused their pushes. The
  developer's own key needs a passphrase. Stage 4 input:
  - The history push that `pre-push` starts fails when the key needs a
    passphrase, and nothing tells the user. `ctx sync` from a terminal
    pushes.
  - `ctx doctor` reports that failure as a failed Claude Code hook, and still
    shows sync as OK. It also counts against the exit's "no hook failure".
  - A session enters the history ref only when it ends. A push while it is
    open shares the commit's link but not the session. The other checkout
    then shows only `no session '<id>'`.
  - When two sessions were active at commit time, `ctx why` lists them by
    start time, so a session that did not touch the line can come first.
  - `ctx sync --dry-run` prints the same output as `ctx sync --status`.
- **Exit:** at least 30 real sessions with no hook failure and no blocked
  commit, and the benchmark result above. Defects found go into stage 4.
  A sync fetch or push that fails for lack of a credential is not counted
  as a hook failure. It is the first stage 4 item in the sync result above.
- **Exit result (2026-11-21, plugin 0.3.5):** passed. 34 real sessions since
  the start: 18 and 11 in two of the private repositories, 5 in this one.
  Sessions with no turns and checks shorter than a minute are not counted.
  Two hook failures, both a sync fetch or push refused for lack of a
  credential, so not counted. No Git hook failed and no commit was blocked.
  Every code commit in the synced repository is linked to a session.
- **Rust in the code graph:** a Rust language part, built after the sync test
  and the benchmark. Requirements:
  [graph-rust-phase-1.md](hist/graph-rust-phase-1.md).
- **Gate result (2026-11-23, plugin 0.4.1):** passed. The offline suite had 800
  tests and no warnings. `acceptance/graph_rust.py` passed 8 of 8 scenarios and
  uses no `claude -p`. `stage1.py` passed 13 of 13 (some went manual) and
  `stage2.py` 10 of 10, both on 0.4.0. `stage3.py` passed 15 of 16 on 0.4.0.
  Its `stats-code` scenario expected the old language list and passed on 0.4.1
  after one line of the script was updated. The operator read a real run of
  the repeated-name answer and asked for line-range links in `ctx why`. That
  fix made 0.4.1. Deviations are listed in the build plan's closing notes.

## Import — past work in a repository already in progress

wscontext is often set up in a repository that already has history: past
Claude Code sessions, commits and design documents. This stage finds what is
still available, shows it, and imports what the user accepts. Built after the
Alpha exit and before Beta, where a second developer sets wscontext up on a
shared repository that is already in progress.

- **Requirements:** [import-phase-1.md](import-phase-1.md).
- **Past sessions:** Claude Code transcripts of this repository that are still
  on disk and not captured yet. Claude Code deletes its transcripts after a
  set number of days (30 by default), so older sessions are gone for good.
- **Past commits:** a commit made before setup is linked to an imported
  session only when that session ran the `git commit` that made it. A commit
  with no such session stays unlinked. Links are never guessed from time or
  changed files.
- **Workspace documents:** Markdown files in the repository that record
  decisions or design, such as `docs/adr.md` and `docs/architecture.md`.
  Their sections become searchable through `ctx find`, next to sessions and
  commits. Turning them into decision records is part of stage 4.
- **The user decides:** a preview lists what was found (sessions, the commits
  they link, documents) and writes nothing. The user then imports all of it,
  part of it, or nothing. A later run offers only what is new.
- **Same path as live capture:** imported sessions and commit links pass
  through the redactor and are stored like a live session. With sync on, they
  are shared like a live session. Documents stay in the repository; their
  text passes through the redactor before it is indexed.
- **Not counted:** imported sessions do not count toward the Alpha exit.
- **Gate:** the stage's acceptance scenarios pass in a throwaway repository
  that has transcripts and commits from before setup: the preview writes
  nothing; accepted items are imported and declined ones are not; a commit is
  linked only through its own `git commit` result; a second run imports
  nothing new.

## Stage 4 — Distillation and semantic search

Asynchronous decision records with supersession and review status, local
embeddings, hybrid ranking.

- **Monitoring added:** decisions by status (active, superseded,
  machine-written, reviewed), embedding coverage.
- **Gate:** the stage's acceptance scenarios pass, the 20-question set improves
  further, and spot checks find no superseded decision presented as current.

## Beta — team use

Stages 1 to 4, functionally complete, used by at least one more developer on a
shared repository.

- **Watch:** history sharing between developers, history-branch size on the
  remote, adoption across both developers.
- **Value:** the benchmark improves on Alpha's result.
- **Exit:** Alpha's reliability bar held across both developers.

## Stage 5 — Finalize

- A versioned plugin release published through the marketplace on the chosen
  Git host, with a changelog.
- A README covering install, init, sharing, stats and uninstall.
- Purge of a shared session (branch rewrite with guidance).
- A performance check of hook latency against the budget, from
  `ctx stats hooks`, on a large repository.
- A Windows 11 VM (UTM or Parallels on Apple Silicon): run the offline suite,
  then click through install → session → commit → `ctx why` →
  `ctx stats`.
- **Optional:** a VS Code extension that shows the session behind a line on
  hover.
- **Gate:** a clean install on a second machine and on Windows passes
  `ctx doctor` and the scripted acceptance run.
