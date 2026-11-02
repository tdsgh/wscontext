<!-- kit:core/rules/guardrails v1 -->
# Execution guardrails

- Root `CLAUDE.md` § Execution guardrails lists the commands and files in this
  repo that need a human's go-ahead. Ask before each one. Approval for one run
  does not cover the next.
- Never read, edit or print a file matching the repo's secret-shaped gitignore
  patterns (`.env*`, `*.tfstate*`, `*.tfvars`, keys, credentials) unless the
  user asked for that file.
- Prefer a development or throwaway environment over production for anything
  that runs against real infrastructure.
- When a step's real verification is a human action — applying DDL to a live
  database, deploying, clicking through a screen — report the exact command or
  click-through and stop.
- When a code change only takes effect after a rebuild or redeploy, say so.
