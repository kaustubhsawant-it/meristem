"""Claude Code hook dispatch — pure Python, in-process, shipped in the package.

Ports the reference behaviour of the five ``~/.claude/hooks/dlms-*.js``
scripts (SessionStart digest, UserPromptSubmit router, PostToolUse liveness
watcher, Stop capture, PreCompact handoff) that used to live outside this
repo entirely. Those scripts checked ``.dlms/atoms.sqlite`` (renamed to
``.meristem/`` on 2026-08-11) and shelled out to a ``dlms`` binary that no
longer exists — dead on the one machine that had them installed, and
uninstallable by anyone else, since they were never part of the package.

Contract for every event, unchanged from the JS originals:
  * silent no-op outside a Meristem workspace;
  * never blocks or stalls the host turn (bounded wall-clock budget);
  * never raises past this module's boundary — ``run_hook`` always exits 0;
  * never prints anything but the ``hookSpecificOutput`` JSON block (or
    nothing at all).

The one behaviour the JS scripts did NOT have: a heartbeat. Each invocation
records that the hook fired — into the workspace's own ``hook_heartbeat``
table when one exists, or a small per-user JSON file otherwise — before
anything about content is decided. A silent no-op that cannot be detected is
exactly what let the ambient layer die unnoticed for 8 days; this is the fix.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import (
    GIT_PROBE_TIMEOUT_SECONDS,
    LEGACY_STATE_DIRNAME,
    Layout,
    git_toplevel,
    legacy_state_present,
    resolve_workspace,
)

# ---------------------------------------------------------------------------
# Event registry
# ---------------------------------------------------------------------------

# `meristem hook <event>` CLI names, in the order Claude Code's own session
# lifecycle fires them.
HOOK_EVENTS: tuple[str, ...] = ("session-start", "prompt", "post-tool", "stop", "pre-compact")

# Events that fire in the normal course of any session, versus events that fire
# only when something specific happens to happen. `pre-compact` runs only when
# Claude Code compacts the context window — a week of ordinary sessions that
# never filled the window produces zero pre-compact invocations and is a
# perfectly healthy workspace. Staleness therefore means nothing for it, and
# `doctor` must not warn on it; see `_check_hooks_heartbeat`. (The codebase's
# own rule, from `_check_retrieval_quality`: a check that fires on correct
# behaviour is a check people learn to ignore.)
EPISODIC_EVENTS: frozenset[str] = frozenset({"pre-compact"})

# CLI event name -> Claude Code's hookEventName / settings.json event key.
_HOOK_EVENT_NAME: dict[str, str] = {
    "session-start": "SessionStart",
    "prompt": "UserPromptSubmit",
    "post-tool": "PostToolUse",
    "stop": "Stop",
    "pre-compact": "PreCompact",
}

# A prompt/edit/session must never stall on us. `meristem route` loads an
# embedding model (~4s warm, ~10s if it round-trips the HF Hub) — this is the
# outer bound past which we give up and stay silent rather than hold up the
# turn. Chosen to match the reference JS routers' own ~8-12s timeouts.
DEFAULT_BUDGET_SECONDS = 8.0

MAX_ATOMS = 6
MIN_PROMPT_CHARS = 20
MAX_CAPTURE_MESSAGES = 40
EDIT_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})

_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime(_ISO_FMT)


def parse_heartbeat_ts(value: str | None) -> float | None:
    """Parse a heartbeat's ISO-8601 timestamp back to epoch seconds. None on
    anything unparsable — a corrupt timestamp must read as "unknown", not
    crash `doctor` or be mistaken for "just now"."""
    if not value:
        return None
    try:
        return dt.datetime.strptime(value, _ISO_FMT).replace(tzinfo=dt.UTC).timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# stdin parsing
# ---------------------------------------------------------------------------


def read_hook_input(stream: Any = None) -> dict[str, Any]:
    """Parse Claude Code's hook JSON from stdin. Tolerant of empty/invalid
    input — a hook that cannot parse its own payload must still exit clean.
    """
    try:
        raw = (stream or sys.stdin).read()
    except Exception:  # noqa: BLE001 — stdin can be closed, a pipe, anything
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _touched_file(tool_input: Any) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    return tool_input.get("file_path") or tool_input.get("notebook_path")


def _relative_within(root: Path, file_path: str) -> str | None:
    """Workspace-relative path for `file_path`, or None if it resolves
    outside `root`. Liveness targets are resolved relative to the workspace
    root, so the argument to `watcher.watch` must be too (mirrors the JS
    hook's `path.relative(cwd, filePath)` + `rel.startsWith('..')` check).
    """
    p = Path(file_path)
    if not p.is_absolute():
        p = root / p
    try:
        rel = os.path.relpath(str(p), str(root))
    except ValueError:
        return None  # e.g. different drive on Windows
    if not rel or rel == "." or rel.startswith(".."):
        return None
    return rel


# ---------------------------------------------------------------------------
# heartbeat sinks
# ---------------------------------------------------------------------------


def user_state_dir() -> Path:
    """Per-user state directory for machine-wide (non-workspace) records.
    Honors XDG_STATE_HOME; falls back to ~/.meristem to match the workspace
    dir name."""
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "meristem"
    return Path.home() / ".meristem"


def _user_heartbeat_path() -> Path:
    return user_state_dir() / "hook_heartbeat.json"


def _heartbeat_to_db(layout: Layout, event: str, outcome: str) -> None:
    """Upsert into the workspace's own `hook_heartbeat` table. Best-effort —
    a heartbeat write must never be the reason a hook fails."""
    try:
        from . import store

        with store.connect(layout.db) as conn:
            conn.execute(
                """INSERT INTO hook_heartbeat (event, last_invoked_at, last_outcome, invocations)
                        VALUES (?, ?, ?, 1)
                   ON CONFLICT(event) DO UPDATE SET
                        last_invoked_at = excluded.last_invoked_at,
                        last_outcome    = excluded.last_outcome,
                        invocations     = invocations + 1""",
                (event, _now_iso(), outcome),
            )
            conn.commit()
    except Exception:  # noqa: BLE001 — see docstring
        pass


def _heartbeat_to_file(cwd: Path, event: str, outcome: str) -> None:
    """Upsert one event's record into ~/.meristem/hook_heartbeat.json (or
    $XDG_STATE_HOME/meristem/...). Used when `cwd` is not a Meristem
    workspace, so `doctor` elsewhere can still tell hooks fire on this
    machine at all — just not here. Best-effort, same reasoning as above."""
    try:
        path = _user_heartbeat_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, json.JSONDecodeError):
                data = {}
        rec = data.get(event) if isinstance(data.get(event), dict) else {}
        rec = dict(rec) if isinstance(rec, dict) else {}
        rec["last_invoked_at"] = _now_iso()
        rec["last_outcome"] = outcome
        rec["last_cwd"] = str(cwd)
        rec["invocations"] = int(rec.get("invocations", 0)) + 1
        data[event] = rec
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def record_heartbeat(cwd: Path, event: str, outcome: str) -> None:
    """Record that `event` fired for `cwd`, regardless of what it decided to
    do about it. Routes to the workspace db when one exists, else the
    per-user file. Never raises.

    Uses `resolve_workspace`, not plain `layout_for` (2026-09-23) — a hook
    firing from inside a linked worktree has no `.meristem/` of its own
    (untracked, so it exists only in the main checkout) and used to always
    fall through to the per-user file, indistinguishable from a directory
    that was never a Meristem workspace at all."""
    try:
        layout = resolve_workspace(cwd)
        target_db = layout.db.exists()
    except Exception:  # noqa: BLE001 — a bad cwd must not lose the heartbeat
        target_db = False
        layout = None
    if target_db and layout is not None:  # target_db is False whenever layout is None
        _heartbeat_to_db(layout, event, outcome)
    else:
        _heartbeat_to_file(cwd, event, outcome)


def read_workspace_heartbeats(layout: Layout) -> dict[str, dict[str, Any]]:
    """{event: {last_invoked_at, last_outcome, invocations}} from this
    workspace's own store. Empty dict on any error (no db, pre-migration,
    corrupt file) — a read for reporting must never raise."""
    if not layout.db.exists():
        return {}
    try:
        from . import store

        with store.connect(layout.db) as conn:
            rows = conn.execute(
                "SELECT event, last_invoked_at, last_outcome, invocations FROM hook_heartbeat"
            ).fetchall()
        return {r["event"]: dict(r) for r in rows}
    except Exception:  # noqa: BLE001
        return {}


def read_user_heartbeats() -> dict[str, dict[str, Any]]:
    """The per-user fallback file's contents. Empty dict on any error."""
    path = _user_heartbeat_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# bounded execution — a hook must never stall the host turn
