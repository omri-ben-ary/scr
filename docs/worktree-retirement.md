# Worktree retirement

How a Steward disposes of a task's worktree once the task reaches `merged`,
and why the disposal is a *quarantine* rather than a delete.

## The gap this closes

`agents/steward.md` currently ends its merge route with:

> … `scr task set <id> merged --tokens <n>`; merge the branch; **delete the worktree.**

That last clause is an unguarded instruction. It says what to achieve, not how,
and it has no notion of what must be true first. Left to interpretation across
many tasks, it degrades in two directions at once: worktrees that are never
retired, and worktrees retired while still holding work that exists nowhere
else.

Both happened in the `quantum-insertion` workload.

## Evidence from quantum-insertion (2026-09-07)

The repo had grown to **95.2 GB**. The cause was not accumulated cruft — it was
one artifact duplicated twelve times:

| Component | Size | Varies per worktree? |
|---|---:|---|
| `.lake/packages` (mathlib 6.3 GB + 9 pinned deps) | 6,874 MB | **No** |
| `.lake/build` (the project's own oleans) | 545–658 MB | Yes |

Fourteen worktrees existed. Seven were fully merged into `master` and clean —
dead weight that the "delete the worktree" step should have removed and did
not. Retiring them recovered **53 GB** (95.2 → 41.9 GB) and cost nothing: every
branch survived, because removing a worktree does not touch commits.

Two findings from that cleanup shape the design below.

**All fourteen worktrees pinned identical dependency revisions.** The two
distinct `lake-manifest.json` hashes differed only by CRLF vs LF; every rev
matched. So ~99% of the footprint was redundant by construction. This makes a
shared package store tempting (junction every `.lake/packages` at one store,
dropping a worktree from 7.5 GB to ~0.7 GB) — but see "Why not a shared store,
yet" below.

**All value-bearing content in the entire repository was ~200 MB.** Everything
else was reproducible cache. Retirement therefore has a large upside and a very
small blast radius — *provided* the small part is identified correctly.

The step that earned its keep was identifying it: `qi-mission-9` held an
untracked `missions/QI10/` folder that existed in **no commit anywhere**. It
would have been destroyed by an unguarded delete.

## Design: quarantine, never delete

The reference implementation is `hooks/retire_worktree.sh`. Its core invariant:

> **The script never deletes anything.** It moves the worktree to
> `<repo>-RETIRED/<timestamp>-<name>/` outside the repo. Reclaiming disk is a
> separate, deliberate act by Omri.

Two gates and a backup precede the move:

1. **Gate 1 — merged.** `git merge-base --is-ancestor <branch> <main>`. Refuses
   otherwise (exit 2). Retirement is for closed tasks only.
2. **Gate 2 — no uncommitted tracked changes.** Refuses rather than guessing
   (exit 3).
3. **Backup of untracked files**, verified by SHA-256 *before* anything moves.
   A mismatch aborts without touching the worktree.

Everything is `--dry-run` by default; `--execute` acts.

### Why quarantine rather than a guarded delete

Two bugs surfaced while building this, both in versions that looked correct and
passed their own checks:

**Bug 1 — path traversal.** The first version ran
`rm -rf "$WT_ROOT/$NAME"` behind a non-empty check and a `-d` check. Passing
`../..` satisfies both: it resolves to the parent of the repo — the user's
Desktop — and `[ -d ]` returns true. One typo from catastrophe.

**Bug 2 — the "tidy path".** The rewrite quarantined by `mv`, but first called
`git worktree remove --force` "in case it works cleanly". It did work: it
**deleted the tree**, so the `mv` had nothing to move and the quarantine folder
was empty. The script violated its own core invariant on its first run — while
its verification block printed three `OK` lines.

The lesson from bug 1 is that guards accumulate around a destructive primitive
faster than they cover it; removing the primitive removes the class. The lesson
from bug 2 is sharper:

> **Verify recoverability, not disappearance.** "Is it gone from `.worktrees/`?"
> is the wrong assertion — bug 2 passed it. The script now counts files
> readable in quarantine and fails loudly if the count is zero.

Quarantine also happens to be immune to the junction hazard discussed below:
`mv` renames a directory, it never recurses into it.

## Integration with the Steward

Amend the merge route in `agents/steward.md` to call the hook instead of
"delete the worktree", and route its refusals like any other check:

- exit 0 → retired; note the quarantine path in `review.md`.
- exit 2 (not merged) → the merge did not actually land. **Verify `main`'s tree,
  not the PR status** before anything else.
- exit 3 (uncommitted tracked changes) → `escalated` → `omri_queue`. The Scriber
  left work behind; that is a judgment call, not a mechanical one.

This mirrors how `statement_diff` already routes: mechanical pass proceeds
autonomously, ambiguity goes to Gate 1.

### Proposed workload key

`config/workloads.yaml` already carries every repo-specific fact the hook
needs — `repo`, `main_branch`, `worktrees`. Only the hook path is missing:

```yaml
quantum-insertion:
  repo: ../quantum-insertion
  main_branch: master
  worktrees: ../quantum-insertion/.worktrees
  retire_worktree: ../../scriptorium/hooks/retire_worktree.sh   # ← proposed
```

Invocation:

```sh
hooks/retire_worktree.sh \
  --repo ../quantum-insertion \
  --worktrees ../quantum-insertion/.worktrees \
  --main master --name qi-mission-10 --execute
```

**This PR does not edit `config/workloads.yaml`** — it had uncommitted local
changes at the time of writing, and mixing them into this branch would make the
diff unreviewable. Adding the key is a one-line follow-up.

A workload may point `retire_worktree` at its own script instead, the same way
`done_when_checker` and `statement_diff` live in the target repo.

## Why not a shared package store, yet

Since all worktrees pin identical revisions, junctioning every
`.lake/packages` at one shared store would cut a worktree from 7.5 GB to
~0.7 GB. It is also lower-maintenance than it first appears: the store holds
*pinned dependencies*, which change only when the manifest changes — a few
times a year, not on every push. Keying the store directory by manifest hash
makes even that maintenance-free, since a bump creates a new store while
worktrees on the old pin keep working.

It is nonetheless **deferred**, for a reason specific to this environment:
`git worktree remove` fails with `Permission denied` on this Windows setup
(observed on all seven retirements), so directory-level `mv`/`rm` is the
working path. If `.lake/packages` were a junction, a stray recursive delete
could follow it into the shared store — turning a local mistake into a global
one, and a 6.9 GB rebuild.

Retirement discipline captures most of the win without that exposure. Revisit
the store afterwards, and only with a delete path that unlinks junctions first.

## Adjacent finding

`~/.elan` held **12.2 GB** across four Lean toolchains, of which v4.33.0 and
v4.33.1 (6.1 GB) were referenced by nothing in the workload. Toolchain
retirement is a sibling problem to worktree retirement and is *not* addressed
here: `.elan` is shared across repos, so no single workload can safely decide.
It belongs in an Observer report, not a Steward action.
