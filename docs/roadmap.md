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
4. **Gate.** Measure the stage gate with `ctx stats` and the eval set. Record
   the result in the build plan's closing notes.
5. **Record.** Update `architecture.md` (move the stage out of § Not built yet)
   and append ADR entries for decisions taken during the build.
6. **Archive.** When stage N+1 starts, `git mv` stage N's two documents into
   `hist/` and fix links (see [CLAUDE.md](CLAUDE.md) § Archiving).

## Stage 1 — Capture and trace

Plugin, capture with redaction, commit linking, opt-in history sharing, and
`ctx why`, `sessions`, `show`, `forget` and `stats`.

- **Requirements:** [wscontext-phase-1.md](wscontext-phase-1.md).
- **Build plan:** [build-plan-wscontext-phase-1.md](build-plan-wscontext-phase-1.md).
- **Monitoring added:** collected data, Claude Code turns, `ctx` calls, capture
  health. The turn and tool-call baseline starts here.
- **Gate:** two weeks of real work captured, with zero blocked commits or
  stalled sessions; with sharing on, history is available on a second machine
  after clone and init.

## Stage 2 — History search

FTS5 index with the query-time gap check, `ctx find`, the session brief at
start, the `ctx` skill and CLAUDE.md triggers for search.

- **Monitoring added:** index size and lag; adoption (sessions with a `ctx`
  call, `ctx` before the first search or read).
- **Eval set:** 20 questions about past work, each with the expected session or
  commit, kept in the repository and run by a `ctx` eval command.
- **New agent:** `py-index`.
- **Gate:** the right session or commit is in the top 3 for at least 15 of the
  20 questions.

## Stage 3 — Code graph

tree-sitter symbols, co-change from `git log`, `touched_by` edges, then
name-matched `calls`; `ctx code`, `ctx impact`; `ctx why #symbol` on the graph.

- **Monitoring added:** files, symbols and edges by language; build time; stale
  files.
- **New agent:** `py-graph`.
- **Gate:** fewer search-type tool calls and tokens per task than in stage 2,
  from `ctx stats turns`.

## Stage 4 — Distillation and semantic search

Asynchronous decision records with supersession and review status, local
embeddings, hybrid ranking.

- **Monitoring added:** decisions by status (active, superseded,
  machine-written, reviewed), embedding coverage.
- **Gate:** the 20-question set improves further, and spot checks find no
  superseded decision presented as current.

## Stage 5 — Finalize

- A versioned plugin release published through the marketplace on the chosen
  Git host, with a changelog.
- A README covering install, init, sharing, stats and uninstall.
- Retention pruning, and purge of a shared session (branch rewrite with
  guidance).
- A performance check of hook latency against the budget, from
  `ctx stats hooks`, on a large repository.
- A Windows 11 VM (UTM or Parallels on Apple Silicon): run the offline suite,
  then click through install → session → commit → `ctx why` →
  `ctx stats`.
- **Optional:** a VS Code extension that shows the session behind a line on
  hover.
- **Gate:** a clean install on a second machine and on Windows passes
  `ctx doctor` and the stage-1 human gate.
