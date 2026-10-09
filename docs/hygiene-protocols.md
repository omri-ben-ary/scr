# Hygiene protocols

How the system keeps its own git hygiene in `quantum-insertion` and in the scriptorium tree, so that
stale worktrees, stale lock files, branch drift and content that exists in only one place are each
**detected, repaired or quarantined, and reported** by the watcher / `scr sweep` / `scr retire`, and
none of it ever becomes an item in Omri's queue.

This is a plan (T-0060, `kind: survey`). It changes no tooling. Every detector below extends a check
that exists today; every repair respects the never-delete invariant of `docs/worktree-retirement.md`;
every follow-up in §11 is one Toolsmith session. File:line references are to the tree as of
2026-09-22 (scriptorium `main` at `55e964c`; quantum-insertion scripts as tracked on `origin/master`).

Origin: T-0022 (2026-09-17). Omri's ruling, verbatim from the answer event (seq 1232):

> Git hygiene (worktree pruning, stale locks, master-behind detection) is the system's job, never an
> Omri-queue item; the system already owns it via scr sweep / scr retire. The Desktop checkout's
> branch is Omri's working copy (uncommitted CLAUDE.md/README/.gitignore edits; missions/LOOPY and
> loopy.toml exist in no branch) and is not a system question.

## 1. Principles

1. **Ownership.** Hygiene is owned by the watcher tick (`scr watch`, `scr/watch.py:192-382`) and the
   two commands it drives, `scr sweep` (`scr/ops.py:298-338`) and `scr retire` (`scr/ops.py:342-381`).
   No hygiene finding is a task record. Only Intake and the Concierge may create tasks
   (`agents/_common.md:7`), and §9 tells Intake to refuse this class.
2. **Never delete; move.** `docs/worktree-retirement.md:56-58`: "The script never deletes anything. It
   moves the worktree to `<repo>-RETIRED/<timestamp>-<name>/` … Reclaiming disk is a separate,
   deliberate act by Omri." `scr/gitops.py:4`: "never delete or force: no `rm -rf`, no `push --force`,
   no `worktree remove`". This plan adds exactly two sanctioned deletions, both of things that carry no
   content: a **zero-byte** `*.lock` file older than `STALE_LOCK_S` (already the rule at
   `scr/gitops.py:5-8, 187-188`), and `git worktree prune` of an administrative entry whose directory
   is already gone (already run inside `retire`, `scr/ops.py:372`). Everything else moves: worktrees to
   `<repo>-RETIRED/`, non-empty old locks to `<git-common-dir>/_to_delete/`, orphan directories to
   `<repo>-RETIRED/<stamp>-orphan-<name>/`.
3. **Verify recoverability, not disappearance** (`docs/worktree-retirement.md:91-93`). Every repair
   ends by reading back what it preserved (a sha on origin, a file count in quarantine) and fails
   loudly if the read-back is empty (`scr/ops.py:376-378`).
4. **One reference ref.** Every mechanical judgment about "merged", "in scope", "statement changed" or
   "drift" is made against `origin/<main_branch>` immediately after `git fetch origin`. The local
   `<main_branch>` of a workload repo is a cache no service reads (§7). The scriptorium's own workload
   is the one exception, by construction (§7.3).
5. **Nothing exists in only one place across a tick.** A running worktree is committed every tick
   (`scr/watch.py:228-235`, `README.md:77-78`); this plan extends the same guarantee to every open
   task's worktree in both workloads, to mission worktrees, and to leftovers found at retirement (§6).
6. **Report by event, aggregate by day.** Each repair journals one `system` event with a fixed name
   (§10). Hygiene events are **not** alarm cards unless a human must act; the daily pass writes one
   digest file. The 2026-09-16 incident — 465 false drift files on T-0029 became 583 cards
   (`scr/watch.py:240-241`, `scr/ui.py:162-165`) — is the failure mode this rule prevents.
7. **The Desktop checkout is Omri's.** The system reads it and writes one informational line about
   it (§8). It never changes a branch ref, the index or the working tree there.

## 2. What runs today, and the gaps (calibration 2026-09-22)

### 2.1 Existing surface

| Concern | Exists today | Where | Gap |
|---|---|---|---|
| Checkpoint of running worktrees | every tick, `checkpoint_commit` | `scr/watch.py:228-235`, `scr/ops.py:173-193` | only statuses `dispatched/in_progress/checkpoint` (`watch.py:214`); **scriptorium tasks skipped** (`watch.py:225-226`, flag `tooling`) |
| Wrong branch in a task worktree | `checkpoint_skipped` event, alarm card | `scr/ops.py:183-184`, `scr/ui.py:117` | none (keep) |
| Uncommitted mission work | `scr sweep [--merge]` commits, pushes, PRs | `scr/ops.py:298-338` | first workload only (`ops.py:301`), skips `T-*` dirs (`ops.py:305`), never scheduled — **zero `sweep` events in `events/events.jsonl`**; runs only via chore `sweep-worktrees` (`ui/chores.yaml:20-28`) |
| Retire a merged worktree | `scr retire <name> --execute`, quarantine, gates 2/3, empty-quarantine check | `scr/ops.py:342-381` | gate 1 tests the **local** `<main>` (`ops.py:358`) with no fetch; exit 3 routes to `omri_queue` (`agents/steward.md:34`, `docs/worktree-retirement.md:106`); only called right after auto-land (`watch.py:353`) |
| Stale git locks | empty `*.lock` > 60 s removed **after** a git call fails with a lock error, then one retry | `scr/gitops.py:19, 74-78, 174-192` | reactive only; proactive scan exists but runs only in `migrate` (`scr/admin.py:84-86`); `ui/chores.yaml:25` says "cleared automatically" |
| Journal lock | `.scr/journal.lock`, taken over after 180 s | `scr/journal.py:19-20, 47-48` | four `journal_failed … busy for 60s` lines on 2026-09-16 (`.scr/journal.log`) — a slow holder, not a stale lock; uncounted |
| Fetching `origin` | only in `spawn` and `land --merge` | `scr/ops.py:150, 278` | the drift base `origin/<main>` (`watch.py:243`) is as old as the last spawn/land |
| Local `<main>` vs `origin/<main>` | `ensure_main` folds a side branch and pushes (scriptorium) | `scr/admin.py:113-150` | called only from `migrate` (`admin.py:96-97`) and by hand; no fetch; nothing for quantum-insertion (by design, §8) |
| Scriptorium worktree landing | `accepted → merged` with **no merge** ("change already on main") | `scr/watch.py:335-340` | T-0078 (in `omri_queue`) owns the fix; until it lands, scriptorium task branches are never ancestors of `main` |
| Reporting | `sweep`, `worktree_retired` events; retire refusals as `needs` lines | `scr/ops.py:337, 380`, `scr/watch.py:357` | not alarm kinds (`scr/ui.py:117`); `needs` lines live only in the dashboard and `reports/steward_watch.log` |

