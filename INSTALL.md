# Installing Meristem 0.3.0

## Requirements

- Python 3.11 or newer.
- `git` (Meristem reads history and installs git hooks).
- [uv](https://docs.astral.sh/uv/) (recommended), or pip.
- Optional: the `mcp` extra for the MCP server, the `embed` extra for real
  embeddings (pulls in torch, about 2 GB). Without `embed` a built-in hash
  embedder is used; neither is needed to try Meristem.

## Install the wheel

From this folder:

    uv tool install ./meristem-0.3.0-py3-none-any.whl

With the MCP server extra:

    uv tool install "./meristem-0.3.0-py3-none-any.whl[mcp]"

Or with pip (inside a virtual environment):

    pip install ./meristem-0.3.0-py3-none-any.whl
    pip install "./meristem-0.3.0-py3-none-any.whl[mcp]"

For real embeddings add the `embed` extra the same way, for example
`[mcp,embed]` (large download).

Check it: `meristem --version` prints `meristem 0.3.0`.

## Wire it into your coding agents

    meristem setup --dry-run     # show what would change, write nothing
    meristem setup               # show the plan, ask, apply

`setup` detects Claude Code, Cursor, Windsurf, Codex and Gemini CLI. Claude Code,
Cursor and Windsurf are configured; Codex and Gemini CLI get a snippet to paste.
It is idempotent, never overwrites foreign config keys, and backs a file up once
to `<file>.meristem-bak` before first changing it. Run it inside a repo.

## Index a repo

    cd your-repo
    meristem init --all          # init + ingest + embed
    meristem doctor              # health check

## Team use

Meristem can share memory through git. Clone a repo that already carries a shared
export (`.meristem/atoms.jsonl`), then run `meristem init --all` in the clone:
`init` imports the shared memory into your fresh store and reports the atom count.
See `docs/12-team-memory.md`.

## Status line

    meristem-statusline

Prints the one-line memory pulse. To use it in Claude Code, run
`meristem hooks install --statusline`.

## What's new in 0.3.0

- `meristem setup`: one command wires Meristem into Claude Code, Cursor, Windsurf,
  Codex and Gemini CLI (with `--dry-run`).
- Team onboarding: `meristem init` in a clone imports the shared memory.
- Trust guard: a secret and personal-data scanner keeps such content out of capture
  and export; new `guard.store` check in `meristem doctor`.
- Faster review: agent suggestions shown beside each pending fact, and better
  capture precision.
- Fast `meristem-statusline` (about 75 ms) and a docs site (`mkdocs.yml`, `docs/`).

## Uninstall

- Claude Code hooks: `meristem hooks uninstall`.
- MCP entries: remove the `meristem` key under `mcpServers` in the files `setup`
  reported (or restore the `.meristem-bak` copy).
- Git hooks: delete the `post-commit`, `post-rewrite`, `post-merge` files in
  `.git/hooks` that carry the Meristem marker.
- Local state: delete `.meristem/` and `meristem.toml` in each repo.
- The tool: `uv tool uninstall meristem` (or `pip uninstall meristem`).