# ---------------------------------------------------------------------------


def _run_bounded(fn, budget: float):
    """Run `fn()` in a daemon thread, joined with a timeout. Returns
    (finished, value_or_None, error_or_None). On timeout the thread is
    abandoned (daemon, so it cannot keep the process alive) and `finished`
    is False — the caller treats that as "stay silent", never as a crash.
    """
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001 — captured for the caller to inspect
            box["error"] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(budget)
    if t.is_alive():
        return False, None, None
    if "error" in box:
        return True, None, box["error"]
    return True, box.get("value"), None


def _short_error(exc: Exception) -> str:
    return f"error:{type(exc).__name__}"[:60]


# ---------------------------------------------------------------------------
# housekeeping — the two "someone should act on this" nudges
# ---------------------------------------------------------------------------

# Sessions remembered in housekeeping_seen.json. Sessions run for days and
# there is one entry per session, so a small cap keeps the file tiny while
# still covering every session plausibly alive at once.
HOUSEKEEPING_SEEN_KEEP = 20
HOUSEKEEPING_SEEN_FILE = "housekeeping_seen.json"


def _detach_kwargs() -> dict:
    """Popen kwargs that detach the child from this process, per platform.

    POSIX: `start_new_session`. Windows has no sessions; the equivalent is a
    new process group with no console window.
    """
    if sys.platform == "win32":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
        )
        return {"creationflags": flags}
    return {"start_new_session": True}