### 2.2 Calibration facts (read-only observation, not to be acted on here)

quantum-insertion (`C:\Users\User\Desktop\quantum-insertion`):

| Fact | Value |
|---|---|
| checkout branch | `audit-controlled` (tracking `origin/audit-controlled`) |
| local `master` vs `origin/master` | 0 ahead, **40 behind** (31 on 2026-09-17, 6 on 2026-09-03) |
| tracked modified / untracked paths in the checkout | 5 / 22 (`missions/LOOPY/`, `loopy.toml`, `_to_delete/`, `missions/SCRIPTORIUM/`, 12 `missions/QI*/` folders, …) |
| worktrees (`git worktree list`) | 26 incl. the checkout; 12 mission-style, 13 `T-*` |
| branches already ancestors of `origin/master` | 17 of 26 (retire-eligible on gate 1) |
| worktrees with uncommitted tracked changes | 2: the checkout (5) and `T-0084` (1) |
| live `index.lock` anywhere under `.git` | none |
| `_to_delete/` at the repo root | untracked, **not gitignored**, 12 entries + `qi21-setup/` (zero-byte `index.lock*`, `*-locked` markers, one `HEAD.lock`) |
| `.git/_to_delete/` | one file, `qi7.lock` (0 B, 2026-08-30) |
| quarantine folders | no `quantum-insertion-RETIRED/`; a `quantum-insertion-BACKUP-20260907/` exists |

scriptorium (`C:\Users\User\Desktop\scriptorium`, worktrees in `..\scriptorium-worktrees`):

| Fact | Value |
|---|---|
| checkout branch | `main`, **3 ahead** of `origin/main`, 0 behind (`push: true` is configured, `config/workloads.yaml:22`) |
| task worktrees | 21; 17 of them have uncommitted tracked changes (1–10 files each) |
| branches not ancestors of `main` | 1 (`task/T-0059`, 2 ahead) |
| lock files under `.git` | none; no `_to_delete/` directory (`.gitignore:22` ignores `_to_delete*`) |

The T-0022 brief (2026-09-03: "5 prunable worktrees, master 6 behind, two stale locks") was stale by
2026-09-17 ("prunable worktrees already gone, master 31 behind, `_to_delete/` holds 12 files") and is
stale again today. A hygiene finding filed as an `approve` item ages faster than Omri's queue turns
over; that is the case study for §9.

## 3. Protocol A — stale worktrees

A worktree is *stale* when it is finished but still occupies the worktree root, when git still lists
it although its directory is gone, or when a directory sits in the worktree root that git does not
know. Four sub-conditions, one detector pass.

**Detector** — run by the watcher once a day (a `hygiene:<date>` flag in `.scr/watch.state.txt`, the
same mechanism as `intake:<date>` at `scr/watch.py:360`) and on demand as `scr sweep --dry-run`
(§11 F1); the auto-land path keeps calling `retire` immediately after a verified merge
(`scr/watch.py:353`). Per workload `wl` in `config/workloads.yaml`, in `wl.repo`:

1. `git fetch --quiet origin` (`Git.fetch`, `scr/gitops.py:136-137`).
2. `git worktree list --porcelain` → the set of registered worktrees (path, branch, `locked`).
3. For each directory `d` under `wl.wt_root` (`ops.workload`, `scr/ops.py:41`):
   - **A1 finished**: `d` is registered; branch `B = git -C d rev-parse --abbrev-ref HEAD`;
     `git merge-base --is-ancestor B origin/<main>` is true; `git -C d status --porcelain
     --untracked-files=no` is empty; and either `d` is `T-<id>` with the record's status in
     `{merged, closed, rejected}`, or `d` is a mission worktree whose branch is on origin
     (`git branch -r --contains B` lists `origin/B` or `origin/<main>`). **Age gate for the daily
     pass:** newest mtime under `d` (`Watcher.newest_change`, `scr/watch.py:176-189`) older than 24 h,
     and no `launch`, `review_started` or `discuss` event naming the task in the last 24 h — a
     Steward Discuss session opens *in* the task's worktree (`scr/discuss.py:314`; the T-0021 and
     T-0025 Discuss jobs of 2026-09-19 ran inside merged tasks' worktrees).
   - **A2 phantom**: a registered path that does not exist on disk.
   - **A3 orphan**: `d` exists but is not a worktree of `wl.repo` (`git -C d rev-parse
     --git-common-dir` fails or resolves elsewhere).
   - **A4 locked**: `.git/worktrees/<name>/locked` exists (the "one locked" `qi-mission-7` of the
     T-0022 brief).
   - Otherwise the worktree is *live* and belongs to Protocol D.

Cost: one fetch, one `worktree list`, and ~26 ancestor tests per repo; well under a minute; fine daily.

**Repair / quarantine**

- A1 → `retire(root, name, execute=True)` with gate 1 rewritten to test `origin/<main>` after the fetch
  (F2); gate 2 (uncommitted tracked, exit 3) is handled by Protocol D4 *before* retiring, so exit 3
  stops being a route to Omri. The quarantine path, the empty-quarantine check and `git worktree
  prune` after the move stay as they are (`scr/ops.py:363-378`).
- A2 → `git worktree prune` (F2). Prune only removes bookkeeping for directories that are already
  gone and honours `locked`; it is the second sanctioned deletion of §1.
- A3 → move `d` to `<repo>-RETIRED/<stamp>-orphan-<name>/` with the same read-back count as
  `retire` (F2). Never `rm`.
- A4 → nothing. A lock is a deliberate human act; the digest lists it with its age and reason file.
- A retire refusal with exit 2 *after* a fetch, for a task whose record says `merged`, is a truth
  mismatch (the merge was reverted, or the record lied): event `hygiene_mismatch`, an alarm card
  (§9 bin 3), no repair. **Until T-0078 lands, every scriptorium task worktree would trip this** —
  `accepted → merged` there is a status change with no merge (`scr/watch.py:335-340`) — so F2
  suppresses the card for a workload whose `repo` is the scriptorium root until the landing path
  exists, and F2 is `blocked_by` T-0078.

