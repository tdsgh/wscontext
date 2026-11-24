# wscontext

A local harness for Claude Code that captures agent sessions, links them to Git
commits, and answers why a line of code exists. Stages 1 to 3 and the import
stage of [docs/roadmap.md](docs/roadmap.md) are built: capture, commit linking, history
sharing, history search, the session brief, the code graph,
`ctx why/sessions/show/forget`, `ctx find/brief`, `ctx code/impact`,
`ctx import`, `ctx stats`. Architecture:
[docs/architecture.md](docs/architecture.md).

Prerequisites: Git, Claude Code with plugin support, [uv](https://docs.astral.sh/uv/).

## Install

    claude plugin marketplace add <path to this clone>
    claude plugin install wscontext@wscontext

The marketplace and the plugin must both be named `wscontext`. A local-folder
marketplace runs the plugin straight from that folder; code edits take effect
only after `.claude-plugin/plugin.json` changes (bump the version). The first
session builds the plugin's environment in the data directory; the build runs in
the background and hooks never wait for it. A session that is already running
keeps the old plugin until you run `/reload-plugins` or restart the session.

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
`ctx init` recovers shared history. Shared history is stored at
`refs/wscontext/history`. This ref is not a branch, so a plain clone does not
download it.

wscontext fetches and pushes history in background Git processes. These
processes cannot ask you for a credential. Use a credential that works without
a prompt: an SSH key loaded in the SSH agent, or a Git credential helper.
Otherwise fetches and pushes fail, and `ctx doctor` warns.

## Search past work

    ctx find <words> [--branch B] [--author A] [--since 7d] [--file F] [--kind session|commit|document]
    ctx import                 # preview past sessions and documents; --offer TOKEN --all|--take 1,3 imports
    ctx brief                  # the brief a session starting now would get
    ctx rebuild                # delete the search index and rebuild it
    ctx eval [--corpus DIR] [--questions FILE] [--min N]   # score search on the fixed corpus

A session gets the brief when a new session starts, after `/clear` and after
compaction. A resumed session does not get it.

The first prompt of a session also gets a prompt brief: up to 3 past sessions
that match the words of the prompt. If no past session matches well, there is
no prompt brief. Later prompts in the same session get none. `ctx brief` shows
the session brief only.

Best practice: the prompt brief is built from the first prompt only. Keep a
session on the topic of its first prompt. Start a new session, or use `/clear`,
for a new topic.

Setting `[retrieval] enabled = false` in `.wscontext/config.toml` turns off the
brief, the prompt brief, `ctx find`, `ctx code` and `ctx impact`. Then run
`ctx init` again, so that the CLAUDE.md block no longer has their lines.
`ctx why` keeps working. `brief = false` turns off only the session brief.
`prompt_brief = false` turns off only the prompt brief.

## Import past work

`ctx import` shows Claude Code sessions of this repository that are still on
disk, and Markdown files that look like decisions or design. `ctx init` in a
repository with past work prints how much it found.

    ctx import                                  # preview; writes nothing, prints a token
    ctx import --offer TOKEN --all              # import everything offered
    ctx import --offer TOKEN --sessions         # only sessions (or --documents)
    ctx import --offer TOKEN --take 1,3,5-7     # chosen item numbers
    ctx import --offer TOKEN --decline 2,4      # never offer these again
    ctx import --declined                       # preview with declined items marked

- Every form that writes needs the token of a fresh preview. If the list
  changed since, the command asks you to run the preview again.
- Sessions are read from `$CLAUDE_CONFIG_DIR/projects`, or `~/.claude/projects`
  when that variable is not set. Claude Code deletes transcripts after
  `cleanupPeriodDays` (30 by default); the preview says when the oldest one goes.
- A session in use right now, and one already captured, is not offered.
- A commit is linked to an imported session only when that session ran the
  `git commit` and its result names the commit. `git commit -q` and commits
  that carry an `AI-Session` trailer stay unlinked.
- Chosen documents go into `[documents] paths` in `.wscontext/config.toml`.
  Commit that file. `ctx find --kind document` searches them. Documents never
  appear in the briefs.
- Nothing is pushed by the import. With sync set up, imported sessions go out
  with the next `git push` or `ctx sync`.
- `ctx forget` removes a session but does not decline it. If its transcript
  is still on disk, the next `ctx import` offers it again; decline it there.

## Search code

    ctx code <name> [--kind function|class|method|type] [--limit N] [--full]   # where a name is defined
    ctx code <path>                    # a file's definitions, imports and importers
    ctx impact <target> [--depth 1|2|3] [--limit N]   # callers, tests, importers, co-changed files, sessions
    ctx why <file>#<name>              # also #<name> and <file>#Class.method
    ctx rebuild [--index | --graph]    # delete and rebuild the search index, the code graph, or both

`<target>` is a file, `FILE#NAME`, `FILE#Class.method`, `#NAME` or `NAME`. Callers
are matched by name, so `ctx impact` says how many definitions share the name.
Every graph command first brings the graph up to the working tree. An answer
may carry `graph-busy` or `graph-incomplete`; rerun it in a moment.

The graph reads Python, TypeScript, JavaScript and Rust. These `[graph]` settings are in
`.wscontext/config.toml`:

- `languages`: the languages switched on. A new `ctx init` writes all four. It
  keeps an existing config, so a repository set up earlier adds `"rust"` to
  this list by hand. `ctx doctor` shows the line to add.
- `exclude`: glob patterns on repository paths left out of the graph.
- `query_budget_ms`: the longest a command waits for the graph.
- `cochange_commits`: how many recent commits count toward files that change
  together.

The benchmark compares retrieval off and on over ten tasks. It spends API usage.

    .venv/bin/python acceptance/benchmark.py --list           # the tasks
    .venv/bin/python acceptance/benchmark.py --verify-checks  # offline
    .venv/bin/python acceptance/benchmark.py [--runs N] [--max-budget-usd X]

Each command takes `--subject DIR`, a folder with the tasks, checks and
reports. The default subject is `eval/bench/`.
## Retention

Raw transcripts older than `retention.raw_days` (default 180) are removed from
the local store, but only after they are on the history ref. Session and commit
records stay. The sizes that trigger a warning are set by
`store.session_warn_bytes` and `store.store_warn_bytes`. Tool results are kept
whole unless you set `capture.tool_result_max_chars`, which cuts each result at
that many characters. All defaults are in `.wscontext/config.toml`.

## Statistics

    ctx stats [data|turns|queries|hooks|code|session <id>] [--since 7d] [--json]

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