def _spawn_background_sync(root: Path) -> bool:
    """Start `meristem sync --quiet` fully detached; return whether it started.

    Fire-and-forget by design: a new session, its own process group
    (`start_new_session`) and every stdio stream on /dev/null, so the sync
    survives this hook exiting and can neither block it nor write into the
    hook's stdout — which must carry only the hookSpecificOutput JSON. The
    sync itself is debounced and lock-guarded, so a spawn that races another
    sync (or a git hook) is a cheap no-op. Never raises: a hook that fails
    loudly is worse than a sync that did not start. Tests replace this — they
    must never launch a real `meristem`.
    """
    try:
        exe = shutil.which("meristem")
        if exe is None:
            for cand in ("meristem", "meristem.exe"):
                sibling = Path(sys.executable).parent / cand
                if sibling.exists():
                    exe = str(sibling)
                    break
        if exe is None:
            return False
        subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            [exe, "sync", "--quiet"],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_detach_kwargs(),
        )
        return True
    except Exception:  # noqa: BLE001 — advisory; never let it reach the host turn
        return False


def _pending_review_line(pending: int) -> str:
    s = "" if pending == 1 else "s"
    return (
        f"{pending} captured fact{s} awaiting review — surface this to "
        "the user and offer to run `meristem review` together; a candidate only "
        "becomes memory once a human accepts it."
    )


def _housekeeping_signature(pending: int, staleness: dict[str, Any]) -> str:
    """What "the same nudge" means: the pending count plus whether the index is
    stale at all. Deliberately not the staleness `summary`: it embeds the commit
    count, so every commit in an active session would re-fire the nudge between
    the commit and its post-commit sync catching up. One notice per stale
    episode; a sync that catches up resets it."""
    return f"{pending}|{'stale' if staleness.get('summary') else ''}"


def _seen_path() -> Path:
    return user_state_dir() / HOUSEKEEPING_SEEN_FILE


def _load_all_seen() -> dict[str, Any]:
    try:
        data = json.loads(_seen_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_seen(layout: Layout) -> dict[str, Any]:
    entry = _load_all_seen().get(str(layout.root))
    return entry if isinstance(entry, dict) else {}


def _record_seen(layout: Layout, session_id: str, signature: str) -> None:
    """Remember what this session was last shown. A small JSON file in the
    per-user state dir (`user_state_dir()`), keyed by workspace root and then
    session id, rather than `session_state` (store content the hook would have
    to open a write transaction on, on every prompt) or a file inside the
    workspace (it showed up as an untracked file in every repo). Written
    atomically since several sessions' hooks can write at once. Best effort —
    if the write fails the worst outcome is one repeated nudge."""
    all_seen = _load_all_seen()
    seen = all_seen.get(str(layout.root))
    seen = dict(seen) if isinstance(seen, dict) else {}
    seen[session_id] = {"sig": signature, "at": _now_iso()}

    # ISO timestamps sort lexically, so the newest entries survive the prune.
    def _at(item: tuple[str, Any]) -> str:
        return str(item[1].get("at", "")) if isinstance(item[1], dict) else ""

    all_seen[str(layout.root)] = dict(sorted(seen.items(), key=_at)[-HOUSEKEEPING_SEEN_KEEP:])
    path = _seen_path()
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(all_seen), encoding="utf-8")
        os.replace(tmp, path)
    with contextlib.suppress(OSError):
        tmp.unlink()
    # Older versions kept this file inside the workspace, where it showed up
    # as an untracked file. It is ours and never tracked, so drop it here and
    # the stray copies disappear on their own.
    with contextlib.suppress(OSError):
        (layout.db.parent / HOUSEKEEPING_SEEN_FILE).unlink()


def _session_id(payload: dict[str, Any]) -> str | None:
    sid = payload.get("session_id")
    return str(sid) if sid else None


