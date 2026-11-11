<!-- kit:core/rules/guardrails v2 -->
# Execution guardrails

- Root `CLAUDE.md` § Execution guardrails lists the commands and files in this
  repo that need a human's go-ahead. Ask before each one. Approval for one run
  does not cover the next.
- Never read, edit or print a file matching the repo's secret-shaped gitignore
  patterns (`.env*`, `*.tfstate*`, `*.tfvars`, keys, credentials) unless the
  user asked for that file.
- Prefer a development or throwaway environment over production for anything
  that runs against real infrastructure.
- When a step's verification is a command that needs a go-ahead — applying DDL
  to a live database, deploying, a run that spends API usage — ask, then run
  it yourself. Hand the person only what needs a person: a click-through or a
  judgment call, written so it runs as written.
- A subagent never runs a go-ahead action. It reports the exact command to the
  session that started it, which asks and runs it.
- When a code change only takes effect after a rebuild or redeploy, say so.
