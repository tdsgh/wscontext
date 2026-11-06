---
name: init
description: Use when the user asks to set up, enable, initialize or check wscontext in a repository, or when `ctx why` reports that nothing is captured because the repository is not set up. Runs `ctx init` and `ctx doctor` and explains the result.
---

# init

`ctx init` changes the repository, so run it only when the user asked for it in
this repository.

1. Run `ctx init`. It creates `.wscontext/config.toml`, chains the Git hooks
   after any existing ones, and adds a short wscontext block to `CLAUDE.md`. Running it again
   replaces the block in place, which is how an older block is upgraded.
2. Run `ctx doctor` and explain each line. Every failure comes with its fix.
3. Tell the user to commit `.wscontext/config.toml` and the `CLAUDE.md` block.
4. If the user wants the whole team to get the plugin, tell them to add it to
   the repository's committed `.claude/settings.json` themselves:
   `"enabledPlugins": {"wscontext@wscontext": true}`, plus an
   `extraKnownMarketplaces` entry for the `wscontext` marketplace with the
   source teammates install from. wscontext never edits that file. Each
   teammate still runs `ctx init` in their clone.

Reading the output:
- `existing hooks kept and chained`: the user's own hooks still run first.
- `not managed by wscontext` with lines to copy: the repository's hooks live in
  a shared directory; the user adds each line by hand.
- `not installed, an original is already parked`: resolve the old
  `<hook>.pre-wscontext` file by hand, then rerun.

Related commands:
- `ctx sync --setup [--remote NAME]` turns on sharing through a Git remote
  name, not a URL. Nothing is shared before this.
- `[retrieval] enabled = false` in `.wscontext/config.toml` turns the brief and
  `ctx find` off; run `ctx init` again so the block drops its `ctx find` line.
- `ctx uninstall` removes the hooks and the block and keeps captured data.
