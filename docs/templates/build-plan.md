<!-- kit:core/docs/build-plan v1 -->
# <Area> stage N — build plan

Builds [<area>-phase-N.md](<area>-phase-N.md). Run with `/kit:run-build-plan`.

## Context

## Decisions taken

## Step routing at a glance

| Step | Agent | Model | Gate |
|---|---|---|---|
| WP1 | `<agent>` | sonnet | test |
| WPn | `kit:docs-writer` | sonnet | human |

## WP1 — <title>
> `agent: <agent>` · `model: sonnet` · `gate: test` · `reads: §4.1, §5`

### `<file or module>`

What changes, at file level.

### Tests

## WPn — Docs and rules
> `agent: kit:docs-writer` · `gate: human`

## Verification

## Deliberately out of scope
