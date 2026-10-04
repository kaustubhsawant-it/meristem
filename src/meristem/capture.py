"""Capture durable facts from what the user actually said — SPEC §18.

The adoption problem this solves
--------------------------------
Everything Meristem learned for free was re-derivable from the repo: commits,
primary keys, symbols. Everything that was *not* derivable — why a choice was
made, what is forbidden, which domain rules bind — required someone to stop and
type `meristem decide`. Measured on a real workspace after ten weeks of daily use:
317 decision atoms minted from git, **2** written by a human.

Meanwhile `retrieval_log` held 190 prompts from the same workspace containing
exactly the knowledge that was missing — paraphrased below to the same
grammatical shape without the real workspace's own domain content:

    "the nightly job runs for every environment except staging"
    "if a worker has no active lease then drop it from the pool"
    "the reviewers are saying the deploy counts are mismatched"

Meristem read every one of those — as a *query*. Never as a *fact*. It looked at the
conversation a hundred times a week and learned nothing from it.

Why this is deterministic and not an LLM call
---------------------------------------------
Tencent's memory layer runs an extraction pass every five turns, which is an
LLM call every five turns. Meristem already sits inside the agent loop as a hook, so
it does not need to pay someone to watch the conversation — but an extractor
that *itself* calls a model would reintroduce exactly the cost and latency that
keeps hooks unwired (see the 11s `meristem route` that went unused for two months).

So extraction is marker-based and stdlib-only: it looks for the grammar of a
standing rule (universal quantifiers, exceptions, prohibitions, policy modals)
rather than trying to understand the sentence. That is a *lower-precision*
strategy than an LLM and it is chosen deliberately, because nothing here is
trusted: extraction proposes, a human disposes (`meristem review`). A false positive
costs one keystroke to reject. A missed fact costs nothing that was not already
being lost. What is unacceptable — and what this module is built to avoid — is
silently writing a wrong fact into memory that later gets retrieved as truth.

The scoring weights below were tuned against 190 real prompts from a live
workspace; `tools/replay_capture.py` re-runs that measurement.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import guard

# --- Durability signals ------------------------------------------------------
# A standing rule and a one-off task read differently. "always", "never",
# "except", "by default" describe how the world IS; "fix the login bug"
# describes what to do once. These patterns detect the former.

_SIGNALS: list[tuple[str, str, int]] = [
    # (name, pattern, weight)
    # Bare "all"/"every" matter as much as "all of" — the canonical captured
    # fact shape is "the nightly job runs for every environment except
    # Staging", which an "all of"-only pattern misses entirely.
    ("universal",
     r"\b(always|never|every|each|all|everyone|everybody|any ?time|in all cases)\b", 3),
    ("exception", r"\b(except|unless|other than|apart from|besides)\b", 3),
    ("policy",
     r"\b(must|should|shall|has to|have to|needs? to|is required to|not allowed)\b", 2),
    ("prohibition", r"\b(don'?t ever|do not ever|never|avoid|stop doing|no longer)\b", 2),
    ("default",
     r"\b(by default|default(s)? to|convention|we (use|prefer|treat)"
     r"|prefer(s|red)? to|stick to)\b", 2),
    ("conditional", r"\b(if .+ then|whenever|when(ever)? .+, )\b", 2),
    ("correction", r"\b(actually|instead of|not .+ but|rather than|correction)\b", 1),
    ("scope", r"\b(only|just for|applies to|for all|for every)\b", 1),
    # A copula in the present tense marks a statement about how things ARE,
    # which is what separates "every worker gets 2 hrs of downtime on weekdays"
    # (a durable spec, universal alone scores 3 and would miss) from "remove
    # all the old jobs" (an imperative that happens to contain a universal).
    ("declarative", r"\b(is|are|was|were|gets|goes|has|have|belongs|lives|runs|uses|stays)\b", 1),
]

# --- Transience signals ------------------------------------------------------
# Subtracted. A message can carry both ("clear the cache for every environment
# except staging" has an exception AND an imperative); the sum decides.

_IMPERATIVE_VERBS = (
    r"(add|fix|remove|delete|make|update|change|implement|refactor|"
    r"create|build|run|write|put|move|rename|install|commit|push|check|look|show|"
    r"give|send|open|close|start|stop|continue|try|redo|undo|revert)"
)
_IMPERATIVE_LEAD = re.compile(
    rf"^\s*(please\s+)?{_IMPERATIVE_VERBS}\b", re.IGNORECASE
)
# The same imperative, but behind a discourse
# lead — "ok now update every task so it has a deadline". The plain lead above
# only sees a verb in first position, so a spoken "ok/so/now/also …" prefix hid
# the command from it. Soft penalty (-2), not a drop: the same shape can still
# carry a real rule when the rest of the sentence is strong.
_DISCOURSE_IMPERATIVE = re.compile(
    r"^\s*(?:(?:ok|okay|so|now|first|then|also|and also|pls|please|lets|let's)[,]?\s+)+"
    rf"{_IMPERATIVE_VERBS}\b",
    re.IGNORECASE,
)
# C2: a request addressed to the agent ("can u commit and check…", "u are the
# manager…", "want you to…", "ur"). Measured on review labels: 22 of 95
# rejected but also 1 of 26 accepted, so it is a -2 penalty, never a drop —
# a strong standing rule wrapped in "if u …" still clears the threshold.
_ADDRESSED = re.compile(
    r"\b(can|could|would) (u|you)\b|\bu are\b|\bif u\b"
    r"|\bu (just )?(have|need) to\b|\bwant (u|you) to\b|\bur\b",
    re.IGNORECASE,
)
# Deictic reference — "that line", "this file", "the above". A sentence that
# only makes sense next to the thing it points at is not a durable fact; it is
# a fact about right now, and storing it produces memory that reads as a rule
# and cannot be interpreted six weeks later.
_DEICTIC = re.compile(
    r"\b(this|that|these|those|here|there|above|below|the current|it)\b", re.IGNORECASE
)
_QUESTION = re.compile(r"\?\s*$")

# Wrappers that are not the user talking, even though they arrive as user turns.
_NOT_USER_TEXT = re.compile(
    r"^\s*(<(system-reminder|teammate-message|local-command|command-name"
    r"|command-message|summary)"
    r"|Another Claude session sent|Caveat: The messages below"
    r"|Background command|The core finding:|\[Image[: \]])",
    re.IGNORECASE,
)

# Machine-authored text that arrives as a user turn. Hooks, skills and subagent
# verdicts all inject their output into the conversation this way, and none of
# it carries `promptSource: sdk` to filter on.
#
# This is the single largest precision defect found while building this module.
# Across 278 real transcripts (2108 human messages) an earlier build proposed
# 237 unique candidates, and the overwhelming majority were the agent quoting
# itself: "A session-scoped Stop hook is now active with condition: …",
# "Transcript evidence shows: (1) …", "The assistant has completed …". Accepting
# those would make Meristem learn from its own output — the substrate's own summary
# of a task, fed back as a durable fact about the project.
#
# The reliable tell is grammatical person. A user describing their project says
# "we never deploy on fridays"; a machine describing a session says "the
# assistant", "the transcript", "the condition". Third-person process narration
# is never someone stating a project rule.
_MACHINE_AUTHORED = re.compile(
    r"("
    r"session-scoped|Stop hook is now active|transcript evidence|"
    r"\bthe (assistant|agent|transcript|condition|user has)\b|"
    r"\bassistant('s)? (states|confirmed|explicitly|has|acknowledges)\b|"
    r"\b(is|was) (NOT |not )?(satisfied|complete)\b|"
    r"^\s*\(\d+\)|"          # "(2) …" — enumerated continuation prompts
    r"^\s*\[.{0,80}\]:"      # "[git commit and deploy all]: …" — quoted condition
    r")",
    re.IGNORECASE,
)

MIN_CHARS = 25
MAX_CHARS = 400
DEFAULT_THRESHOLD = 4

# --- Precision drops (measured on review labels) -----------------------------
# Measured from human review labels across every onboarded workspace on the dev
# machine: 26 accepted vs 95 rejected, precision about 21%, and every one of the
# 121 still passed extract(). Each rule below is a hard drop that removed
# rejected candidates and lost no accepted one in that replay
# (`tools/replay_capture.py --labels` re-measures; counts only, never text).
# Order matters only for the name `explain()` reports.

# Unit opens with a bracket or quote: a quoted template or condition, not the
# user speaking ("[run all checks …]", '" and also remove it for all…').
# 15 of 95 rejected, 0 of 26 accepted.
_LEADING_QUOTE = re.compile(r"^\s*[\[\"'\u201c]")
# Agent narration that slipped past _MACHINE_AUTHORED: "transcript provides
# evidence of partial completion". 9 of 95 rejected, 0 of 26 accepted.
_NARRATION = re.compile(
    r"\btranscripts?\b.{0,80}\b(provides?|shows?|evidence|indicates?)\b|\bevidence of\b",
    re.IGNORECASE,
)
# Talk about the conversation itself, not the project: "this session", "your
# review", "waiting on you". 3 of 95 rejected, 0 of 26 accepted.
_META_CONVERSATION = re.compile(
    r"\b(this|the current) (conversation|session|chat)\b|\byour review\b"
    r"|\bmemory note\b|\bwaiting on you\b",
    re.IGNORECASE,
)
# First-person musing or status, not a rule: "ok i have gotten the gist".
# 5 of 95 rejected, 0 of 26 accepted.
_FIRST_PERSON = re.compile(
    r"\bi (wish|was wondering|think|guess|have gotten|want u|am|was)\b", re.IGNORECASE
)
# A short clause that opens on a preposition/conjunction and has no modal or
# copula: "for all users side by side" is a dangling fragment of an earlier
# message. 1 of 95 rejected, 0 of 26 accepted. "and per project each task
# should have a deadline!" is short and starts with "and" but has a modal, so
# it stays.
_FRAGMENT_LEAD = re.compile(r"^\s*(for|and|or|but|with|on|in|to|of|by)\b", re.IGNORECASE)
_FRAGMENT_VERB = re.compile(
    r"\b(must|should|shall|will|would|can|could|is|are|was|were|be|has|have|had"
    r"|do|does|need|needs)\b",
    re.IGNORECASE,
)
FRAGMENT_MAX_CHARS = 60


def _drop_rule(unit: str, guard_policy: guard.Policy | None = None) -> str | None:
    """Name of the hard-drop rule that removes `unit`, or None."""
    if guard.scan(unit, policy=guard_policy or guard.Policy()):
        return "guard"  # a secret or personal data is never proposed as a fact
    if _MD_LINE.match(unit):
        return "markdown"  # a stray list item or table row that survived splitting
    if _NOT_USER_TEXT.match(unit) or _MACHINE_AUTHORED.search(unit):
        return "machine-authored"  # wrappers can start a unit mid-message
    if _LEADING_QUOTE.match(unit):
        return "leading-quote"
    if _NARRATION.search(unit):
        return "narration"
    if _META_CONVERSATION.search(unit):
        return "meta-conversation"
    if _FIRST_PERSON.search(unit):
        return "first-person"
    if (
        len(unit) < FRAGMENT_MAX_CHARS
        and _FRAGMENT_LEAD.match(unit)
        and not _FRAGMENT_VERB.search(unit)
    ):
        return "verbless-fragment"
    return None


def explain(
    unit: str,
    *,
    threshold: int = DEFAULT_THRESHOLD,
    guard_policy: guard.Policy | None = None,
) -> str | None:
    """Why `unit` would not be proposed now, or None if it would.

    Returns the hard-drop rule's name, or for a unit that scores under the
    threshold the penalty signal that cost it ("-addressed", "-imperative",
    "-deictic"), else "below-threshold". Shared by `meristem review --noise`
    and `tools/replay_capture.py --labels` so both name the same rule.
    """
    rule = _drop_rule(unit, guard_policy)
    if rule is not None:
        return rule
    score, signals = score_unit(unit)
    if score >= threshold:
        return None
    for name in signals:
        if name.startswith("-"):
            return name
    return "below-threshold"


# Markdown furniture. A pasted document is not the user talking, and mining one
# produces confident nonsense: on a real transcript an earlier build of this
# module proposed 34 "facts" from 37 messages — "Never use ease-in for UI
# animations", "Most users never customize" — every one of them a line lifted
# out of an injected skill document. The rules in a document are the document's
# rules, not this project's, and the user never said them.
_MD_LINE = re.compile(r"^\s*(#{1,6}\s|[-*+]\s|\d+[.)]\s|\||>\s|```)")
_MD_TABLE = re.compile(r"\|\s*-{2,}|\|.*\|.*\|")
DOCUMENT_LINE_RATIO = 0.30
DOCUMENT_MIN_LINES = 4
# Length ceiling for a message to count as someone talking. Measured on a real
# transcript: the messages that leaked document prose past the markdown check
# were a single 8042-char / 52-line paste, while the longest genuine prompt —
# itself carrying a real constraint ("never npm start — it writes 18 rows…") —
# was 1477 chars. Prose-only documents have no markdown furniture to detect, so
# length is the only signal left that separates them.
MAX_MESSAGE_CHARS = 3000


def looks_like_document(text: str) -> bool:
    """True when `text` is a pasted/injected document rather than a typed turn."""
    if len(text) > MAX_MESSAGE_CHARS:
        return True
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < DOCUMENT_MIN_LINES:
        return False
    if _MD_TABLE.search(text):
        return True
    marked = sum(1 for ln in lines if _MD_LINE.match(ln))
    return marked / len(lines) >= DOCUMENT_LINE_RATIO

# `promptSource` values that represent a human typing. `sdk` is a subagent
# prompt and `system` is injected text — neither is the user, and capturing
# them would teach Meristem its own output.
HUMAN_PROMPT_SOURCES = frozenset({"typed", "queued", ""})


@dataclass
class CandidateFact:
    text: str
    score: int
    signals: list[str] = field(default_factory=list)
    proposed_type: str = "convention"
    session_id: str | None = None
    source_ref: str | None = None

    @property
    def fingerprint(self) -> str:
        """Stable id for dedupe — normalised text, not raw.

        Rejection must stick. Without a fingerprint that survives whitespace and
        case drift, the same sentence gets re-proposed every session and
        `meristem review` becomes a treadmill nobody runs twice.
        """
        norm = re.sub(r"\s+", " ", self.text.strip().lower())
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _split_units(text: str) -> list[str]:
    """Split a message into candidate units.

    Real prompts are frequently unpunctuated ("clear the cache for every
    environment except staging"), so a sentence splitter alone drops most of
    the corpus. Newlines and terminators both split; a message with neither
    stays whole.

    Semicolons and arrows split too. In the tuning corpus a durable fact is
    almost always one clause inside a long multi-topic message — "…every worker
    gets a daily 2 hour maintenance window on weekdays…" arrives welded to
    three unrelated requests — and this user, like many, separates those with
    `;` and `->` rather than full stops. Without these separators the unit
    exceeds MAX_CHARS and the fact inside it is never seen.
    """
    parts = re.split(r"(?<=[.!?;])\s+|\n+|\s*->\s*|;", text)
    return [p.strip() for p in parts if p and p.strip()]


def score_unit(unit: str) -> tuple[int, list[str]]:
    """Score one unit for durability. Returns (score, matched signal names)."""
    if len(unit) < MIN_CHARS or len(unit) > MAX_CHARS:
        return 0, []
    if _QUESTION.search(unit):
        # A question is a request for knowledge, not an assertion of it.
        return 0, []

    score = 0
    matched: list[str] = []
    for name, pattern, weight in _SIGNALS:
        if re.search(pattern, unit, re.IGNORECASE):
            score += weight
            matched.append(name)

    if not matched:
        return 0, []

    if _IMPERATIVE_LEAD.match(unit) or _DISCOURSE_IMPERATIVE.match(unit):
        score -= 2
        matched.append("-imperative")
    if _ADDRESSED.search(unit):
        score -= 2
        matched.append("-addressed")
    # Deixis is only penalised when it dominates: a rule may legitimately say
    # "if a worker has no active lease then remove that entry". Two or more
    # pointers with no universal quantifier means it is about the here-and-now.
    deictic_hits = len(_DEICTIC.findall(unit))
    if deictic_hits >= 2 and "universal" not in matched:
        score -= 2
        matched.append("-deictic")
    return score, matched


def _classify(signals: list[str]) -> str:
    """Map signals to an atom type from the closed §3 taxonomy.

    Deliberately coarse. A wrong type is visible and one keystroke to fix at
    review; a confident-but-wrong taxonomy would be baked into retrieval.
    """
    if "prohibition" in signals or "policy" in signals:
        return "invariant"
    if "correction" in signals:
        return "decision"
    return "convention"


def _build_exclude_matcher(exclude_terms: Sequence[str]) -> re.Pattern[str] | None:
    """Compile `[capture] exclude_terms` into one whole-word, case-insensitive pattern.

    A workspace's own denylist (SPEC §18) — not a general PII detector. A real
    onboarded workspace's incident that motivated this (2026-08) had the
    extractor's own tuned-for grammar ("the nightly job runs for every
    environment except staging") wrapped around real personal names and
    schedules belonging to actual people the workspace tracked. Rule-shaped
    sentences that name real people should never become a durable "fact" no
    matter how well they score, so this is a hard exclusion, not a score
    penalty — a workspace owner listing a term here means it must never appear
    in a captured candidate, full stop.
    """
    if not exclude_terms:
        return None
    alternation = "|".join(re.escape(t) for t in exclude_terms if t)
    if not alternation:
        return None
    return re.compile(rf"\b({alternation})\b", re.IGNORECASE)


def extract(
    text: str,
    *,
    threshold: int = DEFAULT_THRESHOLD,
    session_id: str | None = None,
    exclude_terms: Sequence[str] = (),
    guard_policy: guard.Policy | None = None,
) -> list[CandidateFact]:
    """Pull candidate durable facts out of one user message."""
    if not text or _NOT_USER_TEXT.match(text) or text.lstrip().startswith("/"):
        return []
    if looks_like_document(text) or _MACHINE_AUTHORED.search(text):
        return []
    exclude_re = _build_exclude_matcher(exclude_terms)
    out: list[CandidateFact] = []
    for unit in _split_units(text):
        if _drop_rule(unit, guard_policy) is not None:
            continue  # see _drop_rule: markdown, wrappers, and the C1 precision drops
        if exclude_re is not None and exclude_re.search(unit):
            continue  # workspace-owner denylist (e.g. personal names) — never captured
        score, signals = score_unit(unit)
        if score >= threshold:
            out.append(
                CandidateFact(
                    text=unit,
                    score=score,
                    signals=signals,
                    proposed_type=_classify(signals),
                    session_id=session_id,
                )
            )
    return out


# --- Transcript reading ------------------------------------------------------


def iter_user_messages(transcript: Path) -> Iterable[tuple[str, str | None]]:
    """Yield (text, session_id) for each genuinely human turn in a transcript.

    Tolerant by design: a transcript is an append-only log being written by
    another process, so a truncated final line is normal and must not abort the
    capture of everything before it.
    """
    try:
        raw = transcript.read_text(errors="replace", encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "user":
            continue
        if rec.get("isSidechain"):
            continue  # subagent conversation, not the user
        src = rec.get("promptSource", "")
        if src not in HUMAN_PROMPT_SOURCES:
            continue
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, list):
            # Lists are tool results; only `text` blocks are the user speaking.
            content = " ".join(
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        if isinstance(content, str) and content.strip():
            yield content, rec.get("sessionId")


def capture_transcript(
    conn: sqlite3.Connection,
    transcript: Path,
    *,
    threshold: int = DEFAULT_THRESHOLD,
    limit_messages: int | None = None,
    exclude_terms: Sequence[str] = (),
    guard_policy: guard.Policy | None = None,
) -> list[CandidateFact]:
    """Extract from a transcript and queue what is new. Returns queued items."""
    from . import candidates as cand

    msgs = list(iter_user_messages(transcript))
    if limit_messages is not None:
        msgs = msgs[-limit_messages:]

    found: list[CandidateFact] = []
    seen: set[str] = set()
    for text, session_id in msgs:
        for c in extract(
            text,
            threshold=threshold,
            session_id=session_id,
            exclude_terms=exclude_terms,
            guard_policy=guard_policy,
        ):
            if c.fingerprint in seen:
                continue
            seen.add(c.fingerprint)
            c.source_ref = transcript.name
            found.append(c)
    return cand.queue_many(conn, found)


__all__ = [
    "DEFAULT_THRESHOLD",
    "CandidateFact",
    "capture_transcript",
    "explain",
    "extract",
    "iter_user_messages",
    "score_unit",
]
