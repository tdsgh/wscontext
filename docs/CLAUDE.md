<!-- kit:core/docs/CLAUDE v1 -->
# docs/ — system documentation rules

Adds to the root `CLAUDE.md` and does not repeat it. The approval gate on this
folder is in `.claude/rules/kit-docs-gate.md`.

## What lives here

| File | Holds | Lifecycle |
|---|---|---|
| `architecture.md` | What the system is | Edited in place; always describes now |
| `adr.md` | What was decided | Append-only |
| `<area>-phase-N.md` | What one stage of one area will build | Edited until that stage is built, then left alone |
| `build-plan-<area>-phase-N.md` | The file-level technical design that builds one `<area>-phase-N.md` | Edited until that stage is built, then left alone |
| `templates/` | The shapes new stage and build-plan documents start from | Kit-managed |
| `hist/<name>.md` | A stage or build-plan document whose stage is fully built and superseded | Left alone |
| anything else | Extended reasoning for one decision | Written once, rarely touched |

- A new stage document starts as a copy of `templates/feature-phase.md`; a new
  build plan as a copy of `templates/build-plan.md`. Drop the `<!-- kit: -->`
  marker in the copy.
- A stage document holds requirements at capability level. Technical design is
  left out; findings from writing it go in its closing notes section.
- A build plan holds the design the stage document withholds, as ordered work
  packages, each tagged with the agent that implements it and the gate before
  the next one starts. `/kit:run-build-plan` executes it.

## Archiving to hist/

Move a superseded stage or build-plan document with `git mv
docs/<file> docs/hist/<file>`. Before moving, grep the repo for the
filename and update every link to the `hist/` path in the same change.
`architecture.md` and `adr.md` never move.

## How to write

Declarative and scannable. Read start to finish once, then jumped into later
for a single entry. Written for whoever is about to change the system.

- **State what, not why.** Present tense. What exists, what was decided.
- **Carry implications, not rationale.** The constraints that follow from a
  decision, not the debate that produced it.
- **Extended reasoning gets its own file**, linked from the entry it explains.
- **Link, don't duplicate.** Point at `file:line`, `CLAUDE.md` rules and READMEs.
- **No length limit.**

## ADR entries

Numbered and append-only. A reversed decision gets a **new** entry; the old one
is marked `Superseded by ADR-NNN` and left in place — never rewritten, never
renumbered.

```markdown
### ADR-0NN — Short title, stated as a decision

**Decision.** What is true now. One or two sentences.

**Implications.** What follows from it. Bullets when there is more than one.

**Guardrail.** Optional, one line. Only where the bare statement would read as
an oversight someone might helpfully "fix".

**Reasoning.** Optional link to a sibling doc.
```

Most entries are `Decision` + `Implications` alone.

## The seam between the two files

`architecture.md` states facts; `adr.md` states choices. Where architecture
describes a deliberate decision, it links `→ ADR-0NN` instead of re-explaining
it. Anything stated in full in both files is a bug in the split.

## Accuracy

Verify every claim against source before writing and cite `file:line` so it can
be rechecked. Prefer omitting a detail to asserting a shaky one. Values that
drift — addresses, versions, counts — belong in the code or config that owns
them.
