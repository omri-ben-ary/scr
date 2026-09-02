# Hashes and verification

Git already content-addresses every blob and commit. Scriptorium adds a second,
*semantic* layer of hashes on top of it, for three reasons git's own hashes
cannot serve:

1. Git hashes bytes. Two task records that differ only in YAML key order,
   quoting, or trailing whitespace get different blob hashes but are the same
   record. Scriptorium hashes the *canonical form*, so a hash names a meaning.
2. Git hashes files. The dependency graph is spread across every task record;
   there is no git object whose hash is "the blocks graph". Scriptorium
   computes one.
3. Git hashes history. The event log must be tamper-evident *within* a commit
   too — an agent that rewrites an old event in the same commit it appends a
   new one should be caught. A hash chain over events does that.

Everything below is recomputable from the working tree with `scr verify`, and
nothing below is a source of truth: if a stored hash and a recomputed hash
disagree, the files win and the disagreement is the finding.

## Canonical form

`canon(x)` = JSON, keys sorted, separators `,` and `:`, no whitespace, UTF-8,
non-ASCII kept as-is, `NaN`/`Infinity` rejected. Floats are emitted with
Python's shortest round-trip repr. Every hash is `sha256(canon(x))`, hex.

`H(tag, *parts)` = `sha256("scriptorium/v1/" + tag + "\n" + "\n".join(parts))`.
The tag prevents a task hash from ever colliding with a graph hash of the same
bytes; the version prefix lets the scheme change without silent
misinterpretation.

## Task hash

`task_hash(T)` = `H("task", canon(record))` where `record` is the parsed
`task.yaml` with the volatile fields removed: `hash` itself and nothing else.
Everything a task *is* — status, edges, budget, priority inputs — is in the
hash, so any change to a task changes its hash. `spec.md`, `log.md`,
`report.md`, `review.md` are hashed separately as `docs_hash` =
`H("docs", sha256(spec), sha256(log), sha256(report), sha256(review))` and
both are recorded in the snapshot; the doc hashes catch edits to the prose,
the task hash catches edits to the state.

## Graph hashes

For each stored edge set `E ∈ {blocks, unlocks, informs, supersedes}` and the
derived set `conflicts`:

`edge_set_hash(E)` = `H("graph/" + name, *sorted(canon(e) for e in E))`

An edge is a small dict: `{"from","to"}` for blocks/unlocks/supersedes,
`{"from","to","weight","reason"}` for informs, `{"a","b"}` with `a < b` for
conflicts. Sorting makes the hash independent of discovery order.

`graph_root` = `H("graph", blocks_hash, unlocks_hash, informs_hash,
supersedes_hash, conflicts_hash)`.

The graph root changes if and only if some edge changed. Two snapshots with the
same graph root have identical graphs, whatever else changed.

## Event chain

`events/events.jsonl` is append-only. Each line is an event dict with two
special fields: `prev`, the `h` of the preceding line (64 zeros for the first),
and `h` = `H("event", canon(event without h))`. Because `prev` is inside the
hashed content, rewriting any event invalidates every later `h`.

`events_head` = `h` of the last line. `events_count` is recorded beside it so a
truncated log is detectable even though truncation leaves a valid chain.

## State root

Leaves: for every task in `tasks/`, sorted by id, `leaf = H("leaf", id,
task_hash, docs_hash)`. Merkle-fold pairwise (odd leaf duplicated) to
`tasks_root`. `config_hash` = `H("config", *sorted(sha256 of each file under
config/))`.

`state_root` = `H("state", tasks_root, graph_root, events_head,
str(events_count), config_hash)`.

The state root is *the* identity of the operation at a moment. `scr snapshot`
writes it, with all its inputs, to `state/STATE.json`, and the pre-commit hook
refreshes it so every commit carries the state root that matches its tree.

## What `scr verify` checks

1. **Snapshot** — `state/STATE.json` recomputes to itself from the tree.
2. **Chain** — every event's `prev` and `h` are consistent; count matches.
3. **Replay** — folding the state events per task yields each task's current
   `status`; folding the edge events yields each task's current edge fields.
   This is the "everything is derivable" guarantee: the records are a cache of
   the log, and verify proves the cache is faithful.
4. **Invariants** — blocks and supersedes acyclic; exactly one origin per task
   and origin chains terminate; every open non-milestone task reaches an open
   milestone (orphans are warnings, not errors); blocked_by references exist.
5. **Graph view** — `graph/graph.json` has the current graph root.

Any error makes verify exit non-zero, which is what the pre-commit hook uses to
refuse a commit that would leave the repo in an unverifiable state.

## Versioning

Each snapshot names its predecessor: `STATE.json` carries `parent_state_root`.
Snapshots therefore form their own chain, parallel to git's commit DAG, and a
later `scr history` can walk them without git. `scr diff-state A B` reports
which tasks, edges, config files, and event ranges differ between two roots,
using nothing but the leaf hashes recorded in each snapshot.
