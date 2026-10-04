"""`meristem setup` — detect installed coding agents and wire Meristem in.

Pure planning/applying logic; `cli.py` owns the console output and the git
hook installer (passed in, to keep this module free of a circular import).

Safety rules, all enforced here rather than left to callers:

* Never guess a config format. An agent is *written* only when its format is
  documented in this repo (README "Connecting other agents",
  `examples/mcp/`) — the JSON `mcpServers` shape. Every other agent gets a
  printed snippet and an "add this to <path>" line.
* Merge, never overwrite: only the `mcpServers.meristem` key is ever added.
  An existing `meristem` entry that differs from ours is left alone.
* A file that already exists is copied once to `<file>.meristem-bak` before its
  first modification (an existing backup is never replaced).
* A file that is not valid JSON is not touched.
* `dry_run=True` computes the identical plan and writes nothing.
* Importing this module does no I/O.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import hooks as hooks_mod

BACKUP_SUFFIX = ".meristem-bak"

AGENTS = ("claude", "cursor", "codex", "windsurf", "gemini")

# agent -> (config dirs relative to HOME, binary names on PATH)
_DETECT: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "claude": ((".claude",), ("claude",)),
    "cursor": ((".cursor",), ("cursor",)),
    "codex": ((".codex",), ("codex",)),
    "windsurf": ((".codeium/windsurf",), ("windsurf",)),
    "gemini": ((".gemini",), ("gemini",)),
}

# Outcomes. "added"/"updated" were (or, in a dry run, would be) written.
WRITTEN = ("added", "updated")


@dataclass(frozen=True)
class Step:
    agent: str  # an AGENTS name, or "git"
    target: str  # path, shown to the user
    outcome: str  # added | updated | unchanged | snippet | left alone | skipped
    note: str = ""
    snippet: str = ""

    @property
    def changes(self) -> bool:
        return self.outcome in WRITTEN


def detect_agents(
    home: Path, *, which: Callable[[str], str | None] = shutil.which
) -> list[str]:
    """Agents whose config dir exists under `home`, or whose binary is on PATH."""
    found = []
    for name in AGENTS:
        dirs, bins = _DETECT[name]
        if any((home / d).is_dir() for d in dirs) or any(which(b) for b in bins):
            found.append(name)
    return found


def server_entry(*, which: Callable[[str], str | None] = shutil.which) -> dict[str, Any]:
    """The `mcpServers.meristem` value. Plain `meristem mcp` when installed;
    through `uvx` when only uv is available (the zero-install path)."""
    if which("meristem") or not which("uvx"):
        return {"command": "meristem", "args": ["mcp"]}
    return {"command": "uvx", "args": ["--from", "meristem[mcp]", "meristem", "mcp"]}


def json_snippet(entry: dict[str, Any]) -> str:
    return json.dumps({"mcpServers": {"meristem": entry}}, indent=2)


def codex_snippet(entry: dict[str, Any]) -> str:
    args = ", ".join(json.dumps(a) for a in entry["args"])
    return (
        "[mcp_servers.meristem]\n"
        f"command = {json.dumps(entry['command'])}\n"
        f"args = [{args}]\n"
    )


def _backup_once(path: Path) -> None:
    bak = path.with_name(path.name + BACKUP_SUFFIX)
    if path.exists() and not bak.exists():
        shutil.copy2(path, bak)


def _merge_mcp_json(path: Path, agent: str, entry: dict[str, Any], dry_run: bool) -> Step:
    """Add `mcpServers.meristem` to the JSON file at `path`."""
    target = str(path)
    data: dict[str, Any] = {}
    existed = path.exists()
    if existed:
        text = path.read_text(encoding="utf-8")
        if text.strip():
            try:
                loaded = json.loads(text)
            except ValueError:
                return Step(
                    agent, target, "snippet",
                    "existing file is not valid JSON, not touched — add this to it by hand",
                    json_snippet(entry),
                )
            if not isinstance(loaded, dict):
                return Step(
                    agent, target, "snippet",
                    "existing file is not a JSON object, not touched — add this to it by hand",
                    json_snippet(entry),
                )
            data = loaded
    servers = data.get("mcpServers")
    if servers is None:
        servers = data["mcpServers"] = {}
    if not isinstance(servers, dict):
        return Step(
            agent, target, "snippet",
            "`mcpServers` is not an object, not touched — add this to it by hand",
            json_snippet(entry),
        )
    current = servers.get("meristem")
    if current == entry:
        return Step(agent, target, "unchanged", "mcpServers.meristem already present")
    if current is not None:
        return Step(
            agent, target, "left alone",
            "an existing mcpServers.meristem entry differs from ours; not overwritten",
            json_snippet(entry),
        )
    servers["meristem"] = entry
    if not dry_run:
        _backup_once(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    note = "add mcpServers.meristem" + (" (new file)" if not existed else "")
    return Step(agent, target, "added", note, json.dumps(entry))


def _claude_hooks(settings: Path, dry_run: bool) -> Step:
    target = str(settings)
    changes = hooks_mod.install_hooks(settings_path=settings, dry_run=True)
    dirty = [c.event for c in changes if c.outcome in ("installed", "updated", "set")]
    if not dirty:
        return Step("claude", target, "unchanged", "Claude Code hooks already installed")
    if not dry_run:
        _backup_once(settings)
        hooks_mod.install_hooks(settings_path=settings, dry_run=False)
    return Step("claude", target, "added", "hooks: " + ", ".join(dirty))


def run(
    *,
    home: Path,
    workspace: Path | None,
    agents: list[str],
    dry_run: bool,
    git_hooks: Callable[[bool], list[Step]] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> list[Step]:
    """Plan (and, unless `dry_run`, apply) setup for `agents`.

    `workspace` is the current meristem workspace root, or None when the
    current directory has no store — project-scoped files are then skipped and
    the caller is told to run `meristem init`. `git_hooks(dry_run)` installs
    the sync git hooks and returns steps for them.
    """
    entry = server_entry(which=which)
    steps: list[Step] = []
    for agent in agents:
        if agent == "claude":
            if workspace is None:
                steps.append(Step("claude", "(project)", "skipped", NEEDS_INIT))
                continue
            steps.append(_claude_hooks(workspace / ".claude" / "settings.json", dry_run))
            steps.append(_merge_mcp_json(workspace / ".mcp.json", "claude", entry, dry_run))
        elif agent == "cursor":
            if workspace is None:
                steps.append(Step("cursor", "(project)", "skipped", NEEDS_INIT))
                continue
            steps.append(
                _merge_mcp_json(workspace / ".cursor" / "mcp.json", "cursor", entry, dry_run)
            )
        elif agent == "windsurf":
            path = home / ".codeium" / "windsurf" / "mcp_config.json"
            if workspace is None:
                steps.append(Step("windsurf", str(path), "skipped", NEEDS_INIT))
                continue
            # Windsurf's config is global, so it needs to be told which repo's
            # `.meristem/` to read (README: "set cwd to the repo").
            steps.append(
                _merge_mcp_json(path, "windsurf", {**entry, "cwd": str(workspace)}, dry_run)
            )
        elif agent == "codex":
            steps.append(Step(
                "codex", str(home / ".codex" / "config.toml"), "snippet",
                "format not documented in this repo, so not written — add this to it by hand",
                codex_snippet(entry),
            ))
        elif agent == "gemini":
            steps.append(Step(
                "gemini", str(home / ".gemini" / "settings.json"), "snippet",
                "format not documented in this repo, so not written — add this to it by hand",
                json_snippet(entry),
            ))
    if workspace is not None and git_hooks is not None:
        steps.extend(git_hooks(dry_run))
    elif workspace is None:
        steps.append(Step("git", "(workspace)", "skipped", NEEDS_INIT))
    return steps


NEEDS_INIT = "not a meristem workspace — run `meristem init` here first, then re-run setup"
