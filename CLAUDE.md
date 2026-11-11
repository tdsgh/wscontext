<!-- kit:core/CLAUDE.root v1 -->
# wscontext — orientation for Claude Code

A local harness for Claude Code that captures agent sessions, links them to Git
commits, indexes them alongside a code graph, and gives the agent one search
tool (`ctx`) over both. Stages 1 to 3 are built. Stages 4 and 5 are designed.
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
| Config loader | `wscontext.config.load_config()` |
| Config file | `.wscontext/config.toml` |
| Secret field type | `SecretStr` |
| Test paths | `tests/` |
| Test fakes | `tests/conftest.py` (`git_repo`, `shared_remote`, `run_git`, `put_store_file`, `branch_blob`, `set_identity`); `tests/cli/cli_support.py`, `tests/capture/support.py`, `tests/githooks/githook_support.py`; recorded inputs in `tests/fixtures/` |

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
- Push the history ref only from the `pre-push` hook and `ctx sync`
  (→ ADR-008).
- No POSIX-only calls in application code; shell stays in the plugin launchers
  (`bin/ctx`, `bin/ctx.cmd`) and the Git-hook shims (→ ADR-020, ADR-022).

## Test gates

- Run the full suite once at a build-plan `test` gate.
- When a step changes timing-sensitive code — locks, concurrent writers,
  history merge or compare-and-swap, detached workers, hooks under a time
  budget, retries — also run the changed and affected test files 5 more times
  and name the reason in the gate report.
- A test that fails and then passes on a rerun makes the gate red. Investigate
  it as a flake.
- An agent runs its own layer's tests while working and the full suite once
  before reporting.
- The suite fails on any warning (`filterwarnings = ["error"]`). A run that
  adds a warning is red. Fix a warning at its source; a `filterwarnings` entry
  is narrow and carries a comment naming the reason.

## Command index

| I want to... | See |
|---|---|
| Understand the system as a whole | [docs/architecture.md](docs/architecture.md) |
| Understand what wscontext does for a developer | [docs/what-wscontext-does.md](docs/what-wscontext-does.md) |
| Check what's already been decided | [docs/adr.md](docs/adr.md) |
| Run the tests | `.venv/bin/pytest`; gates in [Test gates](#test-gates) |
| Install the plugin locally | [README.md](README.md) § Install |
| Understand why a decision was taken | [docs/design-rationale.md](docs/design-rationale.md) |
| See the stages and how to run one | [docs/roadmap.md](docs/roadmap.md) |
| Read the current stage's requirements | [docs/wscontext-phase-3.md](docs/wscontext-phase-3.md) |
| Build the current stage | [docs/build-plan-wscontext-phase-3.md](docs/build-plan-wscontext-phase-3.md) via `/kit:run-build-plan` |
| See the pre-Alpha work packages | [docs/build-plan-wscontext-pre-alpha.md](docs/build-plan-wscontext-pre-alpha.md) via `/kit:run-build-plan` |

## Execution guardrails

Ask before:

- `git push` of any ref, including `wscontext/history`.
- Installing, updating or uninstalling the plugin at user scope
  (`claude plugin …`, `/plugin …`).
- Writing anywhere under `~/.claude/`.
- Running `ctx init`, `ctx uninstall` or `ctx sync` against any repository
  other than a throwaway one under a test's `tmp_path`.
- Running a script that spends API usage (acceptance runs, the benchmark).
- Touching anything matching the repo's secret-shaped gitignore patterns.

<!-- wscontext:begin -->
## wscontext

Past agent sessions in this repository are captured and linked to commits.
Before changing code you did not write, run `ctx why <file>:<line>` to see the
session that produced it.
Before starting on a topic, or when asked whether something was done or
discussed before, run `ctx find <words>`.
Before searching for where something is defined, run `ctx code <name>`.
Before changing a function that other code calls, run `ctx impact <name>`.
<!-- wscontext:end -->
