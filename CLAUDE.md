<!-- kit:core/CLAUDE.root v1 -->
# wscontext — orientation for Claude Code

A local harness for Claude Code that captures agent sessions, links them to Git
commits, indexes them alongside a code graph, and gives the agent one search
tool (`ctx`) over both. Design stage, no code yet.
The system itself is described in
[docs/architecture.md](docs/architecture.md).

This file owns the rules that hold everywhere in this repo. Subtrees
(none yet) each have a `CLAUDE.md` that adds rules for that subtree and does
not repeat these. Precedence and conflict handling:
[.claude/rules/kit-precedence.md](.claude/rules/kit-precedence.md).

## Project facts

Skills and agents read these by slot name. Change a value freely; keep the slot
names.

| Slot | Value |
|---|---|
| Docs dir | `docs/` |
| Build plans | `docs/build-plan-*.md` |
| Test command | `.venv/bin/pytest` |
| Subtrees | none yet |
| Python version | `3.13` |
| Venv | `.venv` |
| Config loader | `wscontext.config.load_config()` (planned, stage 1 WP1) |
| Config file | `.wscontext/config.toml` |
| Secret field type | `SecretStr` |
| Test paths | `tests/` |
| Test fakes | TODO(kit): shared fakes and helpers, none yet |

## Non-negotiable rules

The bullets tagged **(non-negotiable)** under Cross-cutting rules below and in
`.claude/rules/kit-*.md`, plus everything under Execution guardrails. They hold
in every subtree and no subtree file can override them.

## Cross-cutting rules

Rules here apply repo-wide. Where a subtree or an ADR owns the details, point at
it instead of restating them — `→ ADR-0NN` refers to
[docs/adr.md](docs/adr.md).

- Code reachable from a hook entrypoint imports only the standard library and
  always exits 0 within its time budget (→ ADR-009, ADR-016). *(non-negotiable)*
- Everything written to the store or a log passes through the redactor first
  (→ ADR-006). *(non-negotiable)*
- Read Claude Code transcripts only through an adapter (→ ADR-002).
- Push the history branch only from the `pre-push` hook and `ctx sync`
  (→ ADR-008).
- No POSIX-only calls in application code; shell stays in one-line launcher and
  Git-hook shims (→ ADR-020).

## Command index

| I want to... | See |
|---|---|
| Understand the system as a whole | [docs/architecture.md](docs/architecture.md) |
| Understand what wscontext does for a developer | [docs/what-wscontext-does.md](docs/what-wscontext-does.md) |
| Check what's already been decided | [docs/adr.md](docs/adr.md) |
| Understand why a decision was taken | [docs/design-rationale.md](docs/design-rationale.md) |
| See the stages and how to run one | [docs/roadmap.md](docs/roadmap.md) |
| Read the current stage's requirements | [docs/wscontext-phase-1.md](docs/wscontext-phase-1.md) |
| Build the current stage | [docs/build-plan-wscontext-phase-1.md](docs/build-plan-wscontext-phase-1.md) via `/kit:run-build-plan` |

## Execution guardrails

Ask before:

- `git push` of any ref, including `wscontext/history`.
- Installing, updating or uninstalling the plugin at user scope
  (`claude plugin …`, `/plugin …`).
- Writing anywhere under `~/.claude/`.
- Running `ctx init`, `ctx uninstall` or `ctx sync` against any repository
  other than a throwaway one under a test's `tmp_path`.
- Touching anything matching the repo's secret-shaped gitignore patterns.