**Report** — `worktree_retired` (exists, `scr/ops.py:380`), `worktree_pruned` (new), `worktree_quarantined`
(new, A3), `hygiene_refused` (new; code and reason), `hygiene_mismatch` (new; alarm). All are
`system` events in `events/events.jsonl`; the daily digest (§10) lists counts and the quarantine paths.

## 4. Protocol B — stale locks

A *stale lock* is a `*.lock` file under a repo's common git dir (`git rev-parse --git-common-dir`,
which covers `.git/worktrees/<name>/` for every worktree) left behind by a crashed git process:
`index.lock`, `HEAD.lock`, `refs/heads/<b>.lock`, `packed-refs.lock`, `config.lock`
(`scr/gitops.py:7-8` lists the three kinds seen on 2026-09-07 and 2026-09-15). A live git process
holds a lock that is non-empty within a minute (`scr/gitops.py:8`). `.git/worktrees/<name>/locked`
is **not** a lock file (Protocol A4).

**Detector** — every watcher tick (default 30 min: `--interval` falls back to
`checkpoint_every_minutes`, `scr/cli.py:388`, `config/limits.yaml:9`), per workload repo, and once
more before every git mutation that already retries on lock errors (`scr/gitops.py:74-78`).
The scan is `Git.clear_stale_locks` (`scr/gitops.py:174-192`) run proactively (F3), skipping
`objects/` for speed and anything under a `_to_delete` path (`gitops.py:183`). Classification:

| lock file | age | action |
|---|---|---|
| 0 bytes | > `STALE_LOCK_S` (60 s, `gitops.py:19`) | unlink (sanctioned deletion 1) |
| > 0 bytes | > 10 min | move to `<git-common-dir>/_to_delete/<stamp>-<relpath>` (never delete; `.git/_to_delete/qi7.lock` is the precedent) |
| any | younger | leave: a live process |

The scriptorium's own `.scr/journal.lock` needs no new repair: a holder older than `LOCK_STALE_S`
(180 s) is taken over by the next journal call (`scr/journal.py:47-48`), and the tick journals first
(`scr/watch.py:205`). The detector only *counts* `journal_failed` lines in `.scr/journal.log` per day
for the digest; a run of them means a slow holder, which is a performance problem, not a lock problem.

