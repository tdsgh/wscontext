# wscontext

A local harness for Claude Code that captures agent sessions, links them to Git
commits, and answers why a line of code exists. Stage 1 of
[docs/roadmap.md](docs/roadmap.md) is built: capture, commit linking, history
sharing, `ctx why/sessions/show/forget`, `ctx stats`. Architecture:
[docs/architecture.md](docs/architecture.md).

Prerequisites: Git, Claude Code with plugin support, [uv](https://docs.astral.sh/uv/).

## Install

    claude plugin marketplace add <path to this clone>
    claude plugin install wscontext@wscontext

The marketplace and the plugin must both be named `wscontext`. A local-folder
marketplace runs the plugin straight from that folder; code edits take effect
only after `.claude-plugin/plugin.json` changes (bump the version). The first
session builds the plugin's environment in the data directory; the build runs in
the background and hooks never wait for it.

`ctx` is on `PATH` only inside Claude Code's Bash tool and `!` commands. From a
terminal, use `~/.claude/plugins/data/wscontext-wscontext/bin/ctx` (written by
`ctx init`).

## Initialize a repository

Ask Claude to set up wscontext in the repository (the `init` skill runs
`ctx init` and `ctx doctor`), or run `ctx init` from Claude Code's Bash tool.
It creates `.wscontext/config.toml`, chains the Git hooks after any existing
ones, and adds a short block to `CLAUDE.md`. Commit the config file and the block.

To give the whole team the plugin, add `enabledPlugins` and
`extraKnownMarketplaces` to the committed `.claude/settings.json` yourself;
wscontext never edits it. Each teammate still runs `ctx init`.

`ctx doctor` checks the setup. `ctx uninstall` removes the hooks and the block
and keeps captured data.

## Share history (optional)

    ctx sync --setup --remote origin    # a Git remote name, not a URL
    ctx sync --status                   # ahead and behind
    ctx sync --dry-run                  # sessions a push would send
    ctx sync                            # push now

Nothing is shared until `--setup`. After that history goes out with your own
`git push` (the `pre-push` hook) and on `ctx sync`. Commit the changed
`.wscontext/config.toml` so teammates inherit the remote. A fresh clone plus
`ctx init` recovers shared history.

## Statistics

    ctx stats [data|turns|queries|hooks|session <id>] [--since 7d] [--json]

## Developing

- `uv sync`, then `.venv/bin/pytest` (see [CLAUDE.md](CLAUDE.md) § Test gates).
- To test a plugin update, use a Git-backed marketplace: a local
  `marketplace.json` whose plugin source is
  `{"source": "url", "url": "file:///.../wscontext.git"}`. It installs into
  `plugins/cache/<marketplace>/wscontext/<version>` like a user install.
  `file://` as the marketplace URL itself is rejected.
- `CLAUDE_PLUGIN_*` variables are not set in the Bash tool; the launcher falls
  back to the data directory above.
- Uninstalling the plugin does not stop hooks in sessions already running.
