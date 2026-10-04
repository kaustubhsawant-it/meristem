"""README ingester — markdown docs at workspace root → glossary atoms.

Targets a small list of "project introduction" files (README.md, CLAUDE.md,
AGENTS.md, CONTRIBUTING.md). For each, we emit one glossary atom whose
50-word summary is the first paragraph after the leading H1. The intent is
to seed the substrate with "what is this project" knowledge before any
deeper ingester runs.

Liveness: regex predicate that the H1 still matches — files renamed or
title-changed will fail liveness, which is exactly the staleness signal we
want surfaced.
"""

from __future__ import annotations

import re

from ..atoms import Liveness, assert_fact
from .base import IngestContext, IngestResult

ROOT_DOC_NAMES: tuple[str, ...] = (
    "README.md", "README.rst", "README", "CLAUDE.md", "AGENTS.md",
    "CONTRIBUTING.md", "ARCHITECTURE.md",
)

_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def _summarize(text: str, *, words: int) -> str:
    """First non-empty paragraph after the H1 (or top of doc), capped to N words."""
    body = text
    m = _H1_RE.search(text)
    if m:
        body = text[m.end():]
    for para in re.split(r"\n\s*\n", body.strip()):
        para = para.strip()
        if para and not para.startswith("#"):
            tokens = para.split()
            snippet = " ".join(tokens[:words])
            return re.sub(r"\s+", " ", snippet)
    # fallback — whole file, capped
    return " ".join(text.split()[:words])


def run(ctx: IngestContext) -> IngestResult:
    res = IngestResult(name="readme")
    for name in ROOT_DOC_NAMES:
        path = ctx.root / name
        if not path.is_file():
            continue
        if not ctx.is_changed(name):  # incremental: skip docs that didn't change
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            res.notes.append(f"{name}: read failed ({exc})")
            res.atoms_skipped += 1
            continue
        title_m = _H1_RE.search(text)
        title = title_m.group(1).strip() if title_m else path.stem
        summary_10 = title
        summary_50 = _summarize(text, words=50) or title
        summary_250 = _summarize(text, words=250) or summary_50
        rel = ctx.rel_path(path)
        liveness = (
            Liveness(kind="regex", target=rel, pattern=rf"(?m)^#\s+{re.escape(title)}\s*$")
            if title_m
            else Liveness(kind="none")
        )
        assert_fact(
            ctx.conn,
            type="glossary",
            topic_key=f"doc:{rel}",
            summary_10w=summary_10,
            summary_50w=summary_50,
            summary_250w=summary_250,
            source_kind="readme",
            source_ref=rel,
            liveness=liveness,
            repo_id=ctx.repo_id,
            workspace_id=ctx.workspace_id,
        )
        res.atoms_inserted += 1
    if not res.atoms_inserted and not res.notes:
        res.notes.append("no root docs found")
    return res
