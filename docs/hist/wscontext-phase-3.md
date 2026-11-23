# wscontext — stage 3 feature description

Requirements for a person deciding whether these are the right ones. Technical
design is a later document, `build-plan-wscontext-phase-3.md`, kept local
and not in the repository.

## 1. Purpose

The agent and the operator can ask "where is this defined?", "what could this
change break?" and "which session last changed this function?" and get the
answer in one call, instead of a chain of searches and file reads. Stage 3 also
measures, for the first time, whether wscontext makes the agent's work cheaper:
the same tasks run once with wscontext's help switched off and once with it on,
and the two runs are compared.

## 2. What we are building

A code graph for each repository. It knows the functions, classes and methods
each file defines, which files import which, which functions call which (matched
by name), which files usually change together in Git history, and which past
sessions changed which functions. On top of the graph: `ctx code` answers where
something is defined and what a file contains; `ctx impact` answers what
depends on a function or a file; `ctx why` accepts a function name and finds it
through the graph. Statistics report on the graph. A fixed set of benchmark
tasks runs headless with wscontext off and then on, and a report compares the
two. One gap carried over from stage 2 closes: a tool's name no longer matches
every session that called that tool.

## 3. Scope of stage 3

**In.**

- The code graph: definitions, imports, calls matched by name, files that
  change together, and links from sessions to the functions they changed, for
  the languages in § 16 Q1.
- Each language as a separate part that can be switched on or off, so a new
  language can be added later as a task of its own (§ 8).
- Keeping the graph current through edits, branch switches and merges, with
  nothing running between calls.
- `ctx code`, `ctx impact`, and `ctx why` on a function or class name.
- Triggers for the new commands in the CLAUDE.md block, and their reference in
  the `ctx` skill.
- The benchmark switch from stage 2, extended to the new commands.
- The on/off benchmark: the task set, a runner, and a comparison report.
- Statistics on the graph and on how often the agent uses the new commands.
- Tool names in search.

**Out.**

- Exact references from a language server or a compiler's index. They come
  later, and only for languages where matching by name gives wrong answers too
  often.
- Anything from the graph in the session brief.
- Sharing the graph between machines. Each machine builds its own.
- A graph across several repositories.
- Decision records and search by meaning (stage 4).
- Judging whether the benchmark result is good enough. That bar is set at
  Alpha; stage 3 only has to run the benchmark end to end.

## 4. Where code is defined

**The operator and the agent can see:**
- For a name: every definition with that name in the repository. Each one shows
  its file, line range, kind (function, class, method or type), its first line, and
  the class it belongs to, if any. Exact matches come first, then names that
  contain the given text.
- For a file: the definitions it holds, in order, with line ranges; the files it
  imports; and the files that import it.
- Answers that match the working tree as it is now, including edits not yet
  committed.
- When the answer may be incomplete — the graph is still being built, or a file
  could not be parsed — that fact, on the answer.

**The operator can change:**
- How much is shown: a compact default, a limit, or full detail, as for the
  other commands.
- Rebuild the graph at any time. Deleting it loses nothing.

**Deliberately cannot:**
- Find definitions in files Git ignores.
- Find definitions in a language the graph does not support. Such files still
  take part in "files that change together" (§ 5).
- Receive results as instructions. Code and history in answers are quoted data,
  as in stages 1 and 2.

## 5. What a change affects

**The operator and the agent can see, for a function, a class or a file:**
- Its callers: the functions that call it by name. Each caller is marked as a
  name match, with the number of definitions that share the name, because a
  name shared by several definitions can point to the wrong one.
- The files that import the file it lives in.
- Tests among the callers and importers, listed separately.
- Files that usually change together with its file in Git history, with how
  often they did.
- The past sessions that changed it, newest first, with the command that opens
  each one.

**The operator can change:**
- How far to follow callers: direct callers by default, or a few levels more,
  up to a fixed maximum.
- How much is shown, as for the other commands.

**Deliberately cannot:**
- See name matches presented as certain. Every caller says how it was found.
- Find calls made indirectly — through a variable, a registry, or a name held
  in a string. The answer says that calls are matched by name only.

## 6. Why a function exists

**The operator and the agent can see:**
- `ctx why <file>#<name>` finds the right definition through the graph,
  including a method given as `Class.method`, at its current lines even with
  uncommitted edits. Today the name is found by a text match.
- `ctx why #<name>`, with no file: the definition found through the graph. When
  several definitions share the name, the candidates are listed with their
  files and lines, and the caller picks one.
- The sessions that changed the function, even when its lines have moved since
  then. These come from the links between sessions and functions, next to the
  commits found through Git's line history.

**Deliberately cannot:**
- Follow a function through a rename. A renamed function starts a new history in
  the graph. `ctx why` on a file and line still follows Git's line history.

## 7. Keeping the graph current

**The operator can see:**
- Answers that match the working tree at the moment of the call. Before
  answering, the graph re-reads only the files whose content changed since it
  last read them.
- After a branch switch or a merge, the graph is refreshed in the background, so
  the next call is fast. The switch or merge itself never waits for it.