def _housekeeping_for_prompt(payload: dict[str, Any], layout: Layout) -> str | None:
    """A short Housekeeping block for UserPromptSubmit, only when the nudge
    content changed since this session last saw it.

    Sessions run for days, so SessionStart's one-shot notice goes stale: facts
    captured mid-session or drift that appears later would otherwise never
    surface. Cheap by construction — a COUNT(*) and one rev-list per root, no
    embedder — and it runs on every prompt (trivial ones included, since it
    costs nothing and a "yes" is as good a moment to speak as any). Without a
    session_id there is nothing to key "already shown" on, so it stays silent
    rather than nag on every prompt."""
    sid = _session_id(payload)
    if sid is None:
        return None
    from . import candidates as candidates_mod
    from . import digest as digest_mod
    from . import store

    with store.connect(layout.db) as conn:
        try:
            pending = candidates_mod.pending_count(conn)
        except sqlite3.Error:
            pending = 0
        staleness = digest_mod._staleness(conn)

    sig = _housekeeping_signature(pending, staleness)
    entry = _load_seen(layout).get(sid)
    last = entry.get("sig") if isinstance(entry, dict) else None
    if last is None:
        # This session never went through SessionStart here (resumed under a
        # new id, or the hook was installed mid-session): treat what exists
        # now as the baseline for the block below, so it speaks once.
        last = _housekeeping_signature(0, {})
    if sig == last:
        return None
    _record_seen(layout, sid, sig)

    lines: list[str] = []
    if staleness.get("summary"):
        # Same wording as SessionStart: when behind, kick off the (debounced,
        # lock-guarded) background sync rather than asking the model to run it
        # by hand mid-session.
        behind = int(staleness.get("behind") or 0)
        suffix = _maybe_auto_sync(layout) if behind > 0 else " — run `meristem sync`"
        lines.append(f"- {staleness['summary']}{suffix}")
    if pending:
        lines.append(f"- {_pending_review_line(pending)}")
    if not lines:
        return None  # it changed by clearing (e.g. a sync caught up): nothing to say
    return "## Housekeeping (changed since last shown)\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# per-event handlers — each returns (outcome, additionalContext | None)
# ---------------------------------------------------------------------------


def _maybe_auto_sync(layout: Layout) -> str:
    """Kick off a background sync if configured; return the text to append to
    the staleness notice. The notice itself is never dropped just because a
    sync was started — the sync may not finish (or may be debounced) and the
    session must still know its answers can predate HEAD."""
    from . import config as config_mod

    try:
        enabled = config_mod.load(layout.config).freshness.auto_sync_on_session_start
    except Exception:  # noqa: BLE001 — a bad toml must not break the digest
        enabled = True
    if not enabled:
        return " — run `meristem sync`"
    if _spawn_background_sync(layout.root):
        return " — background sync started; answers may lag until it finishes"
    return " — could not start a background sync; run `meristem sync`"


def _handle_session_start(
    payload: dict[str, Any], layout: Layout, cwd: Path
) -> tuple[str, str | None]:
    from . import digest as digest_mod

    d = digest_mod.build_digest()
    lines = ["## Meristem memory substrate"]
    lines.append(
        f"{d.atom_count} live atoms · schema v{d.schema_version}"
        + (f" · branch {d.branch}" if d.branch else "")
    )

    if d.top_atoms:
        lines.append("")
        lines.append("Known facts about this project:")
        for a in d.top_atoms[:MAX_ATOMS]:
            kind = "DECIDED" if a["type"] == "decision" else str(a["type"]).upper()
            lines.append(f"- [{kind}] {a['summary']}")
        lines.append("")
        lines.append(
            "Treat these as established. Query more with the /meristem:trace skill "
            'or `meristem query "<question>"` rather than re-deriving them from source.'
        )

    # Housekeeping: the two nudges a human has to act on. Both are computed
    # by `build_digest`, and both used to reach a session only by accident —
    # pending review because a field nobody rendered (fixed here earlier),
    # staleness only as a doctor-health line that is capped and can be pushed
    # out by worse findings. Rendered together, unconditionally when present.
    housekeeping: list[str] = []
    behind = int(d.staleness.get("behind") or 0)
    stale_summary = d.staleness.get("summary")
    if stale_summary:
        note = f"{stale_summary}"
        if behind > 0:
            note += _maybe_auto_sync(layout)
        else:
            note += " — run `meristem sync`"
        housekeeping.append(f"- {note}")
    if d.pending_review:
        # A candidate is not memory until a human disposes of it, so say so
        # every time one exists, not just once the queue piles up
        # (`_check_capture_queue` in doctor.py stays "ok" below its backlog
        # threshold by design — this is a deliberately unconditional nudge).
        housekeeping.append(f"- {_pending_review_line(d.pending_review)}")
    if housekeeping:
        lines.append("")
        lines.append("Housekeeping:")
        lines.extend(housekeeping)

    # Doctor's `repo.freshness` finding says the same thing as the staleness
    # line above; rendering both is the duplicate to avoid. The Housekeeping
    # line wins (it is where the sync status lives), but a *fail*-severity
    # freshness finding still counts toward the DEGRADED verdict below.
    health = [
        h for h in d.health
        if not (stale_summary and h.get("name") == "repo.freshness")
    ]
    if health:
        lines.append("")
        lines.append("Meristem health:")
        for h in health:
            lines.append(f"- {h['severity'].upper()}: {h['summary']}")
    fails = [h for h in d.health if h["severity"] == "fail"]
    if fails:
        lines.append("")
        lines.append(
            "The substrate is DEGRADED — treat memory as unreliable this session "
            "and tell the user. `meristem doctor -v` explains each finding."
        )

    # Baseline for UserPromptSubmit: what this session has now been shown, so
    # the first prompt does not repeat it and only a later change speaks up.
    sid = _session_id(payload)
    if sid:
        _record_seen(layout, sid, _housekeeping_signature(d.pending_review, d.staleness))

    if not d.atom_count:
        lines.append("")
        lines.append("This workspace has no atoms yet. `meristem ingest --all` populates it.")

    return "injected", "\n".join(lines)


