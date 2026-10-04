# 9. Design principles

Sections 1-8 documented what Meristem does. This section names why: a small
set of principles that recur across the data model, the write path, and the
hooks, stated explicitly in this codebase's own docstrings and commit
messages rather than invented for this document.

## The LLM stays outside the CLI

Meristem's own machinery — ingestion, retrieval, liveness checks, capture —
runs without calling a model. `capture.py`'s module docstring states this by
contrast with a competitor: "Tencent's memory layer runs an extraction pass
every five turns, which is an LLM call every five turns." Meristem's
extractor is marker-based and stdlib-only instead — it looks for the grammar
of a standing rule (quantifiers, exceptions, prohibitions, policy modals)
rather than asking a model to understand the sentence (§6), because an
extractor "that *itself* calls a model would reintroduce exactly the cost
and latency that keeps hooks unwired" — a reference to the 2026-08-11
rename that left all five hooks silently dead for 8 days.

The budget is concrete elsewhere too: `hooks.py`'s dispatcher runs every
handler in a daemon thread joined against `DEFAULT_BUDGET_SECONDS = 8.0`
(§5), chosen because loading an embedding model can take "~4s warm, ~10s if
it round-trips the HF Hub" — a hook that stalls a turn is worse than one
that silently does nothing. A model in the hot path threatens the one thing
the ambient layer promises: it never blocks the turn.

The one place a model does run, `embeddings.py`, is worth being precise
about: `SentenceTransformerEmbedder` loads `bge-small-en-v1.5` locally via
`sentence-transformers` — a ~600MB weights download, not an API call, no
per-query round-trip — behind the optional `meristem[embed]` extra. Without
it, `HashEmbedder` (a deterministic 3-gram hash) keeps retrieval working end
to end, just without semantic matching. Even the one ML dependency is
local, and swappable for a stdlib fallback.

## Fail-loud, not silent

This principle is stated verbatim in the code more than once, because it was
learned the hard way. `doctor.py`'s module docstring gives the clean
version: the goal of `meristem doctor` is "to surface silent rot — schema
drift, embedding-model drift, liveness coverage erosion, dead retrieval
pipeline — before they turn into mystery hallucinations during a real tick."

Three instances back that up:

- **`edges.density`.** A populated store with zero edges means PPR has
  nothing to propagate through — "every claim that distinguishes Meristem
  from a vector store is false in practice" — but the check originally sat
  at `warn` (exit 0). The code comment records the cost: it "sat at `warn`
  ... while two real workspaces ran edgeless for months, which is precisely
  how the problem went unheard." It now fails above a threshold instead.

- **`hooks.heartbeat`.** Before schema v7, a hook wired into
  `settings.json` but pointed at a dead binary "produces zero evidence
  anywhere" — exactly what happened for 8 days after the DLMS→Meristem
  rename. Commit `a484388` makes every hook invocation write a
  `hook_heartbeat` row *before* deciding whether to act, so `doctor` can
  tell "wired but doing nothing" apart from "never wired at all."

- **The `pending_review` digest bug.** `Digest.pending_review` had been
  computed all along, but `_handle_session_start` never read it, so the
  signal meant to close the capture loop was dropped at "the one surface
  guaranteed to be read every session." Fix commit `2cedf6e` quotes the
  digest module's docstring for the general failure mode: "a health check
  nobody reads cannot prevent anything."

Each check existed already; its severity or surface let the failure go
unnoticed for weeks. The fix wasn't smarter detection — it was making the
failure loud enough that a human had to see it.

## Human disposes, agent never auto-accepts

`candidates.py`'s docstring states this as a structural property, not a
policy: "A candidate is deliberately *not* retrievable. `retrieve()` never
sees this table." Captured facts land in `candidates`, never in `atoms`,
until a human runs `meristem review --accept` (§6). That separation lets
capture stay permissive — an extractor with no real understanding of the
sentence can afford to guess, because a wrong guess costs "one keystroke to
reject," not a fact silently retrieved as truth six weeks later.

The same trust split is explicit in the MCP tool surface (§8).
`propose_fact`'s docstring calls itself "the low-risk write path": "A
proposal costs the user one keystroke to reject; a wrong `assert_fact`
becomes a fact that gets retrieved and believed later." `assert_fact`
writes directly to the atom store with no queue — the tool a human uses via
`meristem decide`, or an agent calls when it means to assert outright
rather than merely notice. The two exist side by side so an agent has a
low-trust path by default, and the high-trust path stays deliberate.

## Deterministic over probabilistic where possible

The first three principles are instances of one connecting choice: wherever
a deterministic check can answer a question, Meristem uses it instead of a
model — a deterministic check is auditable, cheap, and fails in ways that
can be caught, where a model's answer is none of those.

Liveness predicates (§4) are regex, AST, or SQL — never "ask the model if
this still seems true." Atom ids are a hash of `(type, topic_key,
summary_50w)` (§2, §6), so re-asserting the same claim is a lookup, not a
fuzzy dedup that could drift. Capture is marker-based pattern matching, not
an LLM extraction pass. The fail-loud checks in `doctor.py` are exact,
inspectable predicates — a count of edges, a heartbeat timestamp, a ratio of
empty results — not a judgment call. Determinism is what makes the silent
failures above detectable at all: a probabilistic check that quietly
degrades looks the same as one that's working; a deterministic one either
passes or it doesn't.