- A first call in a repository that does not wait longer than a set time. If the
  graph is not complete by then, the answer says so and the build goes on in
  the background.
- "Files that change together" kept up to date with new commits.

**The operator can change:**
- Paths left out of the graph beyond what Git ignores, such as vendored or
  generated code. The setting is committed, so every machine leaves out the same
  paths.
- Rebuild the graph at any time.

**Deliberately cannot:**
- Have a commit, a branch switch, a merge or a session wait on the graph.
- Share the graph. Each machine builds it from the code and history it already
  has. Links between sessions and functions are rebuilt from shared history.

## 8. Languages

Language support is built so that adding a language later is a separate,
self-contained task.

**The operator can see:**
- Which languages the graph supports, which of them are switched on, and how
  many files of each it read. This shows in the health check and in the
  statistics.

**The operator can change:**
- Switch each supported language on or off, in the committed configuration. A
  language that is off is treated like an unsupported one: its files take part
  only in "files that change together". Switching a language changes the
  answers for that language's files only.

**Whoever adds a language can rely on:**
- Each language is a separate part. It holds its own rules for finding
  definitions, imports and calls, its own tests and its own sample files.
- Adding a language means adding one such part, plus its grammar for the
  parser, and naming it in the list of supported languages. The graph,
  `ctx code`, `ctx impact`, `ctx why` and the statistics need no change.
- Adding, changing or switching off one language leaves the answers for every
  other language as they were. Each language's own tests show this.
- Python, TypeScript and JavaScript are built in exactly this way, so stage 3
  proves the pattern three times.

**Deliberately cannot:**
- Load a language from outside a wscontext release, such as a separate install
  or a file in the repository (§ 16 Q6).
- Lose the other languages when one language's grammar cannot be installed.
  That language is reported as unavailable, and the rest keep working.

## 9. Telling the agent when to use the graph

**The agent can see:**
- In the repository's CLAUDE.md block, as triggers only: run `ctx code` before
  searching for where something is defined, and run `ctx impact` before changing
  a function that other code calls.
- In the `ctx` skill: the full reference for `ctx code`, `ctx impact` and
  `ctx why` on a name, and how to read their output.

**The operator can change:**
- Upgrade an initialized repository's block by running initialization again, as
  in stage 2.

**Deliberately cannot:**
- Have the block grow into a manual. Low adoption is answered by sharper
  triggers, measured in § 13.

## 10. Benchmark switch

**The operator can change:**
- The stage 2 setting that turns the brief and search off now also turns off
  `ctx code` and `ctx impact`. Each answers that it is switched off. Their
  triggers are left out of the CLAUDE.md block. Capture, tracing, `ctx why` and
  statistics keep running in both arms, as in stage 2.

**Deliberately cannot:**
- Switch the graph on or off separately from search. The benchmark compares
  wscontext's help as a whole against none of it.

## 11. The on/off benchmark

**The operator can see:**
- A fixed set of tasks, kept in this repository. Each task has a prompt for the
  agent, a starting point (a repository at a known commit, with its captured
  history), and a check that tells whether the task was done.
- On demand: every task run headless, first with wscontext off and then with it
  on, each in its own throwaway copy. A report compares the two arms per task
  and in total: whether the task was done, turns, tool calls by kind (with
  searches and file reads counted separately), `ctx` calls, tokens and time.
- Each run's report saved with its date and the plugin version, so Alpha and
  Beta can compare against earlier runs.

**The operator can change:**
- The tasks and their checks, by editing the set.
- How many times each task runs in each arm. The agent does not behave the same
  way twice, so one run per arm shows little.
- Run only some of the tasks.

**Deliberately cannot:**
- Run the benchmark in a repository that is not a throwaway copy, or with the
  operator's personal Claude Code configuration. The isolation is the same as
  for the stage 2 eval corpus.
- See a cheaper run counted as better when its task was not done. The report
  shows "done" next to every cost figure.
- Pass or fail stage 3 on the result. The stage gate needs the benchmark to run
  end to end. The value bar is at Alpha.

## 12. Tool names in search

**The operator and the agent can see:**
- A word that is also a tool name, such as `bash`, `read` or `edit`, matches a
  session only where the word appears in the conversation or in a tool's input
  or output. Calling the tool no longer makes a session match the word.
- The 20-question score from stage 2 still at or above the gate (15 of 20). Any
  drop from stage 2's 20 of 20 is named in the gate report.

## 13. Monitoring

**The operator can see, in addition to stages 1 and 2:**
- **Graph:** files, definitions and links by language; files that could not be
  parsed; how long the last full build and the last refresh took; files changed
  since the graph last read them; the graph's size on disk.
- **Adoption:** for each session and over time, calls to `ctx code`,
  `ctx impact` and `ctx why` on a name, and the share of sessions that called
  `ctx code` or `ctx impact` before their first search or file read.

**Deliberately cannot:**
- See another machine's graph figures. The graph is built per machine; adoption
  counts travel with shared history.

## 14. Cross-cutting requirements

- **Never in the way.** No hook, commit, branch switch or merge waits on the
  graph. A failed refresh means a slower next call, visible in capture health.
- **Private by default.** The graph is built from local code and redacted
  history and never leaves the machine.
