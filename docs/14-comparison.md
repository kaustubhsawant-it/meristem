# 14. How Meristem compares

An honest comparison with the three things most people use today. For how
Meristem relates to specific published research and projects, see
[Related work](10-related-work.md); this page does not go beyond what that page
says about named systems.

## Plain `CLAUDE.md` / `AGENTS.md` files

A hand-written instruction file in the repo. Every agent that supports the
convention reads it at the start of a session.

Where the file is better:
- **Zero setup and no dependency.** It is a text file. Nothing to install, nothing to run.
- **Fully legible and reviewable.** Everything the agent is told is on one page you wrote.
- **Works across agents** that read the convention, with no integration.

Where Meristem is better:
- **It notices when a fact stops being true.** A file says the same thing forever
  and nothing flags the sentence that went stale after a refactor. Meristem facts
  carry liveness predicates that are re-checked against the current code, and a
  failed one is marked stale rather than served with confidence.
- **It scales past one page.** A file that grows is injected whole, or ignored.
  Meristem retrieves the few facts relevant to the current prompt and token-caps
  them.
- **It can say "nothing relevant".** An abstention gate suppresses retrieval when
  nothing clears a relevance floor, instead of always returning something.
- **Typed facts with provenance.** Invariants, decisions, owners and schema
  facts are distinct, linked by typed edges, and `meristem why` shows where one
  came from and re-checks it now.
- **Bi-temporal.** A superseded decision is closed with an end date, not deleted,
  so you can ask what was true at a point in time.

Where Meristem is worse: it is more to set up and understand, and a good, short,
current instruction file beats a poorly maintained store. They also combine well:
keep the file for conventions a human wants to state outright.

## Cursor rules

Project rules files that steer the editor's agent, scoped by glob or always on.

Where rules are better: native to the editor, no extra process, and they apply
automatically in the right files without anyone writing a query.

Where Meristem is better: the same points as above (re-validation, abstention,
typed and time-aware facts), plus a memory that is *captured from work* (a review
queue of facts proposed from your conversations) rather than only hand-written,
and the same store usable from more than one agent over MCP.

Where Meristem is worse: Cursor gets only the MCP tools, not the ambient layer.
Setup writes the MCP entry for it, but it has to call the tools explicitly.

## Vector-memory tools in general

Tools that embed chunks of text or past conversations and retrieve by similarity.

Where they are better: typically simpler mental model, no schema to learn, and
similarity search is good at fuzzy recall over unstructured text.

Where Meristem is better, as a design claim:
- **Facts re-validate against the live repo** rather than expiring on a timer or
  never.
- **Abstention.** Similarity search returns a nearest neighbour whether or not it
  is relevant; the relevance gate lets Meristem stay quiet.
- **Typed, linked facts** and graph retrieval (Personalized PageRank over edges),
  so a constraint can surface because it is connected to the code you are touching
  even when it shares no vocabulary with it.
- **Time.** Bi-temporal validity keeps history instead of overwriting it.

Be careful with that list: it is argued from design, and published evidence for
the combination is still thin ([Related work](10-related-work.md) says the same).

## Where Meristem is heavier or weaker, plainly

- **Setup and weight.** It is a Python package with tree-sitter dependencies, a
  local SQLite store and optional embedding models. `meristem setup` makes the
  wiring one command, but it is still more than a text file.
- **Python dependency.** You need Python 3.11+ (or `uvx`) on every machine that
  uses it.
- **The deepest integration is Claude Code.** Hooks, per-turn injection, the
  liveness watcher, handoffs, the statusline and skills are Claude Code only. Other
  agents get the MCP tools and call them explicitly; Codex and Gemini CLI get a
  config snippet rather than automatic wiring.
- **Capture is noisy.** Conversation capture proposes durable facts from your
  messages. Measured against real review decisions, about 28% of proposals were
  worth keeping after the latest precision work (up from about 21%). That is why
  everything captured goes to a review queue and is never memory until a person
  accepts it.
- **Scale is not demonstrated.** The symbols ingester is capped per run and the
  benchmark plateaus ([Performance](15-performance.md)).
- **Windows is untested.** CI has an advisory Windows leg that has never run on a
  real runner.
- **Young.** The mechanism is inspectable; the benefit is argued from design and a
  small amount of dogfooding, not from a large study.
