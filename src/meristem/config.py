"""Read/write meristem.toml.

We use stdlib `tomllib` for reads (Python 3.11+). Writes go through a small
hand-rolled emitter so we don't take a dependency on tomli-w just to render
a flat default file. Users are free to hand-edit the resulting file.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class WorkspaceCfg:
    name: str = "myproject"
    roots: list[str] = field(default_factory=lambda: ["."])
    shared: bool = True


@dataclass
class ScanCfg:
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(
        default_factory=lambda: [
            "node_modules", "dist", "build", ".venv", "target", ".dart_tool",
            # Claude Code's EnterWorktree feature checks out a full nested
            # git worktree under one of these names. Left unexcluded, its
            # files get ingested as if they were part of the main tree —
            # duplicate atoms while the worktree exists, permanently-failing
            # liveness predicates once it's cleaned up (first hit: Phase 1
            # multi-root onboarding, 2026-08-20).
            "worktrees", ".worktrees",
        ]
    )
    max_file_kb: int = 512
    follow_symlinks: bool = False


@dataclass
class IngestersCfg:
    # `modules` is the only ROLLS_UP producer, and ROLLS_UP is what makes the
    # tier-aware drill-down retrieval of SPEC §14.1 work at all. Omitting it
    # here (as every config generated before 2026-07 did) left that feature
    # dead in every real workspace. Existing configs pin their own list, so
    # they need `meristem ingest --all` once to backfill — `meristem doctor` says so.
    enabled: list[str] = field(
        default_factory=lambda: [
            "readme", "manifest", "schema", "invariants",
            "git", "symbols", "modules", "owners",
        ]
    )


@dataclass
class CaptureCfg:
    # Terms that must never become a captured fact, even when the sentence
    # around them reads as a durable rule. capture.py's own module docstring
    # cites "the nightly job runs for every environment except staging" as the
    # canonical example of what its heuristics are *tuned to catch* — but a
    # real onboarded workspace's incident (2026-08) had that exact grammar
    # wrapped around real personal names and schedules belonging to actual
    # people the workspace tracked, which got proposed as durable "facts"
    # about the codebase. Rule-shaped grammar is not the same as
    # project-relevant content. This is a denylist, not an
    # NLP name-detector, because only the workspace owner knows who is
    # personal-and-off-limits versus a legitimately code-relevant proper noun
    # (e.g. a library or service named after a person). Empty by default —
    # opt-in, since nobody else can populate this list correctly.
    exclude_terms: list[str] = field(default_factory=list)


@dataclass
class BootstrapCfg:
    budget_seconds: int = 60
    token_ceiling: int = 4000


@dataclass
class RetrievalCfg:
    turn_token_cap: int = 3000
    embedder: str = "bge-small-en-v1.5"
    reranker: str = "ms-marco-MiniLM-L6"
    # Minimum query↔atom cosine for an atom to be injected. Below this the
    # atom is dropped; if nothing clears the bar, Meristem injects nothing at all.
    #
    # THIS NUMBER IS EMBEDDER-SPECIFIC. Cosine scales differ per model, so a
    # floor calibrated for one is meaningless for another. 0.70 is calibrated
    # for bge-small-en-v1.5 against a real workspace: deliberate nonsense
    # ("purple elephant tax return quantum") topped out at 0.666 there, and
    # genuine queries reached 0.83. Re-derive it with
    # `tools/replay_retrieval.py` if you change `embedder`.
    min_relevance: float = 0.70


@dataclass
class FreshnessCfg:
    """`meristem sync` behaviour (SPEC §19)."""

    # Minimum seconds between automatic syncs. The post-commit hook passes no
    # flags, so this is the only lever a repo with a heavy commit cadence has.
    debounce_seconds: int = 90
    # Embed after ingesting. On by default because an atom with no vector fails
    # the relevance gate as `unknown` (§14.6) — stored, live and unretrievable.
    embed_after_sync: bool = True
    # When a session starts and the index is behind HEAD, spawn a detached
    # `meristem sync --quiet` so the drift heals without anyone remembering to
    # run it (the git hooks only cover commits made where they are installed).
    # The debounce window and SyncLock make a redundant spawn a cheap no-op.
    auto_sync_on_session_start: bool = True


@dataclass
class SyncCfg:
    """Team sync protocol (SPEC §16). The shared substrate always exports as
    git-mergeable JSONL (see sync_protocol.py); `.meristem/atoms.sqlite`
    itself is always local-only and gitignored. Two `mode`s:
      "git-jsonl"          — one file, `export_path`. Simple; fine until a
                              store gets large enough that rewriting/diffing/
                              parsing it whole on every sync gets expensive.
      "git-jsonl-sharded"  — many small files under `export_dir`, one per
                              `shard_prefix_len`-hex-char bucket of an atom's
                              content hash (sync_protocol.shard_of). A change
                              to one atom only touches its one shard file."""

    mode: str = "git-jsonl"
    export_path: str = ".meristem/atoms.jsonl"
    export_dir: str = ".meristem/atoms"
    shard_prefix_len: int = 2


@dataclass
class GuardCfg:
    """Trust guard (guard.py): secrets and personal data are never captured and
    never leave in the shared export. `allow_patterns` are regexes that
    whitelist a match (re.search against the matched text) — the escape hatch
    for a known-safe false positive."""

    enabled: bool = True
    allow_patterns: list[str] = field(default_factory=list)


@dataclass
class Config:
    workspace: WorkspaceCfg = field(default_factory=WorkspaceCfg)
    scan: ScanCfg = field(default_factory=ScanCfg)
    ingesters: IngestersCfg = field(default_factory=IngestersCfg)
    capture: CaptureCfg = field(default_factory=CaptureCfg)
    bootstrap: BootstrapCfg = field(default_factory=BootstrapCfg)
    retrieval: RetrievalCfg = field(default_factory=RetrievalCfg)
    freshness: FreshnessCfg = field(default_factory=FreshnessCfg)
    sync: SyncCfg = field(default_factory=SyncCfg)
    guard: GuardCfg = field(default_factory=GuardCfg)


def load(path: Path) -> Config:
    """Load meristem.toml from `path`. Missing keys fall back to dataclass defaults."""
    raw: dict = {}
    if path.exists():
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    cfg = Config()
    if (ws := raw.get("workspace")):
        cfg.workspace = WorkspaceCfg(
            name=ws.get("name", cfg.workspace.name),
            roots=ws.get("roots", cfg.workspace.roots),
            shared=ws.get("shared", cfg.workspace.shared),
        )
    if (sc := raw.get("scan")):
        cfg.scan = ScanCfg(
            include=sc.get("include", cfg.scan.include),
            exclude=sc.get("exclude", cfg.scan.exclude),
            max_file_kb=sc.get("max_file_kb", cfg.scan.max_file_kb),
            follow_symlinks=sc.get("follow_symlinks", cfg.scan.follow_symlinks),
        )
    if (ig := raw.get("ingesters")):
        cfg.ingesters = IngestersCfg(enabled=ig.get("enabled", cfg.ingesters.enabled))
    if (cp := raw.get("capture")):
        cfg.capture = CaptureCfg(exclude_terms=cp.get("exclude_terms", cfg.capture.exclude_terms))
    if (bs := raw.get("bootstrap")):
        cfg.bootstrap = BootstrapCfg(
            budget_seconds=bs.get("budget_seconds", cfg.bootstrap.budget_seconds),
            token_ceiling=bs.get("token_ceiling", cfg.bootstrap.token_ceiling),
        )
    if (rt := raw.get("retrieval")):
        cfg.retrieval = RetrievalCfg(
            turn_token_cap=rt.get("turn_token_cap", cfg.retrieval.turn_token_cap),
            embedder=rt.get("embedder", cfg.retrieval.embedder),
            reranker=rt.get("reranker", cfg.retrieval.reranker),
            min_relevance=rt.get("min_relevance", cfg.retrieval.min_relevance),
        )
    if (fr := raw.get("freshness")):
        cfg.freshness = FreshnessCfg(
            debounce_seconds=fr.get("debounce_seconds", cfg.freshness.debounce_seconds),
            embed_after_sync=fr.get("embed_after_sync", cfg.freshness.embed_after_sync),
            auto_sync_on_session_start=fr.get(
                "auto_sync_on_session_start", cfg.freshness.auto_sync_on_session_start
            ),
        )
    if (sy := raw.get("sync")):
        cfg.sync = SyncCfg(
            mode=sy.get("mode", cfg.sync.mode),
            export_path=sy.get("export_path", cfg.sync.export_path),
            export_dir=sy.get("export_dir", cfg.sync.export_dir),
            shard_prefix_len=sy.get("shard_prefix_len", cfg.sync.shard_prefix_len),
        )
    if (gd := raw.get("guard")):
        cfg.guard = GuardCfg(
            enabled=bool(gd.get("enabled", cfg.guard.enabled)),
            allow_patterns=list(gd.get("allow_patterns", cfg.guard.allow_patterns)),
        )
    return cfg


def set_min_relevance(path: Path, value: float) -> str:
    """Write `[retrieval] min_relevance` into an existing meristem.toml in place.

    Surgical rather than a re-render: a workspace's meristem.toml is hand-edited
    (that is the documented contract at the top of the generated file), so
    rewriting the whole thing to change one float would silently discard
    exclude-lists, ingester choices and comments. Returns the new file text.

    Handles the three shapes a real file comes in: the key already present, the
    `[retrieval]` section present without it, and no section at all.
    """
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    line = f"min_relevance = {value}"

    in_retrieval = False
    section_start = None
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_retrieval:
                break  # left [retrieval] without finding the key
            in_retrieval = stripped == "[retrieval]"
            if in_retrieval:
                section_start = i
            continue
        # Match the key only inside [retrieval]; `min_relevance` under another
        # table would be a different setting that happens to share a name.
        if in_retrieval and stripped.split("=")[0].strip() == "min_relevance":
            lines[i] = line
            out = "\n".join(lines) + "\n"
            path.write_text(out, encoding="utf-8")
            return out

    if section_start is not None:
        lines.insert(section_start + 1, line)
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines += ["[retrieval]", line]
    out = "\n".join(lines) + "\n"
    path.write_text(out, encoding="utf-8")
    return out


def render_default(name: str, roots: list[str]) -> str:
    """Render a starter meristem.toml. Mirrors meristem.toml.example, parameterized."""
    roots_toml = ", ".join(f'"{r}"' for r in roots)
    return f"""# meristem.toml — config for Meristem (auto-generated by `meristem init`)