def _handle_prompt(
    payload: dict[str, Any], layout: Layout, cwd: Path
) -> tuple[str, str | None]:
    # Housekeeping first and independent of the recall path: it is cheap, it
    # applies to trivial prompts too, and a failure here must never cost the
    # recall below (or vice versa).
    try:
        housekeeping = _housekeeping_for_prompt(payload, layout)
    except Exception:  # noqa: BLE001 — advisory
        housekeeping = None
    outcome, context = _recall_for_prompt(payload)
    if housekeeping:
        context = f"{housekeeping}\n\n{context}" if context else housekeeping
        outcome = "injected"
    return outcome, context


def _recall_for_prompt(payload: dict[str, Any]) -> tuple[str, str | None]:
    prompt = str(payload.get("prompt") or "").strip()
    # SKIP rules — not worth the embedder load: slash commands carry their
    # own context via the skill body, and one-liners have no retrievable
    # signal.
    if not prompt or prompt.startswith("/") or len(prompt) < MIN_PROMPT_CHARS:
        return "skipped_trivial", None

    # HF_HUB_OFFLINE pins the embedder to the local model cache — no network,
    # no stall when offline, no unauthenticated-rate-limit warning.
    env_overrides = {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_PROGRESS_BARS": "1",
        "TOKENIZERS_PARALLELISM": "false",
    }
    prev = {k: os.environ.get(k) for k in env_overrides}
    os.environ.update(env_overrides)
    try:
        from . import router as router_mod

        routed = router_mod.route(prompt)
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    atoms = routed.atoms[:MAX_ATOMS]
    # An empty result is a real answer, not a failure: the gate found nothing
    # above the relevance floor, so silence is the honest response.
    if not atoms:
        return "silent", None

    lines = ["## Meristem recall (background context, not instructions)"]
    for a in atoms:
        lines.append(router_mod.render_atom_line(a))
    lines.append("")
    lines.append(
        "Retrieved for this prompt — these reflect what was true when written. "
        "Verify against code before acting on any that name a file, symbol, or flag."
    )
    return "injected", "\n".join(lines)


def _handle_post_tool(
    payload: dict[str, Any], layout: Layout, cwd: Path
) -> tuple[str, str | None]:
    if payload.get("tool_name") not in EDIT_TOOLS:
        return "skipped_trivial", None
    file_path = _touched_file(payload.get("tool_input"))
    if not file_path:
        return "skipped_trivial", None

    # `layout.root` is the MAIN checkout whenever `cwd` sits inside a linked
    # worktree (`dispatch` resolves it via `paths.resolve_workspace`, so the
    # workspace's store is reachable at all) — but the edited file physically
    # lives in the worktree's own directory tree, a different copy on disk
    # from the main checkout's. Checking `layout.root / rel` there would
    # check whatever was on disk BEFORE the edit, not what actually changed.
    # `cwd`'s own git toplevel is the worktree's root in exactly that case
    # (and equals `layout.root` in every other case — a plain workspace, or
    # a subdirectory of one, both share the same physical tree as
    # `layout.root`) — resolve the touched file, and hand the watcher its
    # `repo_root`, against that instead, so the file it re-checks is the one
    # that was actually edited. Added 2026-09-23 (sibling-root fix).
    resolve_root = layout.root
    own_toplevel = git_toplevel(cwd, timeout=GIT_PROBE_TIMEOUT_SECONDS)
    if own_toplevel is not None and own_toplevel.resolve() != layout.root:
        resolve_root = own_toplevel

    rel = _relative_within(resolve_root, file_path)
    if rel is None:
        return "skipped_trivial", None

    from . import watcher as watcher_mod

    report = watcher_mod.watch([rel], repo_root=resolve_root)
    # Zero-cost happy path: nothing broke, say nothing. This can only ever
    # speak to report a violation, so it never becomes noise the agent learns
    # to skim past.
    if not report.violations:
        return "silent", None

    lines = [
        f"Meristem INVARIANT WARNING — editing {rel} falsified "
        f"{len(report.violations)} recorded fact(s):"
    ]
    for v in report.violations[:5]:
        label = v.get("topic_key") or v.get("atom_id") or json.dumps(v)
        why = f" ({v['reason']})" if v.get("reason") else ""
        lines.append(f"- {label}{why}")
    lines.append("")
    lines.append(
        "Either the edit is wrong, or the recorded fact is now out of date. Decide which, "
        "and if the fact is genuinely superseded record that (`meristem decide`, or "
        "assert_fact via the meristem MCP tools) rather than leaving the substrate "
        "contradicting the code."
    )
    return "injected", "\n".join(lines)


