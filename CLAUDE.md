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
| Config loader | TODO(kit): the function the harness calls to load settings |
| Config file | `.localAIharness/config.toml` |
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

- TODO(kit): the repo-wide rules. Tag the ones no subtree may override
  *(non-negotiable)*.

## Command index

| I want to... | See |
|---|---|
| Understand the system as a whole | [docs/architecture.md](docs/architecture.md) |
| Check what's already been decided | [docs/adr.md](docs/adr.md) |
| TODO(kit): one row per task | the README that owns it |

## Execution guardrails

Ask before:

- TODO(kit): every command that touches shared or real infrastructure —
  deploys, `docker push`, `terraform apply`, migrations against a live
  database, `git push`.
- Touching anything matching the repo's secret-shaped gitignore patterns.
