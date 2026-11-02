<!-- kit:core/rules/docs-gate v1 -->
# System docs

- `docs/` documents the system as a whole: what the system is
  (`docs/architecture.md`) and what was decided (`docs/adr.md`).
  How to write them is in `docs/CLAUDE.md`.
- Treat both as living. When a change alters the system's shape, adds a
  decision, or invalidates something recorded there, say so and propose the
  edit.
- Never write to `docs/` without explicit human approval. Listing the
  docs edits in an approved plan counts as approval.
- Outside `docs/`, when a change makes a `.md` file inaccurate, name the
  file and the stale line and wait for approval. Never auto-fix it.
