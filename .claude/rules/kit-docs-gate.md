<!-- kit:core/rules/docs-gate v3 -->
# System docs

- `docs/` documents the system as a whole: what the system is
  (`docs/architecture.md`) and what was decided (`docs/adr.md`).
  How to write them is in `docs/CLAUDE.md`.
- Treat both as living. When a change alters the system's shape, adds a
  decision, or invalidates something recorded there, say so and write the
  edit for review.
- Write `docs/` edits in place, uncommitted, for review in the diff. Never
  commit them without the human's approval of that commit.
- When a change makes a `.md` file inaccurate, inside or outside `docs/`,
  write the fix in place, uncommitted, and name the file and line in the
  report.
