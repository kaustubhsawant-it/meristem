# 11. Install and setup

How to get Meristem onto a machine and wired into the coding agents on it.
The short version:

```bash
uvx meristem setup        # detect agents, wire Meristem into them
meristem init --all       # index this repo (init + ingest + embed)
```

## Requirements

- Python 3.11 or newer (the CI matrix covers 3.11 to 3.14).
- `git`. Meristem reads history and installs git hooks; a directory that is not
  a git repo works for the store but gets no drift tracking.
- SQLite 3.38 or newer, which every supported Python on a current OS ships.
  The schema uses `STRICT` tables.
- For the MCP server: the `mcp` extra (`meristem[mcp]`).
- Optional: the `embed` extra (`sentence-transformers`, pulls in torch, about 2 GB)
  for real embeddings, and the `vec` extra (`sqlite-vec`) for an ANN index. Without
  `embed` a deterministic hash embedder is used: it works, retrieval just has less
  semantic reach. Neither is needed to try Meristem.

Windows: the suite and a scale benchmark are being made portable, and CI has a
Windows leg, but that leg is advisory and **has never run on a real runner**.
Treat Windows as untested. The managed git hooks are POSIX `sh` scripts.

## Zero-install: `uvx`

If you have [uv](https://docs.astral.sh/uv/), nothing needs installing first:

```bash
uvx meristem setup
```

`uvx` fetches Meristem into a throwaway environment and runs the command. This
needs the package to be available from a package index you can reach; from a
source checkout use `uv tool install .` or `pip install -e .` instead and run
`meristem setup`.

When `setup` finds that `meristem` is not on your `PATH` but `uvx` is, the MCP
entry it writes launches the server through `uvx --from 'meristem[mcp]' meristem mcp`,
so the zero-install path keeps working for the agent too. Otherwise the entry is
plain `meristem mcp`.

## `meristem setup`

```bash
meristem setup --dry-run             # print the plan, write nothing
meristem setup                       # show the plan, ask, apply
meristem setup --yes                 # apply without asking
meristem setup --agents claude,cursor   # only these (default: whatever is detected)
```

`setup` detects which coding agents are present by looking for each one's
config directory under your home directory or its binary on `PATH`, plans the
changes, prints them, and applies them after you confirm. `--dry-run` computes
the identical plan and writes nothing.

Run it from inside a repo that already has a store (`meristem init` first, or
`meristem init --all`). Outside one, the project-scoped steps and the git hooks
are skipped with a note telling you to run `meristem init` first.

### What each agent gets

| Agent | Detected by | What `setup` does |
|---|---|---|
| Claude Code | `~/.claude` or `claude` on PATH | **Written.** Installs the five lifecycle hooks into the project's `.claude/settings.json` (same as `meristem hooks install`) and adds `mcpServers.meristem` to the project `.mcp.json`. |
| Cursor | `~/.cursor` or `cursor` | **Written.** Adds `mcpServers.meristem` to the project's `.cursor/mcp.json`. |
| Windsurf | `~/.codeium/windsurf` or `windsurf` | **Written.** Adds `mcpServers.meristem` (with `cwd` set to this repo, because Windsurf's config is global) to `~/.codeium/windsurf/mcp_config.json`. |
| Codex | `~/.codex` or `codex` | **Snippet only.** Prints a TOML block to paste into `~/.codex/config.toml`. |
| Gemini CLI | `~/.gemini` or `gemini` | **Snippet only.** Prints a JSON block to paste into `~/.gemini/settings.json`. |

Codex and Gemini get snippets because their config formats are not documented in
this repository, and `setup` never guesses a format: an agent is written to only
when the format is one the project already documents (the JSON `mcpServers`
shape). Other MCP clients (Claude Desktop, Cline, Continue) are not detected;
see `examples/mcp/` for their config locations.

Independent of the agent list, when run in a workspace `setup` also installs the
sync git hooks (`post-commit`, `post-rewrite`, `post-merge`), exactly what
`meristem sync --install-hook` does. A hook file that Meristem did not write is
left alone and the line to add by hand is printed.

Only Claude Code gets the ambient layer (digest, per-turn injection, liveness
watcher, handoff, statusline, skills). Every other agent gets the MCP tools and
calls them explicitly.

### Safety rules

- **Idempotent.** Re-running reports `unchanged` for anything already in place.
- **Merge, never overwrite.** Only the `mcpServers.meristem` key is added. An
  existing `meristem` entry that differs from the one `setup` would write is left
  alone and reported (`left alone`), with the entry printed so you can compare.
- **A file that is not valid JSON is not touched.** You get the snippet to add by hand.
- **Backups.** Before the first change to an existing file, `setup` copies it to
  `<file>.meristem-bak` (for example `.mcp.json.meristem-bak`). An existing backup is
  never replaced, so the backup is always the file as it was before Meristem first
  touched it.

## Uninstall

`setup` has no `--undo`; removal is manual and small.

- Claude Code hooks: `meristem hooks uninstall` (add `--global` if you installed there).
- MCP entries: delete the `meristem` key under `mcpServers` in the files `setup`
  reported, or restore the `<file>.meristem-bak` copy if you have not changed
  the file since.
- Git hooks: delete the `post-commit`, `post-rewrite`, `post-merge` files under the
  repo's hooks directory that carry the Meristem marker comment (and
  `post-checkout` if you ran `meristem import --install-hook`).
- Local state: delete the `.meristem/` directory and `meristem.toml`. The store is
  local and gitignored; the shared export, if you use team sync, is a separate
  committed file.
- The tool itself: `uv tool uninstall meristem` or `pip uninstall meristem`.

## After install

- `meristem init --all` chains `init`, `ingest` and `embed`.
- `meristem doctor` checks that everything is wired and healthy.
- A teammate's clone can pick up shared memory in the same step; see
  [Team memory](12-team-memory.md).