- **Rebuildable.** The graph can be deleted and built again from the code, Git
  history and captured history.
- **Concurrent.** Parallel sessions can query and refresh the graph at the same
  time.
- **Degrades, does not break.** When the parser cannot be installed, or a
  language is not supported, capture, search and `ctx why` on a line keep
  working. `ctx code` says what is missing, and the health check reports it.
- **One new prerequisite.** The parser and its language grammars are the first
  packages wscontext needs beyond Python itself. They are installed into the
  plugin's own environment when it is built, which needs network access once.
  No compiler is needed on macOS, Linux or Windows.
- **Portable.** Works on macOS, Linux and Windows.

## 15. Feasibility

- **Already proven.** The tree-sitter parser reads many languages and ships
  prebuilt for all three platforms. Git gives "files that change together"
  cheaply from its log. Session records already list the files each session
  touched, and commits already link to sessions. The plugin's environment is
  already built from a lock file. Catching up at query time is proven by the
  stage 2 index. Isolated headless runs are proven by the eval corpus recorder.
  Per-session turn and tool-call counts already exist.
- **New but straightforward.** Definitions and imports for each supported
  language; counts of files that change together; the output of `ctx code` and
  `ctx impact`; tool names kept out of plain-word search.
- **Known costs.** Calls matched by name are wrong where names repeat. Each
  language states imports differently, so each language is its own piece of
  work. The first build on a large repository takes time. A session is linked
  to a function only through the changed lines of its commits; a session whose
  edits were never committed is linked to files only. The benchmark spends API
  usage for every task, arm and run, and small differences between arms mean
  little. The benchmark runs in the small stage 2 story repository, so it
  understates what the graph is worth. The new packages make the plugin's
  environment larger. Keeping languages apart costs some design up front: the
  shared part of the graph has to be settled before the first language is
  built.
- **Blocked.** Nothing.

## 16. Open questions

1. Which languages does the graph support in stage 3? Answered: Python, plus
   TypeScript and JavaScript. Files in other languages take part only in
   "files that change together".
2. In which repository do the benchmark tasks run? Answered: the stage 2 story
   repository, restored at a fixed commit with its recorded history. It is the
   only fixed setup that has both code and captured history. It is small, so it
   understates what the graph is worth.
3. How many tasks, how many runs per arm, and what is a check? Answered: about
   10 tasks, and one run per arm at the gate, so about 20 headless sessions.
   More runs per arm come at Alpha. A check is a command that succeeds when
   the task was done, such as the story's own tests.
4. Are sessions linked to functions only through their commits, or also through
   edits they never committed? Answered, and confirmed when built: functions
   are linked through commits only. A session that never committed is linked to
   files only, and `ctx impact` on a file shows it, marked as having no linked
   commit (§ 15).
5. Should search get a filter by tool once tool names stop matching as words?
   Answered: no, until real use asks for it.
6. Can a language be added from outside a wscontext release, for example as a
   separate install or a file in the repository? Answered: no. A new language
   is a separate part inside wscontext and ships with a release (§ 8).
7. Which kinds of definition does the graph know? Answered: function, class
   and method for every language. TypeScript adds `type`, for interfaces, type
   aliases and enums, because "where is this interface defined" is the common
   TypeScript question.

## 17. Beyond stage 3

- **Alpha:** stages 1 to 3 in daily use on the developer's own repositories,
  with sync between two machines; the benchmark shows whether wscontext makes
  tasks cheaper.
- **Stage 4 — distillation and semantic search:** decision records with
  supersession, and search by meaning merged with text search.
- **Later:** exact references for languages where name matching hurts.

## 18. Notes for implementation planning

Stage 2 left places for the graph. The store layout reserves the graph's
database next to the search index. The branch-switch and merge Git hooks are
installed, and their graph step is empty. `ctx why` already accepts
`file#name`, found by a text match, and the graph replaces that match. Session
records already hold the files each session touched. A new layer agent owns
the graph.

The parser is wscontext's first third-party dependency. It must stay off the
hook path, so the hooks only start a detached refresh. Adding it is a lock file
change plus a version bump, since an installed plugin rebuilds its environment
only when the version changes. The build plan must choose between one package
that bundles many grammars and one package per language, and check that the
choice installs with no compiler on all three platforms. A package per language
fits switchable languages better; a bundle ships every grammar whether it is on
or not.

Language support needs one shape that every language part fills, settled
before the first language is built: what a part receives, what it returns, how
it is listed and switched, and what its tests and sample files must cover. The
shared graph, the commands and the statistics then speak only to that shape.
Building Python first and TypeScript and JavaScript second tests the shape:
the second language should need no change outside its own part.

The decisive design point is keeping answers true to the working tree without
anything running between calls: a query re-reads only changed files, as the
index catches up in stage 2. Linking sessions to functions needs the changed
lines of each linked commit mapped onto the definitions they fall in, rebuilt
from shared history.

The benchmark runner is acceptance-style tooling. It should reuse the corpus
recorder's isolation and its throwaway repository, and read its figures from
the existing turn statistics. The tool-name change belongs to the index and
needs the eval set run again.
