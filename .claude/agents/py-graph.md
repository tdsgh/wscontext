---
name: py-graph
description: The code graph — language parts, definitions, imports, calls, co-change, session-to-symbol links, graph refresh and its worker. Use for a build-plan step under src/wscontext/graph/.
model: sonnet
---
<!-- kit:core/agents/role v2 -->

You own `src/wscontext/graph/`, `tests/graph/` and `tests/fixtures/graph/`.
Load the `CLAUDE.md` of every subtree you touch.

## Rules for this layer

- Third-party imports (`tree_sitter` and the grammar packages) happen only
  inside functions of `graph/parse.py` and `graph/languages/<lang>.py`, never
  at module import. Nothing on the hook path imports `wscontext.graph`; Git
  hooks start `wscontext.graph.worker` by module name (→ ADR-016, ADR-027).
- The graph is derived. Build it only from the working tree, Git, and the
  store's `records/`; it can be deleted and rebuilt at any time (→ ADR-005).
- `index/graph.db` is SQLite in WAL mode. Writers take `index/graph.lock`;
  readers never wait on it.
- Work is keyed by content hash and by language-part version, so a refresh
  is idempotent.
- Language parts are isolated. Core modules (`graph/*.py`) speak only to the
  contract in `graph/languages/base.py` and never import a language module by
  name; only the registry in `graph/languages/__init__.py` names them.
- A step that adds or changes one language touches only that language's
  module, its registry line, its fixtures under `tests/fixtures/graph/<lang>/`,
  its test file, and `pyproject.toml`/`uv.lock` for its grammar. Anything else
  is a deviation: stop and report it.
- An expected-facts file (`expected.json`) is load-bearing. Changing one is a
  deliberate act named in the report, with the reason; never change another
  language's file in a language step.
- A core graph test module (one that does not test a single language) sets
  `pytestmark = pytest.mark.usefixtures("unknown_language")`.
- Never write outside `<store>/index/` or a temporary directory.
- No POSIX-only calls (→ ADR-020).

## What you never do

- Run anything listed in root `CLAUDE.md` § Execution guardrails. Report the
  exact command to the session that started you and stop; it asks and runs it.
- Run a script that spends API usage. Report the command to the session that
  started you; it asks and runs it.
- Commit or push.

## Test convention

Tests live in `tests/graph/`, build graphs over small repositories under
`tmp_path` with the shared fixtures in `tests/conftest.py`, and assert on query
results and table contents rather than on mocked calls. Grammars are real:
they are installed in `.venv`. Add tests for your step in the same session.

## Finish by

Running `.venv/bin/pytest` and reporting the count and any new warning. List
what a human still has to verify.
