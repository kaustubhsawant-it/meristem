# 15. Performance

Measured numbers for the operations a user waits on, and an honest account of
what they do and do not show.

## Read this first: the benchmark does not show monorepo scale

The symbols ingester (tree-sitter symbol graphs) is capped at
`symbols.MAX_ATOMS = 200` atoms **per ingest run**. A synthetic repo ten times
larger therefore does not produce ten times the atoms: the number of atoms, the
store size and the cold-index time **plateau** as lines of code grow. The table
below is real, but what it measures is the shipped, capped behaviour. It is
**not** evidence that Meristem indexes a large monorepo completely, and it should
not be quoted as such. Full-coverage indexing of very large repos is not a
claim this documentation makes. (SPEC §14 describes the scaling design; its
numbers are targets and design arguments, not results from this benchmark.)

What the table does support: cold indexing, incremental sync after a commit and a
query stay at fractions of a second on repos from 10k to 100k lines, because the
amount of work per run is bounded.

## Scale benchmark

`tools/bench_scale.py` generates a *synthetic* repo in a temporary directory
(never a real one), commits it, and times:

- `meristem init --all` (cold index: init, ingest, embed),
- K incremental commits, each followed by `meristem sync --force`,
- `meristem query` over a fixed set of 20 queries, p50 and p95,
- the size of the `.meristem/` directory and the atom count.

It runs everything as subprocesses against the source tree, with the deterministic
hash embedder and `HF_HUB_OFFLINE=1`, so no model is downloaded and the numbers
measure Meristem rather than a network or a GPU. It points `HOME` at the temp
directory so it cannot touch real state. Query times are whole-process wall
clock: interpreter start, store open and retrieval.

```bash
python tools/bench_scale.py --files 100  --loc-per-file 100 --commits 5   # 10k LOC
python tools/bench_scale.py --files 1000 --loc-per-file 100 --commits 5   # 100k LOC
```

Flags: `--files`, `--loc-per-file`, `--commits` (K), `--keep` (keep the temp repo).

Result, one run on a developer laptop (macOS, Python 3.14, hash embedder):

| files | LOC | init --all | sync (median / max, K commits) | query p50 | query p95 | store | atoms |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 | 10000 | 0.5s | 0.49s / 0.56s (K=5) | 0.16s | 0.17s | 4.3 MB | 439 |
| 1000 | 100000 | 0.5s | 0.50s / 0.55s (K=5) | 0.17s | 0.19s | 4.5 MB | 489 |

Ten times the code, essentially the same time and store size: that is the
`MAX_ATOMS` cap at work, not scale-independence. Treat these as one machine, one
run, one synthetic code shape. With the real `embed` extra, embedding cost is
higher than with the hash embedder used here.

## Statusline fast path

The Memory Pulse statusline runs on every assistant turn, so its cost is paid
constantly. `meristem statusline --compact` through the full CLI pays the cost of
importing the command-line framework. The `meristem-statusline` console script
(what `meristem hooks install --statusline` wires when it is on `PATH`) runs
`meristem.statusline.main`, which uses plain `sqlite3` to open the store
read-only, reads two counts, and shells out to git only for the freshness count
and today's tick count. No embeddings, retrieval or migrations are loaded.

Measured on the same laptop, 40 runs each, a small repo (9 live atoms), whole
process wall clock:

| invocation | p50 | p95 |
|---|---:|---:|
| bare Python interpreter start (`python -c pass`) | 12 ms | 14 ms |
| fast path (`python -m meristem.statusline`) | 75 ms | 80 ms |
| full CLI (`meristem statusline --compact`) | 124 ms | 129 ms |

The fast path was timed as `python -m meristem.statusline`, which runs the same
function the console script calls; the script itself adds only a small launcher
shim. Expect roughly 60 to 80 ms; a larger store or a slow `git` on a large repo
will move it. The line always renders: any failure degrades to a shorter line
rather than an error in your status bar.
