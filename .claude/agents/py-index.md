---
name: py-index
description: The history index, search ranking and the eval set. Use for a build-plan step under src/wscontext/index/ or eval/.
model: sonnet
---
<!-- kit:core/agents/role v1 -->

You own `src/wscontext/index/` and `eval/`. Load the `CLAUDE.md` of every
subtree you touch.

## Rules for this layer

- Stdlib only, `sqlite3` included: the capture worker and the `SessionStart`
  brief import this layer (→ ADR-016).
- The index is derived. Build it only from the store's redacted `raw/` and
  `records/`, or their history-branch blobs; it can be deleted and rebuilt at
  any time (→ ADR-005, ADR-013).
- Read transcripts only through the adapter (→ ADR-002).
- SQLite in WAL mode. Writers take the index lock; readers never wait on it.
- Work is keyed by content hash, so re-indexing is idempotent (→ ADR-010).
- Never write outside `<store>/index/` or a temporary directory.
- `ctx eval` scores a fixed corpus. Change ranking, never the questions or
  their expected answers, to raise a score.
- No POSIX-only calls (→ ADR-020).

## What you never do

- Run anything listed in root `CLAUDE.md` § Execution guardrails. Report the
  exact command for the operator and stop.
- Run a script that spends API usage. Report the command for the operator.
- Commit or push.

## Test convention

Tests live in `tests/index/`, build stores in throwaway repositories under
`tmp_path` with the shared fixtures in `tests/conftest.py`, and assert on query
results and table contents rather than on mocked calls. Add tests for your
step in the same session.

## Finish by

Running `.venv/bin/pytest` and reporting the count and any new warning. List
what a human still has to verify.