**Repair / quarantine** — as in the table. The one place a stale lock may exist inside Omri's own
`.git` (the quantum-insertion checkout's `index.lock`) is covered by the same rule: a zero-byte lock
has no content, and the shared `.git` also serves every worktree, so this is the system's domain
(§8). The repo-root `_to_delete/` folder of the Desktop checkout (12 stale lock and marker files) is
Omri's working tree: report only (§8).

**Report** — `stale_lock_cleared` (new; note = the relative paths removed or moved; actor
`steward-watch`). Never a card. The digest counts per repo per day; the same path cleared three times
in a day is listed by name (a crash loop somewhere else). This makes the claim at `ui/chores.yaml:25`
("Stale git locks are cleared automatically") true; today it is true only after a git command has
already failed.

## 5. Protocol C — branch drift

*Branch drift* is any divergence between what a service assumes a ref to be and what git holds:
the remote-tracking ref is old; the local main is behind or ahead of origin; a checkout sits on a
side branch; a task worktree is on the wrong branch; a task branch has commits that are not on
origin; a task branch's base is far behind the main it will merge into.

**Detector** — every tick, after the fetch of C1, per workload; all commands are reads:

| id | condition | check |
|---|---|---|
| C1 | remote-tracking ref stale | fixed by doing the fetch: `git fetch --quiet origin` at the top of the tick, once per repo (F3). Today only `spawn` and `land` fetch (`scr/ops.py:150, 278`), so the drift base at `scr/watch.py:243` is whatever the last spawn left. |
| C2 | local `<main>` vs `origin/<main>` | `git rev-list --left-right --count <main>...origin/<main>` → (ahead, behind). quantum-insertion today: (0, 40). scriptorium today: (3, 0). |
| C3 | checkout on a side branch | `git rev-parse --abbrev-ref HEAD` in `wl.repo` ≠ `<main>`. quantum-insertion: `audit-controlled`. |
| C4 | task worktree on the wrong branch | already detected by `checkpoint_commit` (`scr/ops.py:183-184`, event `checkpoint_skipped`, alarm card) and refused by `land` (`ops.py:215-216`). |
| C5 | task branch commits not on origin | for every open task with a worktree: `git rev-list --count origin/task/<id>..task/<id>` (or "no upstream"). |
| C6 | stale base | `git rev-list --count $(git merge-base task/<id> origin/<main>)..origin/<main>` — how far `<main>` moved since the branch was spawned (T-0025's spec had to say "respawn from origin/master first"). |

**Repair**

- C1: the fetch itself. On failure (offline), a log line and a `fetch_failed` count in the digest;
  the tick continues on the last-known refs; a card only after 24 h of failures.
- C2, behind only: **no service reads the local main** (§7, F2, F6), so no repair is *needed*. An
  optional, opt-in ref update `git fetch origin <main>:<main>` (a fast-forward of a branch that is
  not checked out; no working-tree change) is offered behind a workload key `hygiene.ff_local_main`,
  **default off**, and it is off for quantum-insertion under Omri's ruling: the local `master` there
  is reported (§8), not moved.
- C2, ahead only (scriptorium: the journal push did not happen): `ensure_main(root, g, main,
  push=True)` (`scr/admin.py:113-123`), i.e. a plain push. The tick already journals with
  `push=self.push_journal` (`watch.py:205`); F3 adds the read-back (`main == origin/main` after the
  push) and a `push_failed` count from `.scr/journal.log`.
- C2, diverged (both > 0): somebody committed to `origin/main` outside the journal, or to `master`
  outside a PR. No automatic merge. Event `branch_diverged`, alarm card (§9 bin 3); a Toolsmith or
  Steward Discuss session resolves it; never a queue item.
- C3, scriptorium: `ensure_main` daily from the watcher (F3). Its guards are right: it refuses on a
  dirty tree (`admin.py:130-131`; the tick has just journaled), fast-forwards when `main` is an
  ancestor of HEAD (`admin.py:135-140`), merges otherwise, and aborts on conflict (`admin.py:145-150`).
  C3, quantum-insertion: Omri's checkout; report only (§8).
- C4: no automatic repair. A scriber that switched branches violated its brief; the card is the
  Steward's cue to return the task. Keep as is.
- C5: `Git.push("task/<id>")` (`scr/gitops.py:166-167`). Today the checkpoint push is on only with
  `scr watch --push` (`watch.py:230`, `push=self.push`), while the scriptorium workload's `push: true`
  covers the journal only (`watch.py:60-65`). F3 makes a workload-level `push: true` also govern
  checkpoint pushes; turning it on for quantum-insertion is a config change, hence a chore (§9 bin 2).
  The three safety levels: (1) only in a worktree — never across a tick; (2) committed in the repo's
  object store — the checkpoint; (3) on origin — the push, within a tick when `push: true`.
- C6: no automatic repair (rebasing under a running scriber is unsafe). The number goes into the
  dashboard row and the Steward review brief; conflicts surface at `land` as today
  (`ops.py:274-276`).

**Report** — `branch_diverged` (new; alarm), `checkpoint_skipped` (exists; alarm), `stale_lock_cleared`
(§4), and the daily digest's drift table: per repo, checkout branch, (ahead, behind) of `<main>`,
number of open task branches with unpushed commits, the oldest base. `scr verify` is unchanged: it
checks records, not git (`scr/state.py:86-148`).

## 6. Protocol D — content that exists in only one place

*Uncommitted-only content* is anything that is not in git history: a dirty working tree, an
untracked file, a folder in no commit ("`missions/QI10/` existed in no commit anywhere",
`docs/worktree-retirement.md:48-50`). Its sibling, *committed-only-locally* content, is Protocol C5.

| id | where | detector (service, frequency) | repair | report |
|---|---|---|---|---|
| D1 | task worktrees, quantum-insertion, running statuses | `Git.dirty()` each tick → `checkpoint_commit` (`scr/watch.py:228-235`) — **exists** | commit `[checkpoint]` on `task/<id>`, push per C5 | `checkpoint_commit` (exists, `ops.py:190`) |
| D1′ | task worktrees, **any open status** (`submitted`, `accepted`, `returned`, `escalated`, `omri_queue`, `blocked`) | same, extended past `running` (`watch.py:214`) — F4. Cases: T-0025 parked with inputs "not committed on any ref" (chore `park-t-0025`, `ui/chores.yaml:13-18`); `T-0084` has one dirty tracked file today while under review | same | same |
| D2 | task worktrees, **scriptorium** | today skipped (`watch.py:225-226`); 17 of 21 dirty. F4 drops the skip; `checkpoint_commit` already resolves the task's own workload (`ops.py:178`, T-0036) and `push: true` is set | `[checkpoint]` on `task/<id>`, pushed | `checkpoint_commit` |
| D3 | mission worktrees (`qi-mission-N`) | `sweep(root, dry_run=True)` rows each daily pass (`ops.py:305-314`) — exists, unscheduled | `sweep(merge=False)`: commit `[sweep]`, push (`ops.py:318-323`). `--merge` (PR + merge into `<main>`, `ops.py:324-336`) runs automatically only under `--auto-land`; otherwise the `sweep-worktrees` chore | `sweep` (exists, `ops.py:337`) |
| D4 | worktree of a finished task with uncommitted tracked changes (retire exit 3) | gate 2 of `retire` (`ops.py:361-362`) | **new rule:** commit `[leftover] <id>` on `task/<id>`, push, then retire. Master is untouched, so nothing is reviewed into the agents' branch; if the leftover touches `QuantumInsertion/*.lean` the digest says so. Replaces `escalated → omri_queue` at `agents/steward.md:34` and `docs/worktree-retirement.md:106-107` | `leftover_committed` (new; sha and file list) |
| D5 | the quantum-insertion Desktop checkout | daily read-only measurement (§8) | **none** (Omri's ruling) | `hygiene_desktop` (new) → one informational line |
| D6 | the scriptorium checkout | the journal, every tick (`watch.py:205, 381`) with `push: true` — exists | same | `push_failed`/`journal_failed` counts in the digest |
| D7 | quarantine (`<repo>-RETIRED/`, `<repo>-BACKUP-<date>/`) | weekly inventory: count, bytes, oldest stamp | none automatic; reclaiming disk is Omri's deliberate act (`docs/worktree-retirement.md:57-58`) → a manual chore when the total exceeds a threshold (§9 bin 2) | digest |
| D8 | branches on origin with no worktree and not merged | weekly list from `git branch -r --no-merged origin/<main>` | none; level 3 of C5 is safe | digest |

Interplay with **T-0079** (checkpoint stages only in-scope paths; `git add -A` at `scr/gitops.py:144`
swept T-0021's four out-of-scope files onto master): the invariant here is *nothing exists only in a
worktree*, T-0079's is *nothing out of scope reaches main*. Both hold if out-of-scope files go to a
separate `[checkpoint-drift]` commit on the same task branch that `land` refuses to merge — the design
T-0079's own spec offers. F4 states this as a done_when line so whichever of the two lands second
keeps both.

## 7. The reference ref: `origin/<main_branch>` after `git fetch`

### 7.1 What Stewards and `check_done` diff against today

Every checker the Steward runs defaults to the **local** `master`, two-dot, with no fetch, in the
repo that contains the *script*:

- `missions/SCRIPTORIUM/scripts/check_done.py:128` — `ap.add_argument("--base", default="master", help="git ref the scope check diffs against")`
- `check_done.py:116` — `sh(["git", "diff", "--name-only", base, "--", "QuantumInsertion"])`
- `check_done.py:46` — `REPO = HERE.parents[2]`; `:55` — `subprocess.run(cmd, cwd=REPO, …)`
- `missions/SCRIPTORIUM/scripts/statement_diff.py:59` — `ap.add_argument("--base", default="master")`
- `statement_diff.py:46` — `git("diff", "--name-only", base, "--", "QuantumInsertion")`; `:53` — `git("show", f"{base}:{path}")`; `:37` — `REPO = HERE.parents[2]`; `:41` — `cwd=REPO`
- the review brief the watcher hands the headless Steward, `scr/watch.py:395` — "run statement_diff against {wl.main}" (the bare branch name from `config/workloads.yaml:3`)
- `agents/steward.md:17` — "Run the workload's `done_when_checker` on the scope decls and `statement_diff` against main"; `:19` — "rebase on main"; `:33` — "Verify **main's tree**, not the PR status"
- `scr/ops.py:358` — `retire` gate 1: `if not mg.is_ancestor(br, wl.main)` (local, no fetch)

The two places that get it right:

- `scr/ops.py:277-279` (`land --merge`): `mg.fetch()` then `mg.remote_contains(f"origin/{wl.main}", br)`
- `scr/watch.py:238-244` (scope drift): base = `origin/<main>` if it exists, three-dot `{base}...HEAD`, with the comment "Never the local main: that can sit far behind origin and then every file merged since looks like drift (2026-09-16: 465 false files on T-0029 -> 583 alarm cards)"
- `scr/ops.py:150-151` (`spawn`): fetch, then base the task branch on `origin/<main>`

### 7.2 The rule

**R1.** The reference for every mechanical judgment in a workload repo is `origin/<main_branch>` read
immediately after `git fetch --quiet origin` in that repo. The local `<main_branch>` is a cache that
no service may read for a decision.

**R2.** A diff "of the task" is taken from the merge base: `BASE=$(git merge-base origin/<main> HEAD)`
in the task worktree, then `git diff --name-only "$BASE"` (working tree included) or
`"$BASE"...HEAD` (commits only; after a checkpoint the two agree). A two-dot diff against
`origin/<main>` itself, or against the local `master`, attributes to the task every commit that
landed on `<main>` since the branch was spawned. For `statement_diff` that is a **false Gate 1**, i.e.
an Omri-queue item manufactured by hygiene failure.

**R3.** The Steward runs the *worktree's* copy of the scripts from the worktree. `REPO =
HERE.parents[2]` (`check_done.py:46`) means the Desktop checkout's copy diffs Omri's working tree,
not the task branch. Both scripts are tracked on `origin/master` since PR #49 (f9bcfaa), so every
worktree spawned after 2026-09-15 has them.

**R4.** `retire` gate 1 becomes `fetch` + `is_ancestor(br, f"origin/{wl.main}")`, mirroring `land`
(F2). With the local `master` 40 behind, today's gate refuses (exit 2) branches that are genuinely
merged.

### 7.3 The scriptorium workload is the exception, by construction

Its worktrees are spawned from the checkout's **HEAD**, not `origin/main` (`scr/ops.py:147-148`,
docstring `:125-130`: "on 2026-09-19 origin/main sat 1075 commits behind the checkout"), because the
journal commits locally first. So for `repo: .` the reference is the local `main` (which is HEAD),
and the invariant that makes this safe is *`origin/main == main` after every tick* — the push at
`watch.py:205` plus C2's read-back. The drift base at `watch.py:243` should therefore be `main` for
the self workload; today that code path is skipped for scriptorium tasks anyway (`watch.py:225`).

### 7.4 If the checkout's ref is stale

- **quantum-insertion**, `master` behind `origin/master`: nothing in the system reads it after F2 and
  F6; the daily Desktop line says "local master is N behind origin/master" (§8) and stops there. The
  opt-in `ff_local_main` (C2) stays off for this workload.
- **quantum-insertion**, checkers still invoked by hand with the old default: the brief and
  `agents/steward.md:17` say `--base "$(git merge-base origin/master HEAD)"` (F6a); the scripts'
  defaults follow in the workload (F6b).
- **scriptorium**, `main` ahead of `origin/main`: push (C2); `main` behind `origin/main` or diverged:
  `branch_diverged` card — somebody bypassed the journal.

## 8. The Desktop checkout of `quantum-insertion`

**Rule.** The system may *read* the Desktop checkout (`git status`, `rev-list`, `worktree list`,
`ls-tree`) and may update its remote-tracking refs (`git fetch origin` — the worktrees share this
`.git`, and `spawn` already fetches there, `scr/ops.py:150`), prune worktree bookkeeping (Protocol
A2), and clear a zero-byte lock under the shared `.git` (Protocol B). It may act on **none** of the
checkout's own states: not the branch it sits on (`audit-controlled`), not its tracked edits, not its
22 untracked paths, not its `_to_delete/`, not the local `master` ref. No `checkout`, `pull`,
`merge`, `reset`, `stash`, `add`, `commit`, `clean`, no `fetch origin master:master`, no move of any
file there. This is Omri's working copy (T-0022 ruling), and it is not a system dependency once §7
holds — no service reads its `master` or runs its scripts.

**What the report looks like.** One informational line, regenerated daily, never a task, never a card:

- Source: the watcher's daily pass measures and journals `hygiene_desktop` with `data` =
  `{branch, ahead, behind, modified, untracked, untracked_tracked_on_origin: [...], to_delete: n}`.
- Rendering: `scr refresh` writes a section `## Desktop checkout (informational)` into
  `queues/omri.md` from the latest `hygiene_desktop` event (F5), **outside** `## Inbox`. Intake's
  source (1) reads only lines under `## Inbox` (`scr/launch.py:79`, `agents/intake.md:6`), and the
  regenerator already preserves the Inbox block verbatim (`scr/queue.py:51-56`), so the new section
  is invisible to Intake by construction.
- The line (today's values):

  > quantum-insertion checkout (2026-09-22): on `audit-controlled`; local `master` 0 ahead / 40 behind
  > `origin/master`; 5 modified tracked, 22 untracked paths (`missions/LOOPY/`, `loopy.toml`,
  > `_to_delete/` [12 stale lock/marker files], `missions/SCRIPTORIUM/`, 12 `missions/QI*/`, …).
  > Untracked paths that `origin/master` now tracks: `missions/SCRIPTORIUM/**` (identical),
  > `missions/QI24/PROMPT.md` (differs) — the next `pull`/`checkout` of `master` will refuse until
  > they are moved aside. Nothing here is a task; the system does not touch this checkout.

- The "next pull will refuse" clause is computed: untracked paths ∩ `git ls-tree -r --name-only
  origin/master`, with `git hash-object` vs the blob for "identical/differs". That is exactly the
  content of T-0101 (seq 1403) and T-0102 (seq 1407) — two Inbox lines a Steward wrote on 2026-09-19
  that Intake turned into `decide`/`approve` items, now in `omri_queue`. Under this plan they would
  have been this line. The two existing records are Omri's to answer or close; the plan changes what
  happens from now on.

**Rule for Stewards and Discuss sessions** (F5 edits `agents/steward.md`): a finding about the
Desktop checkout is journaled as `scr event system --name hygiene_desktop --note "…"`, never as
`scr inbox` and never as a `HELP:` line; the watcher's next daily pass folds it into the line.

## 9. Routing: what is auto-run, what is a chore, what is a card, what may reach Omri's queue

Four bins. The test for each finding is: *does a human have to do anything, and is the outcome
mechanically checkable?*

**Bin 1 — auto-run by the watcher** (journaled `system` event; no card; no record). Recurring
conditions with a safe, content-preserving repair:

- `git fetch origin` per repo per tick (C1)
- stale-lock scan and clear/move (B)
- checkpoint of every open task's worktree in both workloads (D1, D1′, D2), pushed per `push: true` (C5)
- `sweep(merge=False)` commit + push of dirty mission worktrees (D3); `--merge` too when `--auto-land` is on
- `[leftover]` commit at retirement (D4), then `retire` of finished worktrees after the age gate (A1), `worktree prune` (A2), orphan quarantine (A3)
- `ensure_main(push=True)` for the scriptorium checkout (C2, C3)
- the daily `hygiene_desktop` measurement (D5) and the digest (§10)

**Bin 2 — a chore in `ui/chores.yaml` with a `done` outcome check.** One-off items that need Omri's
hand or a policy toggle, whose outcome the check can see pass. Chores have no scheduler and a
permanent ack (`scr/chores.py:97-98`; `ui/chores.yaml:1-4`), so a *recurring* condition is never a
chore — it is bin 1. Hygiene chores (F7):

| chore | why a chore | `done` |
|---|---|---|
| `sweep-worktrees` (exists, text corrected) | merging unreviewed mission work into `master` while `--auto-land` is off is a policy act | `{no_dirty_worktrees: true}` (exists, `scr/chores.py:81-83`) |
| `checkpoint-push-quantum-insertion` | turning on checkpoint pushes for the workload is a Gate 4 config change | `{config_match: {file: workloads, regex: '(?ms)^quantum-insertion:.*?^\s+push:\s*true'}}` |
| `reclaim-quarantine` (manual) | disk in `<repo>-RETIRED/` and `<repo>-BACKUP-*/` is Omri's to reclaim (`docs/worktree-retirement.md:57-58`); raised only when the total passes a threshold | `{quarantine_below_gb: 20}` (new check kind) |
| `trusted-branch` (exists, untouched) | — | `{remote_branch: trusted}` |

Not a chore: anything about the Desktop checkout (§8; a chore is a nag under "Needs you", and the
ruling says it is not a system question), and any recurring hygiene condition.

**Bin 3 — an alarm card** (`ALARM_EVENTS`, `scr/ui.py:117`; one card per task and kind; dismissable;
not a task). Conditions a human must look at but that need no decision record:

- `checkpoint_skipped` (exists) — a task worktree on the wrong branch (C4)
- `branch_diverged` (new) — `<main>` and `origin/<main>` both ahead (C2)
- `hygiene_mismatch` (new) — a record says `merged` but `origin/<main>` does not contain the branch after a fetch (A1, exit 2)
- `fetch_failed` (new) — only after 24 h of continuous failure (C1)

`sweep`, `worktree_retired`, `worktree_pruned`, `worktree_quarantined`, `stale_lock_cleared`,
`leftover_committed`, `hygiene_desktop`, `hygiene_digest` are **not** alarms.

**Bin 4 — `omri_queue`.** For git hygiene as such: **the empty set.** No finding of Protocols A–D
becomes a `decide`, `approve` or `system` record, in either repo, in the Desktop checkout, or in
quarantine. The one adjacent way in is not a hygiene item but a *task* item: a `hygiene_mismatch`
card that a Steward, in Discuss, judges to need a direction decision about the task (e.g. its merge
was reverted on origin) — Gate 3, "stuck task", created by the Concierge as `kind: decide` about that
task. Expected frequency: rare.

**Intake rule** (F5 adds it to `agents/intake.md` "Triage rules"):

> A proposal or Inbox line whose only action is git hygiene — pruning or retiring worktrees, clearing
> locks, fast-forwarding or checking out a branch, committing or moving leftover files, anything in
> the quantum-insertion Desktop checkout — is not a task. Log `scr event system --name hygiene_routed
> --note "<line>"`, remove the line, and stop. If it names the Desktop checkout, the next
> `hygiene_desktop` line carries it. `verify_failed` stays an Omri-queue item: that is the records,
> not git.

**Steward rule** (F2/F5 edit `agents/steward.md:31-34` and `docs/worktree-retirement.md:103-107`):
exit 3 → the watcher commits the leftover and retires; exit 2 after a fetch → `hygiene_mismatch`;
neither is `escalated → omri_queue`.

## 10. Events and the daily digest

`system` event names are free strings (`scr/events.py:19`, `:52-54`); the ones this plan uses:

| name | new? | emitted by | alarm? | protocol |
|---|---|---|---|---|
| `checkpoint_commit` | exists (`scr/ops.py:190`) | watcher | no | D1, D1′, D2 |
| `checkpoint_skipped` | exists (`ops.py:184`) | watcher | yes | C4 |
| `sweep` | exists (`ops.py:337`) | watcher / `scr sweep` | no | D3 |
| `worktree_retired` | exists (`ops.py:380`) | watcher / `scr retire` | no | A1 |
| `worktree_pruned` | new | watcher | no | A2 |
| `worktree_quarantined` | new | watcher | no | A3 |
| `hygiene_refused` | new | watcher | no | A1 (code 1/3 paths that still refuse) |
| `hygiene_mismatch` | new | watcher | **yes** | A1 exit 2 after fetch |
| `stale_lock_cleared` | new | any `Git.run` retry, watcher scan | no | B |
| `branch_diverged` | new | watcher | **yes** | C2 |
| `fetch_failed` | new (counted; event only at 24 h) | watcher | yes at 24 h | C1 |
| `leftover_committed` | new | watcher | no | D4 |
| `hygiene_desktop` | new | watcher daily | no | D5 / §8 |
| `hygiene_digest` | new | watcher daily | no | all |
| `hygiene_routed` | new | Intake | no | §9 |

**Digest.** The daily pass writes `reports/hygiene/<date>.md` (a new folder next to `reports/daily/`
of `agents/observer.md:5`) and journals one `hygiene_digest` event with the path. Sections: per repo —
checkout branch and (ahead, behind); worktrees (live / finished-retired / phantom-pruned / orphan-
quarantined / locked); locks cleared or moved; task branches with unpushed commits and the oldest
base (C5, C6); leftovers committed (D4, with shas); the Desktop line (§8); quarantine inventory (D7,
weekly); unmerged remote branches (D8, weekly); counts of `journal_failed` / `push_failed` /
`fetch_failed`. The Observer's weekly report (`agents/observer.md:18-19`) takes trends from it (F9);
nothing in the digest is an Omri-queue proposal.

**Frequencies, summarised**

| every tick (30 min) | daily (`hygiene:<date>`) | weekly (Observer) |
|---|---|---|
| fetch (C1); lock scan (B); checkpoint of open task worktrees (D1, D1′, D2); C2/C5 measurements; scriptorium push (C2) | worktree pass A1–A4 with retire/prune/quarantine; `sweep` of mission worktrees (D3); `ensure_main` (C3); Desktop measurement (D5); digest | quarantine inventory (D7); unmerged branches (D8); trends |

## 11. Follow-up `refactor` tasks (one Toolsmith session each)

Order: F3 → F2 → F1 → F4 → F5 → F6 → F7 → F8 → F9. F2 is `blocked_by` T-0078 (scriptorium landing
path); F4 `informs` T-0079; F6b `blocked_by` T-0080 (same file). Tests run as
`.venv/Scripts/python.exe -m pytest tests -q` in the task worktree (`config/workloads.yaml:23`);
every task also keeps `scr verify` at 0 errors and touches only its scope files.

**F1 — `scr sweep` becomes the hygiene audit pass.**
Scope: `scr/ops.py`, `scr/cli.py`, `scr/watch.py`, `tests/test_hygiene.py` (new).
done_when:
- `scr sweep --dry-run --all` enumerates every workload in `config/workloads.yaml` (today: the first only, `ops.py:301`; no `--workload` flag, `cli.py:609-610`) and returns typed rows `{workload, worktree, branch, kind ∈ {dirty-mission, finished, phantom, orphan, locked, live}, …}`; `--json` writes them.
- `T-*` directories appear in the rows (today skipped, `ops.py:305`) but are never committed by `sweep` — a test plants a dirty task worktree and asserts no `[sweep]` commit on it.
- `no_dirty_worktrees` (`scr/chores.py:81-83`) keeps its meaning: it is true iff no `dirty-mission` row exists.
- A watcher tick in a fixture with two workloads runs the daily pass once per date (flag `hygiene:<date>`), writes `reports/hygiene/<date>.md`, and journals exactly one `hygiene_digest` event; a second tick the same day writes nothing.
- `--dry-run` never commits, pushes, moves or prunes (a test asserts the fixture's git state is byte-identical before and after).

**F2 — `retire` judges against `origin/<main>` after a fetch; leftovers are committed, not escalated; phantoms pruned; orphans quarantined.**
Scope: `scr/ops.py`, `scr/watch.py`, `tests/test_journal_ops.py`, `docs/worktree-retirement.md` (lines 103-107), `agents/steward.md` (lines 31-34). `blocked_by`: T-0078.
done_when:
- Fixture: bare origin with the branch merged into `origin/<main>` while the local `<main>` lags → `retire … --execute` succeeds (today exit 2 via `ops.py:358`); a branch on neither → still exit 2.
- Fixture: merged task worktree with one dirty tracked file → after the daily pass, `task/<id>` on origin has a `[leftover] <id>` commit containing the file, a `leftover_committed` event names the sha, and the worktree is in `<repo>-RETIRED/`; no task changes status.
- Fixture: a `git worktree list` entry whose directory was moved away → `worktree_pruned` event, the entry is gone, nothing else is touched; a `locked` entry is listed in the digest and untouched.
- Fixture: a plain directory under the worktree root that is not a worktree → moved to `<repo>-RETIRED/<stamp>-orphan-<name>/`, file count read back > 0, `worktree_quarantined` event.
- Age gate: a merged task whose worktree has an mtime under 24 h or a `discuss`/`launch`/`review_started` event under 24 h is skipped by the daily pass (the immediate post-land retire at `watch.py:353` is unchanged).
- `hygiene_mismatch` is journaled once per task (flag) when the record is `merged` and `origin/<main>` lacks the branch after a fetch; suppressed for a workload whose `repo` resolves to the scriptorium root while T-0078 is not merged.
- `grep -n "omri_queue" docs/worktree-retirement.md agents/steward.md` no longer matches the exit-3 lines; `test_retire_refuses_unmerged` still passes.

**F3 — fetch, proactive lock scan, branch-drift measurements, scriptorium push in the tick.**
Scope: `scr/gitops.py`, `scr/watch.py`, `scr/admin.py`, `config/limits.yaml` (new keys `stale_lock_seconds`, `hygiene_fetch: true`), `tests/test_watch.py`, `tests/test_journal_ops.py`.
done_when:
- One `git fetch --quiet origin` per workload repo per tick before the running-task loop; fixture: origin advanced → after one tick `git rev-parse origin/<main>` in the repo equals the bare origin's head; fetch failure → a log line and a `fetch_failed` counter, no exception (the tick continues).
- Fixture: an empty `index.lock` aged 2 min under `.git/worktrees/<name>/` is removed by the tick and `stale_lock_cleared` names it; a fresh empty lock and a non-empty 2-min lock are left; a non-empty 15-min lock is moved to `<git-common-dir>/_to_delete/<stamp>-<relpath>` (never deleted); `objects/` is not scanned (`STALE_LOCK_S` now read from limits, default 60).
- Fixture (self workload): `main` 1 ahead of `origin/main` → the tick pushes and reads back `main == origin/main`; diverged → exactly one `branch_diverged` event (flag), no merge attempted; checkout on a side branch with a clean tree → `ensure_main` fast-forwards (extends `test_ensure_main_folds_side_branch_and_pushes`).
- A workload-level `push: true` makes the checkpoint push apply to that workload's task branches (`watch.py:230` reads it), `--push` still forces it for all; quantum-insertion stays `push`-less until the chore of F7 is run.
- Dashboard rows gain `ahead/behind` of `<main>` and `unpushed:<n>` / `base-behind:<n>` per task (C5, C6).

**F4 — checkpoint every open task's worktree, in every workload.**
Scope: `scr/watch.py`, `scr/ops.py`, `tests/test_watch.py`. `informs`: T-0079.
done_when:
- The `tooling` skip at `watch.py:225-226` is gone: fixture scriptorium task with a dirty worktree → one `[checkpoint]` commit on `task/<id>` after one tick, pushed when the workload has `push: true`.
- Tasks in `submitted`, `accepted`, `returned`, `escalated`, `omri_queue`, `blocked` with an existing worktree are checkpointed too (fixture: a `submitted` task with a dirty file → committed); `dispatched` without a worktree is still flagged `no-worktree` and nothing else.
- Existing `test_watch_tick_checkpoints_and_detects_submission` and `test_spawn_checkpoint_land_retire` pass unchanged.
- If T-0079 has landed: out-of-scope paths go to a `[checkpoint-drift]` commit on the same branch (never left uncommitted) and `land` refuses to merge a branch that has one; if it has not, this task's report says so and T-0079's spec is amended with the line above.

**F5 — the Desktop-checkout informational line; Intake and Steward routing rules.**
Scope: `scr/watch.py`, `scr/queue.py`, `scr/ui.py`, `ui/index.html`, `agents/intake.md`, `agents/steward.md`, `tests/test_watch.py`, `tests/test_scriptorium.py`.
done_when:
- The daily pass journals `hygiene_desktop` with `data` `{branch, ahead, behind, modified, untracked, untracked_tracked_on_origin, to_delete}` for every workload whose `repo` is not the scriptorium root, using only read commands (a test wraps `Git.run` and asserts no mutating verb — `checkout|pull|merge|reset|stash|add|commit|clean|worktree remove|fetch .*:` — is run against that repo, `fetch origin` excepted).
- `scr refresh` renders `## Desktop checkout (informational)` in `queues/omri.md` from the latest `hygiene_desktop` event, outside and after `## Inbox`; the `## Inbox` block is byte-identical before and after (`queue.py:51-56`); the section is absent when no event exists.
- No card, chore or task is created by the measurement (a test asserts `needs` gains nothing of type `system` and no `T-*` record appears).
- `agents/intake.md` contains the routing rule of §9 (grep `hygiene_routed`); `agents/steward.md` tells Stewards to journal Desktop findings as `hygiene_desktop`, not `scr inbox` (grep).
- UI: the line appears once, muted, in the header — not under "Needs you".

**F6a — Steward brief and `agents/steward.md` name the reference ref.**
Scope: `scr/watch.py` (lines 394-396), `agents/steward.md` (lines 17, 19, 33), `tests/test_watch.py`.
done_when:
- The review brief instructs: `git fetch origin` first; run the **worktree's** `missions/SCRIPTORIUM/scripts/*.py` from the worktree with `--base "$(git merge-base origin/<main> HEAD)"`; a test renders the brief for a fixture task and asserts the string `merge-base origin/` is present and the bare `against <main>` phrase is gone.
- `agents/steward.md` says the same at lines 17/19/33 ("main's tree" → "`origin/<main>` after `git fetch`").

**F6b — checker defaults follow (quantum-insertion workload; `kind: refactor`, `queue: scribers`, not a Toolsmith task).**
Scope: `missions/SCRIPTORIUM/scripts/check_done.py`, `missions/SCRIPTORIUM/scripts/statement_diff.py`, `missions/SCRIPTORIUM/tests/`, `missions/SCRIPTORIUM/README.md`. `blocked_by`: T-0080 (same file, scope check).
done_when:
- `--base` defaults to the merge base of `origin/master` and `HEAD` (computed at runtime), with `--fetch` to refresh first; explicit `--base` still wins.
- Test: fixture repo where local `master` lags `origin/master` by one commit that touches `QuantumInsertion/Other.lean`; the task branch changes only `QuantumInsertion/Mine.lean` → `scope PASS` and `statement_diff` exit 0 (today both report `Other.lean`).
- `--json` shape unchanged; exit-code contract of `config/workloads.yaml:6-7` unchanged.

**F7 — hygiene chores and check kinds.**
Scope: `ui/chores.yaml`, `scr/chores.py`, `tests/test_scriptorium.py`.
done_when:
- New check kind `quarantine_below_gb`: true iff the summed size of `<repo>-RETIRED/` and `<repo>-BACKUP-*/` for every workload is below the argument; documented in the `scr/chores.py` docstring; `scr chore check reclaim-quarantine` runs in a fixture.
- Chores `checkpoint-push-quantum-insertion` (steps: `config set workloads …` or documented manual edit; `done: config_match` on the quantum-insertion block) and `reclaim-quarantine` (manual) exist; `sweep-worktrees`'s `why` no longer claims locks are cleared automatically unless F3 is merged, in which case it cites `stale_lock_cleared`.
- No chore is added for the Desktop checkout or for any recurring condition (a test loads `ui/chores.yaml` and asserts no `id` matches `desktop|lock|fetch|checkpoint-(?!push)`).

**F8 — UI alarm kinds and the digest link.**
Scope: `scr/ui.py`, `ui/index.html`, `tests/test_scriptorium.py`.
done_when:
- `ALARM_EVENTS` gains `branch_diverged`, `hygiene_mismatch`, `fetch_failed`; `sweep`, `worktree_retired`, `worktree_pruned`, `worktree_quarantined`, `stale_lock_cleared`, `leftover_committed`, `hygiene_desktop`, `hygiene_digest` produce no card (test: 20 such events → 0 cards; one `hygiene_mismatch` → exactly one card keyed `events:<task>:hygiene_mismatch:<ts>`).
- The header links the latest `reports/hygiene/<date>.md`.

**F9 — docs: the Observer reads the digest (`kind: docs`).**
Scope: `agents/observer.md`, `README.md` (the "Running a batch"/"Unattended" paragraphs, lines ~76-82 and ~120-121), `docs/worktree-retirement.md` (§"Integration with the Steward" cross-reference to this file).
done_when: `grep -c hygiene agents/observer.md README.md` ≥ 1 each; the weekly report section lists quarantine bytes, locks cleared, leftovers, unmerged branches as trend lines; no Omri-queue proposal is derived from them.

## 12. Dependencies, open questions, and what this plan does not decide

- **T-0078** (scriptorium landing path, `omri_queue`) gates F2's mismatch card and the retirement of
  scriptorium worktrees; **T-0079** (scoped checkpoint) and F4 must agree on the `[checkpoint-drift]`
  design; **T-0080** (whole-tree scope check) shares files with F6b; **T-0093** (trusted-branch policy,
  open) — nothing here assumes a policy for `trusted`: `trusted_branch` stays data the code never
  reads (`scr/ops.py:42`, `scr/ui.py:230`), and no protocol pushes to, merges into or bases on it
  (`agents/_common.md:10`).
- **Discuss in a retired worktree.** `scr/discuss.py:314` opens Steward sessions in the task's
  worktree; after A1 retires it the folder is under `<repo>-RETIRED/`. `discuss` should fall back to
  the repo root when the worktree is gone. That is a one-line change in `scr/discuss.py`, outside F2's
  scope; F2's report proposes it as an `unlocks` follow-up rather than widening its own scope.
- **`.lake` junctions and `.venv` junctions travel with a move** (`shutil.move` renames the directory;
  the target is untouched — `docs/worktree-retirement.md:95-96`, `tasks/T-0061-*/report.md`).
  Retirement never recurses into them. This is why quarantine, not delete, stays the primitive.
- **`~/.elan` toolchains** (`docs/worktree-retirement.md:161-167`) remain outside every workload and
  outside this plan: an Observer line, never a Steward action.
- **Not decided here:** whether the two open Desktop-checkout items (T-0101, T-0102) are closed in
  favour of the informational line — Omri's call; whether `push: true` is turned on for
  quantum-insertion — the chore of F7 asks; whether `ff_local_main` is ever enabled for a workload —
  off by default, and off for quantum-insertion under the ruling.
