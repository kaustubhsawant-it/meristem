# 13. Trust

What Meristem does with your data, and the guard that keeps secrets and personal
data out of memory that leaves the machine.

## Local-only by design

- The store (`.meristem/atoms.sqlite`) is a local file and is gitignored.
- Retrieval is graph walking plus local embeddings. There is no model call in the
  hot path and no network call at all in Meristem's own code. (With the optional
  `embed` extra, the embedding model weights are downloaded once by that library;
  set `HF_HUB_OFFLINE=1` after that to forbid further access.)
- Conversation capture is stdlib heuristics over your own messages: no
  summarisation service, nothing uploaded.
- The only way memory leaves a machine is the shared export you commit
  yourself ([Team memory](12-team-memory.md)). That is where the guard sits.

## The guard

`guard.py` is a deterministic, stdlib-only scanner. It reports a *kind* and a
span, never the matched value, and nothing in Meristem logs or stores a match.
It is tuned for precision, because a guard that cries wolf gets switched off:
hashes, UUIDs, version strings, credential-free URLs, dotted identifiers and
plain prose pass.

Secret kinds:

| kind | catches |
|---|---|
| `private-key` | PEM private key blocks |
| `aws-access-key`, `aws-secret-key` | AWS key ids, and secret keys in an assignment |
| `github-token`, `slack-token` | GitHub (`ghp_`, `github_pat_` ...) and Slack (`xox...`) tokens |
| `anthropic-key`, `openai-key`, `google-api-key` | `sk-ant-...`, `sk-...` (with a digit), `AIza...` |
| `jwt` | three-part JSON web tokens |
| `url-credentials` | `scheme://user:password@host` |
| `credential-assignment` | `password=`, `secret:`, `token=`, `api_key=` followed by a literal-looking value (references like `$VAR` or `{{x}}` pass) |
| `high-entropy` | long random-looking tokens that are not hashes or UUIDs |

Personal-data kinds: `email`, `phone`, and `excluded-term`.

### Where it applies

- **Capture.** A unit of conversation that trips the guard is never proposed to the
  review queue. `capture.explain(unit)` names this rule `guard`, and
  `meristem review --noise` lists already-pending candidates the guard would now drop.
- **Export.** An atom whose topic or any summary trips the guard is withheld from
  the shared export as a whole, together with its summaries and every edge that
  touches it (an edge to a missing atom would not import). A lone evidence row
  that trips it is withheld by itself.
  `meristem export` reports it: how many atoms were withheld, then
  `<atom id>: <kind>` lines (first ten), then the fix. Ids and kinds only, never
  the value. Called without a report list, the library logs the same pairs as
  warnings.
- **Doctor.** The `guard.store` check counts live atoms in your local store that
  carry findings and names ids and kinds. Those atoms stay in your local store
  until you archive them or re-assert them without the value; they just never
  leave in the export.

What the guard does *not* do: it does not scan your source code or your commits
(the ingesters index the repo as it is), it does not clean the local store, and
it is pattern-based, so a secret in an unusual shape can pass. It is a safety
net for the export, not a substitute for keeping secrets out of the repo.

### Configuration

```toml
[guard]
enabled = true            # default; false turns the scanner off everywhere
allow_patterns = []       # regexes; a finding whose matched text matches any is ignored

[capture]
exclude_terms = []        # your own denylist (e.g. names of people a project tracks)
```

`allow_patterns` are matched with `re.search` against the matched text and are
the escape hatch for a known-safe false positive (for example a documented
example token). `exclude_terms` are case-insensitive whole-word matches and are
reported as the `excluded-term` kind; they are empty by default because only you
know what is personal in your project.

## Review is a human step

Captured facts are proposals. A candidate is not memory until a person accepts it
in `meristem review`, and agent suggestions (see [CLI + MCP](08-cli-mcp.md)) are
advisory only and never change a candidate's status.