def _handle_stop(payload: dict[str, Any], layout: Layout, cwd: Path) -> tuple[str, str | None]:
    transcript = payload.get("transcript_path")
    if not transcript:
        return "skipped_trivial", None
    transcript_path = Path(transcript)
    if not transcript_path.exists():
        return "skipped_trivial", None

    from . import capture as capture_mod
    from . import config as config_mod
    from . import store

    cfg = config_mod.load(layout.config)
    with store.connect(layout.db) as conn:
        fresh = capture_mod.capture_transcript(
            conn, transcript_path,
            limit_messages=MAX_CAPTURE_MESSAGES,
            exclude_terms=cfg.capture.exclude_terms,
        )
        conn.commit()
    # Stay silent either way — the SessionStart digest reports the queue
    # (`pending_review`); nothing here is injected into the current turn.
    return ("captured" if fresh else "silent"), None


def _handle_pre_compact(
    payload: dict[str, Any], layout: Layout, cwd: Path
) -> tuple[str, str | None]:
    from . import handoff as handoff_mod

    handoff_mod.write(handoff_mod.HandoffInput())
    # Fire-and-forget: writes a file, never injects context. Compaction must
    # never be blocked or delayed by this.
    return "written", None


_HANDLERS = {
    "session-start": _handle_session_start,
    "prompt": _handle_prompt,
    "post-tool": _handle_post_tool,
    "stop": _handle_stop,
    "pre-compact": _handle_pre_compact,
}


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def dispatch(
    event: str, payload: dict[str, Any], cwd: Path, *, budget: float = DEFAULT_BUDGET_SECONDS
) -> tuple[str, str | None]:
    """Decide + (maybe) produce output for one event. Never raises — every
    failure path returns an `error:*` outcome instead."""
    layout = resolve_workspace(cwd)

    if not layout.db.exists():
        if legacy_state_present(layout):
            context = None
            if event == "session-start":
                context = (
                    "## Meristem\n\n"
                    f"This workspace has a legacy `{LEGACY_STATE_DIRNAME}/` store from "
                    "before the 2026-08-11 rename. Run `meristem init` (or rename "
                    f"`{LEGACY_STATE_DIRNAME}` to `.meristem`) to bring it back online."
                )
            return "legacy_dlms_workspace", context
        return "not_workspace", None

    handler = _HANDLERS[event]
    # Every downstream module (digest.build_digest, router.route, capture's
    # `_open_workspace`, handoff.write) resolves its own workspace via
    # `detect_layout()`, which reads the process's actual OS cwd — not the
    # `cwd` field in the hook JSON. The reference JS hooks got this for free
    # by spawning `dlms <cmd>` with `{ cwd }`; dispatching in-process means we
    # have to do the equivalent chdir ourselves, for the duration of the
    # handler call only. Safe here because each `meristem hook` invocation is
    # a short-lived, single-purpose process handling exactly one event.
    #
    # `layout.root` is the workspace `resolve_workspace` actually found —
    # the MAIN checkout, not `cwd` itself, when `cwd` sits inside a linked
    # worktree (2026-09-23). Handlers that need the original, possibly
    # divergent `cwd` (currently only `_handle_post_tool`, for resolving
    # which physical copy of an edited file to re-check) get it passed
    # through explicitly rather than reading the chdir'd process cwd back.
    prev_cwd = Path.cwd()
    try:
        os.chdir(layout.root)
    except OSError as exc:
        return _short_error(exc), None
    try:
        finished, value, error = _run_bounded(lambda: handler(payload, layout, cwd), budget)
    finally:
        with contextlib.suppress(OSError):
            os.chdir(prev_cwd)
    if not finished:
        return "timeout", None
    if error is not None:
        return _short_error(error), None
    return value  # (outcome, context)


def run_hook(event: str, *, stream: Any = None) -> int:
    """`meristem hook <event>` entrypoint. Reads stdin, dispatches, records
    the heartbeat, prints the hookSpecificOutput JSON (if any). Always
    returns 0 — a hook that fails loudly stalls the host turn, which is
    worse than any bug this could report."""
    try:
        payload = read_hook_input(stream)
        raw_cwd = payload.get("cwd")
        cwd = Path(raw_cwd).expanduser() if raw_cwd else Path.cwd()
        outcome, context = dispatch(event, payload, cwd)
    except Exception as exc:  # noqa: BLE001 — the outermost backstop
        outcome, context = _short_error(exc), None
        cwd = Path.cwd()

    record_heartbeat(cwd, event, outcome)

    if context:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": _HOOK_EVENT_NAME[event],
                "additionalContext": context,
            },
        }))
    return 0


