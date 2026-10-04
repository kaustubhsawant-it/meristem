# 10. Related work / positioning

Sections 1-9 described what Meristem is and why it's built the way it is.
This section places it against the nearest neighbors: what idea it borrows
outright, who the most credible named competitor is and where the gap
actually lies, one easily-confused adjacent project, and — honestly — what
in this document set is still a claim rather than a measurement.

## HippoRAG — the retrieval algorithm's prior art

Meristem's PPR retrieval (§3) is an independent implementation of the
HippoRAG idea (Gutiérrez et al., *HippoRAG: Neurobiologically Inspired
Long-Term Memory for LLMs*, NeurIPS 2024, arXiv:2405.14831): seed a query
into a knowledge graph and rank by Personalized PageRank rather than
nearest-neighbor similarity. It is not a fork or derivative of the HippoRAG
codebase. SPEC §17 documents a verified divergence: HippoRAG runs
`igraph.personalized_pagerank(..., implementation='prpack')` over an
in-memory `igraph.Graph` of LLM-extracted entities and passage edges, for
document QA; Meristem hand-rolls power iteration over typed atoms and 12
closed kind-weighted edge types stored in SQLite, for live code memory with
liveness invalidation. No HippoRAG source was copied, so the honest summary
is: same algorithmic idea, independently implemented, not a derivative.
Both projects are MIT licensed; SPEC §17 attributes the idea to HippoRAG
anyway, in good faith, even though no attribution clause is triggered.

## TencentDB Agent Memory

TencentDB Agent Memory (`github.com/TencentCloud/TencentDB-Agent-Memory`,
MIT) is the most credible named competitor in this space: Trendshift-
featured, an active Discord, multi-framework proxy integration across
Claude Code, Codex, CodeBuddy and others, and far more adoption today than
Meristem. A sandboxed clone-and-read of its source (no install) found three
concrete technical differences. Its retrieval is BM25 plus vector search
combined via RRF — not graph-centrality ranking. Its staleness handling is
TTL/retention-day expiry — a fact ages out on a clock, not because the code
it describes was re-checked and found to have changed. And its results are
count- or budget-capped rather than passed through anything like a
relevance floor: there is no equivalent of Meristem's abstention gate
(§3), which drops results below a measured relevance threshold rather than
just truncating a ranked list. Its "CodeGraph" tier also wraps a
third-party npm package rather than building the graph in-house. The one
benchmark in its own repository is PersonaMem, a general chat-memory
benchmark (48%→76%), not a code-specific one.

A widely-cited figure for this project — 1,540 tasks, 12-35% completion
improvement, 33-64% token reduction — does not appear anywhere in its
repository (README, CHANGELOG, or ROADMAP). That figure is third-party
press, not a number Tencent's own repo makes or substantiates, and it is
not repeated here as a verified claim about their system.

Net position: none of Meristem's core claims — liveness re-validation
against live code, bi-temporal validity, PPR retrieval, an abstention gate
— are addressed by this competitor's architecture. The gap between the two
projects today is adoption and evidence, not the underlying idea.

## PROJECTMEM

PROJECTMEM (arXiv 2606.12329, Malo & Qiu, University of Utah) is unrelated
to Tencent despite the similar-sounding name — an easy mix-up worth heading
off explicitly. It's a local-first, event-sourced memory and judgment layer
for coding agents, with a small independent open-source implementation at
`github.com/riponcm/projectmem`. Architecturally it sits closer to Meristem
than TencentDB Agent Memory does — both are local-first and per-repo rather
than team- or cloud-shaped — but that's a structural similarity, not an
equivalence claim; a fuller comparison would need a closer read of its
event-sourcing model than is available here.

## What's actually novel, and what's shared ground

Graph-based retrieval over typed facts is not, by itself, a novel category
— HippoRAG established the retrieval algorithm, and typed-fact stores show
up across the memory-for-agents space broadly (a competitive
scan also turned up MinnsDB, a bi-temporal knowledge graph project, and
newer arXiv work like PLACEMEM and ZORO, all working similar ground). What
looks distinctive to Meristem, based on the comparisons above, is not any
one of those ideas alone but a specific combination: liveness predicates
that re-validate a fact against the current state of the actual code
(rather than expiring it on a timer), a bi-temporal atom store paired with
typed, weighted edges rather than one or the other, and an abstention gate
that can suppress an entire retrieval when nothing clears a relevance floor
instead of always returning a ranked top-K.

That combination is a design claim, not yet a demonstrated one. This
documentation set is deliberately split into two tracks, and this section
belongs to the one that's writable now — architecture, verifiable by
reading the code. A second track, covering real-world results (token
savings, staleness actually caught, usage at scale), is explicitly deferred
until there are weeks of real usage data across multiple repos to report —
not because the numbers are unflattering, but because they don't exist yet.
Until then, the honest framing is: the mechanism is real and inspectable;
the benefit is argued from design, not yet measured.