# Hand-edit freely. Schema lives in meristem/SPEC.md.

[workspace]
name = "{name}"
roots = [{roots_toml}]
shared = true

[scan]
include = []
exclude = [
  "node_modules", "dist", "build", ".venv", "target", ".dart_tool",
  "worktrees", ".worktrees",
]
max_file_kb = 512
follow_symlinks = false

[ingesters]
enabled = ["readme", "manifest", "schema", "invariants", "git", "symbols", "modules", "owners"]

[capture]
# Terms that must never become a captured fact (SPEC §18), even when the
# sentence around them reads as a durable rule — e.g. the names
# of real people a project tracks. Case-insensitive, whole-word match.
# Empty by default: only you know what is personal here.
exclude_terms = []

[bootstrap]
budget_seconds = 60
token_ceiling = 4000

[retrieval]
turn_token_cap = 3000
embedder = "bge-small-en-v1.5"
reranker = "ms-marco-MiniLM-L6"
# Minimum query-to-atom cosine for an atom to be shown or injected. Below this
# it is dropped; if nothing clears the bar, Meristem says nothing rather than
# answering anyway.
#
# THIS NUMBER IS EMBEDDER- AND CORPUS-SPECIFIC. 0.70 is calibrated for
# bge-small-en-v1.5 against one populated workspace; it is a starting point, not
# a constant. Once this workspace has been used for a while, run
# `meristem calibrate` to re-derive it from your own prompts, and
# `meristem calibrate --write` to store the result here.
min_relevance = 0.70

[freshness]
debounce_seconds = 90
embed_after_sync = true
# Kick off a background `meristem sync` when a Claude Code session starts and
# the index is behind HEAD. The session still gets the staleness notice.
auto_sync_on_session_start = true

[sync]
# "git-jsonl" (default) exports the whole shared store to one file — simple,
# fine until it gets large enough that rewriting/diffing/parsing it whole on
# every sync gets expensive. "git-jsonl-sharded" instead spreads the same
# rows across many small files under export_dir (256 by default), so a
# change to one atom only ever touches its one shard.
mode = "git-jsonl"
export_path = ".meristem/atoms.jsonl"
export_dir = ".meristem/atoms"
shard_prefix_len = 2

[guard]
# Trust guard: secrets (keys, tokens, private keys, password= assignments,
# high-entropy strings) and personal data (emails, phone numbers, your
# [capture] exclude_terms) are never proposed as facts and are withheld from
# the shared export. `meristem doctor` (guard.store) reports atoms already
# holding any. allow_patterns: regexes that whitelist a known-safe match.
enabled = true
allow_patterns = []

[handoff]
capacity_warn_pct = 80
capacity_halt_pct = 85
keep_handoffs_days = 30

[branches]
protected = ["main", "stable"]
"""
