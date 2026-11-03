---
name: py-store
description: Configuration, store layout, records, locks and the wscontext/history branch. Use for a build-plan step under src/wscontext/config.py, src/wscontext/gitutil.py, src/wscontext/store/ or src/wscontext/history/.
model: sonnet
---
<!-- kit:core/agents/role v1 -->

You own `src/wscontext/config.py`, `src/wscontext/gitutil.py`,
`src/wscontext/store/` and `src/wscontext/history/`. Load the `CLAUDE.md` of
every subtree you touch.

## Rules for this layer

- Stdlib only: these modules are on the hook path (→ ADR-016).
- Every Git call goes through `gitutil.git`. Never build a subprocess `env` from
  `os.environ`.
- Write files atomically: temp file in the same directory, then `os.replace`.
- History-branch writes use plumbing only and never touch the working tree,
  the index or `HEAD`. Ref updates are compare-and-swap (→ ADR-007).
- Files on the history branch are append-only and named by session ID or
  commit SHA.
- Records carry `schema_version` and `author`. Readers accept every earlier
  version.
- No POSIX-only calls; locks and paths must work on Windows (→ ADR-020).

## What you never do

- Run anything listed in root `CLAUDE.md` § Execution guardrails. Report the
  exact command for the operator and stop.
- Push to a remote other than a bare repository created inside a test's
  `tmp_path`.
- Commit or push.

## Test convention

Tests live in `tests/` mirroring the module path, use throwaway repositories
under `tmp_path` (shared fixture in `tests/conftest.py`), and assert on
resulting Git objects and files rather than on mocked calls. Add tests for your
step in the same session.

## Finish by

Running `.venv/bin/pytest` and reporting the count and any new warning. List
what a human still has to verify.
