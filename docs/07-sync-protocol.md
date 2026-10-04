# 7. Multi-repo / sync protocol

## What gets exported

`.meristem/atoms.sqlite` is local-only and gitignored — it is never
committed. Two devs' concurrent commits to a binary sqlite file are a hard
git conflict resolvable only by discarding one side wholesale, so the
shared substrate instead leaves the machine as text. `meristem export`
(`sync_protocol.export_jsonl`) writes `atoms`, `atom_summaries`, `edges`,
and `edge_evidence` — nothing else — to `.meristem/atoms.jsonl`, one sorted
JSON object per line, each tagged with its `_table`. Rows are sorted by
`(table, natural key)` before writing, so two stores holding identical data
produce byte-identical output and a git diff shows real changes only, never
reordering noise.

Six per-machine tables are deliberately excluded: `jobs`, `retrieval_log`,
`session_state`, `embedding_ledger`, `candidates` (not memory until
accepted), and `repo_state` (keyed by an absolute local path that can never
agree across two clones). Row identity for merge purposes isn't always the
primary key: atoms use their content-hash `id`; `atom_summaries` use
`(atom_id, resolution)`; edges use `(src_id, dst_id, kind, valid_from)`,
not the local autoincrement `id`, meaningless across two independently
grown databases.

## One file, or sharded

Default `[sync] mode = "git-jsonl"` writes the single file above, rewritten
whole on every export. A second, opt-in mode, `"git-jsonl-sharded"`,
partitions the same four tables across many files under `.meristem/atoms/`,
one per `shard_prefix_len`-hex-char (default 2, so 256) bucket of an atom's
content-hash id (`sync_protocol.shard_of`) — the same fan-out idea as
git's `.git/objects/xx/`. A summary or edge rides in the shard of the atom
that owns it. `export` in this mode only rewrites a shard whose text
actually changed, so touching one atom diffs one shard file, not the whole
store.

## `export` / `import` — not `sync`

The commands that do multi-repo reconciliation are `meristem export`
(store → JSONL) and `meristem import` (JSONL → store). `meristem sync` is
a different, unrelated command (SPEC §19): it catches the local store up
with new commits by running `ingest` + `embed`, and has nothing to do with
team sync despite the name — don't confuse the two.

Nothing triggers `export` automatically; a dev runs it by hand and commits
the resulting file(s) alongside their code. `import` can be automated:
`meristem import --install-hook` writes `post-merge` and `post-checkout`
git hooks that run `meristem import --quiet` detached in the background —
two hooks because `git pull` fires post-merge but not post-checkout, while
a branch switch (including the one `git clone` performs) fires the
reverse. These are the same managed hook files `meristem sync
--install-hook` writes into (SPEC §19.6): each lists every workspace using
the repo with its actions (`import`, `sync`), so installing either command
adds to the list rather than replacing the other's step, and `post-merge`
runs `import` then `sync` when both are wired. (`post-checkout` is only ever
`import`, and acts only on a branch checkout.) `import` is a pure union — an atom present locally but absent
from the import is left untouched. A real git merge/rebase that conflicts
on `atoms.jsonl` (or a shard file) is resolved by the hidden `meristem
merge-driver` command, registered by `meristem init` against the path(s) a
committed `.gitattributes` line names; it runs the same pure merge
function git calls directly, so it never leaves `<<<<<<<` markers.

## The merge: commutative per field

The plan's "commutative per-field merge" description holds up —
`sync_protocol.py` states the goal directly: every mutable column gets a
policy that is commutative, associative and idempotent, so the result
never depends on which side is "ours." There is no single last-write-wins
or confidence-weighted rule; each column has its own logic: earliest
non-null `valid_to`/`superseded_by` wins (a row can't un-close);
`liveness_kind` prefers the more precise predicate (ast > regex > sql >
none); `liveness_last_ok` takes the latest value; `pinned`/`archived` are
sticky-true; `valid_in_refs` is a set union; edge `weight`/`confidence`
take the max; edge `status` is sticky-`rejected`, else `live` beats
`suggested`. `atom_summaries.text` is picked by closeness to the
resolution's target word count, not recency or length, since retrieval
spends a fixed token budget per resolution.

This machinery resolves mechanical divergence on the *same* row. A genuine
disagreement — two devs asserting different claims under the same
`topic_key` offline — is not a field-merge problem: `import_jsonl` calls
`reconcile_topics` after loading, reusing `assert_fact`'s own policy to
close every topic down to one live survivor and raise a `CONTRADICTS` edge
to each closed duplicate, so the disagreement surfaces instead of getting
silently decided by import order.

## Connection to MIRRORS

MIRRORS (section 3) is discovered locally — `discovery._rule_mirrors` runs
during `ingest`'s edge-discovery pass, over the repo roots already
registered in one workspace's `meristem.toml`. Sync doesn't run discovery;
it only carries already-discovered edges, MIRRORS included, since `edges`
is one of the four synced tables. If dev A's workspace spans roots dev B's
doesn't, B receives A's MIRRORS edges on import and they merge under the
same edge policy as any other kind.

## A two-developer walkthrough

Alice and Bob clone the same repo and each run `meristem init`, registering
the merge driver locally against the `.gitattributes` line the first
`init` committed. Alice runs `meristem ingest --all`, asserts `meristem
decide "use JWT for service auth" --topic auth-mechanism --status CLOSED`,
runs `meristem export`, and commits `.meristem/atoms.jsonl`. Bob, having
run `meristem import --install-hook` once, does `git pull`: the post-merge
hook runs `meristem import --quiet`, and Alice's decision lands in his
`atoms.sqlite`, unioned with what he already had. Had Bob asserted a
conflicting fact under `auth-mechanism` first, `reconcile_topics` would
close one side and raise a `CONTRADICTS` edge, visible via `meristem why
<kept-id>`, instead of dropping his work silently. Had both instead edited
the same atom, the next `git merge` touching `atoms.jsonl` would invoke
`meristem merge-driver`, combining both edits per the column policy above
with no manual conflict resolution required.