# ---------------------------------------------------------------------------
# `meristem hooks install|uninstall|status` — settings.json JSON-merge
# ---------------------------------------------------------------------------

HOOK_COMMAND_PREFIX = "meristem hook "
STATUSLINE_COMMAND = "meristem statusline --compact"
# The console script skips the typer/CLI import cost (the statusline runs per
# assistant turn); used only when it is actually on PATH, else the full CLI.
STATUSLINE_FAST_COMMAND = "meristem-statusline"
_STATUSLINE_PREFIXES = ("meristem statusline", "meristem-statusline")


def statusline_command() -> str:
    """The statusLine command to install: the fast console script when present."""
    return STATUSLINE_FAST_COMMAND if shutil.which(STATUSLINE_FAST_COMMAND) else STATUSLINE_COMMAND

# (settings.json event key, our command, PostToolUse matcher | None, timeout).
# Timeouts mirror the reference JS hooks' own TIMEOUT_MS constants, rounded
# to the values already tuned in this project's ~/.claude/settings.json.
_EVENT_SPEC: dict[str, tuple[str, str, str | None, int]] = {
    "session-start": ("SessionStart", "meristem hook session-start", None, 10),
    "prompt": ("UserPromptSubmit", "meristem hook prompt", None, 12),
    "post-tool": (
        "PostToolUse", "meristem hook post-tool", "Edit|Write|MultiEdit|NotebookEdit", 10,
    ),
    "stop": ("Stop", "meristem hook stop", None, 12),
    "pre-compact": ("PreCompact", "meristem hook pre-compact", None, 15),
}

_LEGACY_DLMS_RE = re.compile(r"dlms-|(?<![\w.-])dlms(?=\s|\"|$)")


@dataclass(frozen=True)
class SettingsChange:
    event: str
    outcome: str  # 'installed'|'updated'|'unchanged'|'removed'|'not_installed'|'set'|'left alone'


def default_settings_path(*, use_global: bool, cwd: Path | None = None) -> Path:
    if use_global:
        return Path.home() / ".claude" / "settings.json"
    return (cwd or Path.cwd()) / ".claude" / "settings.json"


def _load_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    data = json.loads(text)
    return data if isinstance(data, dict) else {}


