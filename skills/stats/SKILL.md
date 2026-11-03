---
name: stats
description: Use when the user asks how wscontext is doing, how much was captured, whether hooks are healthy, how many turns or tool calls a session took, or how often `ctx` was used. Runs `ctx stats` and explains the report.
---

# stats

Run `ctx stats [view] [--since 7d]` and explain the result in plain terms.

Views:
- `data` (default): sessions, linked commits and the share of commits that are
  linked, store size, redaction hits, sharing status.
- `turns`: turns, turn duration and tool calls by kind, per session and over
  time.
- `queries`: each `ctx` call with its result count, answer size, latency, and
  whether it came before the first search or read.
- `hooks`: Claude Code and Git hook runs, failures and latency per event.
- `session <id>`: one session in detail.

Add `--json` for machine-readable output. A failure count above zero in
`hooks` means data is missing for those runs; suggest `ctx doctor`.
