# Design rationale

The reasoning behind the decisions in [adr.md](adr.md). What the system is
lives in [architecture.md](architecture.md); this file only explains why. Each
section is linked from the ADR entries it supports.

## Why capture runs on hooks

Skills and CLAUDE.md instructions are suggestions the model may skip, forget
after context compaction, or follow inconsistently. An agent summarizing its
own session is also biased toward its own narrative. Claude Code hooks run
deterministically on every lifecycle event, and Claude Code already writes a
full JSONL transcript whose path each hook receives. Capture therefore copies
what Claude Code records instead of asking the agent to record anything.
Instructions belong only on the retrieval side.

## Why distill decisions instead of searching raw history

Transcripts are dominated by tool output, file dumps and dead ends. Searching
them returns plausible but low-value matches and burns context. The valuable
unit is a decision: what was chosen, why, and what was rejected. Raw text is
indexed only as a fallback, and decision records carry the highest weight.

## Why results carry freshness and supersession

A decision reversed last month still matches today's query. Without
supersession and freshness signals, retrieval actively misleads the agent.
Every result carries a date, a status (active or superseded), and a check of
whether the code it refers to still exists.

## Why push and pull and why adoption is measured

Agents are trained on grep, read and glob, and an extra tool competes with
habit. Entire documents the case where the agent ignores its graph and greps
anyway. Two mitigations follow: inject relevant context automatically at
session start (push), and measure adoption from the transcripts already
captured instead of assuming it. If adoption is low, the response is tighter
triggers or a richer push brief, not longer instructions.

## Why the code graph comes third

In a repo of a few thousand lines, ripgrep is fast and cheap, and a graph adds
build cost and staleness risk. Its value grows with codebase size, language
coupling and refactoring frequency. It is built after history search proves
useful, and starts with cheap signals: symbol definitions, co-change from
`git log`, and the session-to-symbol `touched_by` edge.

## Why history lives on an orphan branch

A plain folder in the working tree either gets committed (bloating history and
leaking transcripts) or gitignored (lost on a fresh clone). An orphan branch
shares no ancestry with the code, travels with the repository to any Git host,
and can be written with plumbing without touching the working tree. Entire uses
the same approach with `entire/checkpoints/v1`; the difference here is that
indexing stays on each host instead of in a cloud service.

Append-only files named by session ID or commit SHA mean two hosts never edit
the same path, so a merge is a union and cannot conflict.

## Why build it rather than reuse

Alternatives weighed:

- **Entire CLI for capture, own index on its branch.** Less work, but depends
  on an undocumented storage format owned by someone else.
- **Claude Code's native features** (CLAUDE.md, memory, session resume). Covers
  the 80% case for one person, but gives no commit linkage, no shared history
  and no search across sessions.
- **Existing code intelligence** (ctags, ast-grep, LSP-backed MCP servers).
  Good for symbols, but none joins code structure to the sessions that
  produced it.

Building it is justified by full privacy, any Git host, and control over
ranking and distillation. Existing tools remain candidates inside components —
for example tree-sitter grammars and SCIP indexers in the graph.

## Why a plugin

A Claude Code plugin ships hooks, skills and executables together, versions
them as one unit, and is enabled per repo through committed settings. That
replaces three manual steps of a plain package — installing the CLI, writing
hook entries into settings, copying the skill — and removes the need for a
`command -v … || exit 0` guard: plugin hooks fire only where the plugin is
enabled.

What a plugin cannot do shapes the rest of the design:

- It has no install-time hook and cannot touch the repository, so Git hooks
  and the CLAUDE.md block are installed by an explicit `ctx init`.
- It has no Python dependency handling, so its launcher maintains its own
  environment in the plugin data directory.
- Its `bin/` directory is appended after the user's own `PATH`, so a generic
  executable name can be shadowed silently. One distinctive entrypoint, `ctx`,
  limits that exposure, and hooks call it by absolute path.

## Why the store sits under the git common dir

Every worktree of a repository has its own working tree but shares one Git
common dir. A store under the working tree fragments history across parallel
sessions in different worktrees. Under the common dir it is shared, invisible
to `git status`, needs no gitignore entries, and survives checkout, stash and
`git clean -fdx`.

Only content meant for review and normal pull requests — configuration and
decision records — stays in the working tree.

## Why sync is opt-in

Even redacted transcripts contain file contents, command output and design
discussion. Pushing them to `origin` by default would publish them for any
public repository. Sync therefore starts off, the remote is an explicit
setting, and pushes happen only when the user pushes code or runs `ctx sync` —
never as a side effect of a hook.

## Why not OpenTelemetry for monitoring

Claude Code's OpenTelemetry export carries token, cost and tool-result events,
but writing them anywhere local needs a running receiver, which reintroduces
the daemon the design avoids. Hooks already see every tool call with its
session, and the captured transcripts carry per-turn detail. OpenTelemetry
remains a possible later source for token and cost figures if the transcript
format stops exposing them.

## Why Python with a stdlib-only hook path

The repository adopts the kit's Python pack, and stages 2 to 4 rely on Python
packages (tree-sitter bindings, sqlite-vec, embedding runtimes). Hooks run on
every lifecycle event, so their cost is interpreter start-up plus imports.
Keeping the hook path to the standard library keeps that within the hook
budget; heavy imports are confined to commands the user or a background worker
runs.

## Why portable from stage 1

Locks, detached workers, launchers and path handling are the parts that differ
by platform. Writing them portably from the start costs little; retrofitting
them after three stages means reworking every component that took a POSIX-only
shortcut. Development happens on macOS with no Windows machine or runner, so
a guard test and platform branches as pure functions catch most portability
regressions on macOS; running on real Windows waits for a VM in the Finalize
stage.
