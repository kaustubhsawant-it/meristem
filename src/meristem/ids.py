"""Stable content-hashed atom IDs.

An atom's identity is `(type, topic_key, canonical_claim)`. The canonical
claim is normally the 50-word summary — it's the "thesis sentence" of the
atom. Two atoms with the same (type, topic_key, summary_50w) are the same
atom; they collapse on insert.

IDs are prefixed by type for human grep-ability:
    invariant_3f7c9a2b1d4e
    decision_8a1b2c3d4e5f
"""

from __future__ import annotations

import hashlib
import re

_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase, collapse whitespace, strip — makes the hash stable across
    cosmetic edits to the claim text."""
    return _WS.sub(" ", text.strip().lower())


def atom_id(type_: str, topic_key: str, claim: str) -> str:
    h = hashlib.sha1(
        f"{type_}\x00{topic_key}\x00{normalize(claim)}".encode()
    ).hexdigest()[:12]
    return f"{type_}_{h}"