def _write_settings(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _entry_for(event_key: str) -> dict[str, Any]:
    _, command, matcher, timeout = _EVENT_SPEC[event_key]
    entry: dict[str, Any] = {}
    if matcher is not None:
        entry["matcher"] = matcher
    entry["hooks"] = [{"type": "command", "command": command, "timeout": timeout}]
    return entry


def _is_ours(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    for c in entry.get("hooks") or []:
        if isinstance(c, dict) and str(c.get("command", "")).startswith(HOOK_COMMAND_PREFIX):
            return True
    return False


def _merge_event(hooks_obj: dict[str, Any], event_key: str) -> str:
    settings_event, *_ = _EVENT_SPEC[event_key]
    group = hooks_obj.setdefault(settings_event, [])
    if not isinstance(group, list):
        return "left alone (not a list)"
    desired = _entry_for(event_key)
    for i, entry in enumerate(group):
        if _is_ours(entry):
            if entry == desired:
                return "unchanged"
            group[i] = desired
            return "updated"
    group.append(desired)
    return "installed"


def _uninstall_event(hooks_obj: dict[str, Any], event_key: str) -> str:
    settings_event, *_ = _EVENT_SPEC[event_key]
    group = hooks_obj.get(settings_event)
    if not isinstance(group, list):
        return "not_installed"
    kept = [entry for entry in group if not _is_ours(entry)]
    if len(kept) == len(group):
        return "not_installed"
    if kept:
        hooks_obj[settings_event] = kept
    else:
        del hooks_obj[settings_event]
    return "removed"


def install_hooks(
    *, settings_path: Path, dry_run: bool = False, statusline: bool = False,
) -> list[SettingsChange]:
    """Merge the five hook entries into `settings_path`. Idempotent: an
    unchanged re-run reports every event 'unchanged' and touches nothing on
    disk. Preserves every other key, and every other event's entries,
    byte-for-byte where untouched."""
    data = _load_settings(settings_path)
    hooks_obj = data.setdefault("hooks", {})
    changes = [
        SettingsChange(event=e, outcome=_merge_event(hooks_obj, e)) for e in HOOK_EVENTS
    ]
    if statusline:
        current = data.get("statusLine")
        ours_or_absent = current is None or (
            isinstance(current, dict)
            and str(current.get("command", "")).startswith(_STATUSLINE_PREFIXES)
        )
        if not ours_or_absent:
            changes.append(SettingsChange(event="statusLine", outcome="left alone (foreign)"))
        else:
            desired = {"type": "command", "command": statusline_command()}
            if current == desired:
                changes.append(SettingsChange(event="statusLine", outcome="unchanged"))
            else:
                data["statusLine"] = desired
                changes.append(SettingsChange(event="statusLine", outcome="set"))

    dirty = any(c.outcome in ("installed", "updated", "set") for c in changes)
    if dirty and not dry_run:
        _write_settings(settings_path, data)
    return changes


def uninstall_hooks(*, settings_path: Path, dry_run: bool = False) -> list[SettingsChange]:
    """Remove only our five entries. Everything else in the file — including
    a foreign statusLine, which install never touches without --statusline
    and uninstall therefore never reverts — is left exactly as it was."""
    data = _load_settings(settings_path)
    hooks_obj = data.get("hooks")
    if not isinstance(hooks_obj, dict):
        return [SettingsChange(event=e, outcome="not_installed") for e in HOOK_EVENTS]
    changes = [
        SettingsChange(event=e, outcome=_uninstall_event(hooks_obj, e)) for e in HOOK_EVENTS
    ]
    if not hooks_obj:
        data.pop("hooks", None)
    if any(c.outcome == "removed" for c in changes) and not dry_run:
        _write_settings(settings_path, data)
    return changes


@dataclass(frozen=True)
class EventStatus:
    event: str
    installed_in: list[str]  # subset of {'project', 'global'}
    last_invoked_at: str | None
    last_outcome: str | None
    invocations: int


def _scan_legacy(hooks_obj: Any) -> list[tuple[str, str]]:
    """[(settings event name, command)] for every hook command in `hooks_obj`
    that still names a dlms-* script or a bare `dlms` binary — dead since the
    rename, and otherwise invisible unless something looks."""
    found: list[tuple[str, str]] = []
    if not isinstance(hooks_obj, dict):
        return found
    for event_name, groups in hooks_obj.items():
        if not isinstance(groups, list):
            continue
        for entry in groups:
            if not isinstance(entry, dict):
                continue
            for c in entry.get("hooks") or []:
                if not isinstance(c, dict):
                    continue
                cmd = str(c.get("command", ""))
                if _LEGACY_DLMS_RE.search(cmd):
                    found.append((event_name, cmd))
    return found


def scan_legacy_wiring(*paths: Path) -> list[tuple[str, str, Path]]:
    """[(settings event, command, path)] of legacy dlms-* wiring across every
    settings.json in `paths` that exists."""
    out: list[tuple[str, str, Path]] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            data = _load_settings(path)
        except (OSError, json.JSONDecodeError):
            continue
        for event_name, cmd in _scan_legacy(data.get("hooks")):
            out.append((event_name, cmd, path))
    return out


def hooks_status(*, project_path: Path, global_path: Path, layout: Layout) -> list[EventStatus]:
    """Per-event: where it's installed (project/global/neither) + the last
    heartbeat this workspace recorded for it."""
    project_data = _load_settings(project_path) if project_path.exists() else {}
    global_data = _load_settings(global_path) if global_path.exists() else {}
    heartbeats = read_workspace_heartbeats(layout)

    out: list[EventStatus] = []
    for event_key in HOOK_EVENTS:
        settings_event, *_ = _EVENT_SPEC[event_key]
        installed_in = []
        if _is_ours_group(project_data.get("hooks", {}).get(settings_event)):
            installed_in.append("project")
        if _is_ours_group(global_data.get("hooks", {}).get(settings_event)):
            installed_in.append("global")
        hb = heartbeats.get(event_key)
        out.append(EventStatus(
            event=event_key,
            installed_in=installed_in,
            last_invoked_at=hb.get("last_invoked_at") if hb else None,
            last_outcome=hb.get("last_outcome") if hb else None,
            invocations=int(hb.get("invocations", 0)) if hb else 0,
        ))
    return out


def _is_ours_group(group: Any) -> bool:
    if not isinstance(group, list):
        return False
    return any(_is_ours(entry) for entry in group)


__all__ = [
    "DEFAULT_BUDGET_SECONDS",
    "EventStatus",
    "HOOK_COMMAND_PREFIX",
    "HOOK_EVENTS",
    "EPISODIC_EVENTS",
    "STATUSLINE_COMMAND",
    "STATUSLINE_FAST_COMMAND",
    "statusline_command",
    "SettingsChange",
    "default_settings_path",
    "dispatch",
    "hooks_status",
    "install_hooks",
    "parse_heartbeat_ts",
    "read_hook_input",
    "read_user_heartbeats",
    "read_workspace_heartbeats",
    "record_heartbeat",
    "run_hook",
    "scan_legacy_wiring",
    "uninstall_hooks",
    "user_state_dir",
]
