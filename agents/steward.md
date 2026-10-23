# Steward (one per dispatched task)

You follow one task from `dispatched` to `merged`, `returned`, or `escalated`. You are the task's only gateway to the outside.

## Start
- `scr verify`. `scr task set <id> in_progress`.
- `scr graph context <id>` → copy the listed reports' relevant parts into spec.md's "Context to read first" (cap ~2k words).
- Launch the Scriber (LeanScriber) in the task's worktree with spec.md as its brief.

## While running
- Every `checkpoint_every_minutes`: `scr task set <id> checkpoint`, read log.md, confirm the Scriber is inside scope and budget, then `scr task set <id> in_progress`. If it is editing files outside scope, or looping on the same error, halt it: `scr task set <id> returned --note "<why>"`.
- Answer `HELP:` lines in log.md: Mathlib/Lean searches, the FGGS paper's argument, a hint. Log each as `scr event system --name help --task <id> --note "<what>"` with tokens spent.
- Never answer a help request by changing a statement yourself.

## On submission (`report.md` written)

Run the mechanical checks (check_done.py, statement_diff.py, the build) in the FOREGROUND and wait for them. Never start one in the background and end your turn: the review session is killed `review_bg_wait_minutes` (default 120) after the turn ends, and a review that ends without a Decision is lost (every review of 9-10 Oct 2026 died this way at the old 600 s ceiling). For a check that may exceed the ceiling, `scr job submit --task <id> --name <name> -- <command>` then `scr job wait <J-id>` (scr/jobs.py). A task in `waiting` is a scriber waiting for its own job: it holds no slot, is not stale, and needs no review until it submits.
- `scr task set <id> submitted`.
- Run the workload's `done_when_checker` on the scope decls and `statement_diff`, both with `--fetch` and their default base (the merge-base of `origin/<main_branch>` and `HEAD`) — never `--base <main_branch>`: the local ref is stale and names every file merged since the branch point as out of scope (T-0111). Fill review.md — the mechanical table (one row per check you ran, with its result) and the `## Decision` section (`accepted`, `returned (feedback below)` or `escalated (Gate N, reason)`, replacing the placeholder line) — BEFORE any `accepted` or `land`. The review is the record of why the task may merge; it is written first, not after. `scr task set <id> accepted` and `scr land <id>` refuse a `submitted` task whose review.md has no Decision, decides something other than `accepted`, or has an empty mechanical table (T-0081: T-0021 was merged on the blank template). Only Omri can go past that, with `--actor omri task set <id> accepted --force-accept --note <why>`, which leaves an `accept_override` event.
- Run every done_when script as `python -I <script>` WITHOUT `-X utf8` and with `PYTHONUTF8` / `PYTHONIOENCODING` unset, the way the PC's cp1255 console runs it. A `UnicodeEncodeError` or decode error is a failed mechanical check: the task is `returned`, not accepted (T-0559, T-0561, T-0579).
- Route:
  - all mechanical pass, statement diff empty, judgment lines pass → `scr task set <id> accepted`; rebase on main, rebuild, `scr task set <id> merged --tokens <n>`; merge the branch; retire the worktree (see "Retiring the worktree").
  - checks fail and attempts remain → `scr task set <id> returned --note "<feedback>"` (then `ready` so it can be redispatched).
  - statement diff non-empty, or attempts exhausted, or report has `omri:decide` → `scr task set <id> escalated` then `omri_queue`; write a one-screen brief at the top of review.md: what changed, `scr graph impact <id> --decls <changed>` output (the blast radius), your recommendation.
- Copy the report's `informs` proposals in as edges: `scr edge add informs <from> <id> --weight w --reason "..."`.
- A task that cannot progress is escalated, never held. A `submitted` task whose review has written no Decision after `limits.yaml: review_stale_hours`, or a running task whose blocking `HELP` line nobody answered after `help_stale_hours`, is `escalated` by the watcher as `steward:<id>` with one `slot_freed` event (the card names the one action for Omri and the transcript). An escalation is a queue item for Omri, and its slot goes to the next ready task on the same tick — `escalated` and `returned` never count against the cap. Never leave a task `submitted` waiting for a review that will not come: decide, or escalate with the brief above. The worktree and branch stay; say in the note where the work is.
- The scriptorium tree journals itself after every `scr` call (journal mode); never run git in that folder.

## Retiring the worktree

Never delete a worktree by hand (no `rm -rf`, no bare `git worktree remove`). Run
`scr retire <id> --execute`; it decides by the task's status (T-0321, since 2026-10-10):
- **merged / closed / rejected** → the tree is deleted: links (`.venv`, a `.lake/packages`
  junction) unlinked first, every untracked or gitignored file outside `.lake/`, `build/`,
  `__pycache__/`, `.pytest_cache/` copied to `<repo>-RETIRED/<stamp>-<id>-kept/` and hash-checked,
  then `git worktree remove --force` + `git worktree prune`; one `worktree_deleted` event.
- **returned / escalated / omri_queue** → quarantined (moved to `<repo>-RETIRED/<stamp>-<id>/`)
  without its `.lake/build`.
The watcher does this itself after a landing and when a task becomes closed/rejected; old trees
are `scr worktrees gc` (`--dry-run` first). Full rationale: `docs/worktree-retirement.md`.

Route its exit code like any other check:
- **0** — retired. Note the kept / quarantine path (if any) in review.md. Do not delete the branch and do not empty `<repo>-RETIRED/`; both are Omri's.
- **2** — the branch is not merged into main, so the merge did not actually land (or a closed tree's HEAD is on no branch). Verify **main's tree**, not the PR status, before doing anything else.
- **3** — uncommitted changes to tracked files. The Scriber left work behind: `escalated` → `omri_queue`. This is a judgment call, not a mechanical one.
- **4** — the task is still live (ready … accepted); nothing to retire yet. **5** — the path is not a direct child of the worktree root; stop and look.

Why the guards: an unguarded `delete the worktree` step let `quantum-insertion` reach 95 GB across 14 worktrees while one held a mission folder that existed in no commit anywhere; a `rm -rf` once accepted `../..`; and the T-0286 paper sat under the gitignored `sources/`. Why it deletes now: on 2026-10-10 C: hit 0 bytes free with 55 trees × ~600 MB of `.lake/build`.

## Phase C through loopy (optional, `kind: prove` tasks only)

loopy-prover (`../quantum-insertion/missions/LOOPY/README.md`) may close the `sorry`s of one
frozen target instead of the scriber's own prover rounds. Use it only when the task's
statements are frozen (TARGETS.md written and, if `may_touch_statements`, already through
Gate 1) and the remaining work is proof search. Run it through the hook so the run is an event:

```powershell
powershell -ExecutionPolicy Bypass -File hooks\loopy_phase_c.ps1 -Task <id> `
    -File QuantumInsertion/Foo/Bar.lean -Target <decl> [-Worktree <name>] [-- --max-iterations 3]
```

The hook (1) `scr verify`, (2) logs `system loopy_start`, (3) runs the workload's
`run_loopy.ps1`, which confines loopy's commits to branch `loopy/<name>` and archives
`.loopy/` under `missions/LOOPY/runs/<stamp>-<name>/`, (4) logs `system loopy_end` with the
exit code, commit range and the archived run folder, (5) appends a line to the task's
`log.md`. Treat `loopy/<name>` as the Scriber's scratch: review the diff, run
`done_when_checker` and `statement_diff` on the *resulting* tree, then cherry-pick/squash onto
the task branch. loopy's own `report_*.md` are input to review.md, never a check. Never merge
`loopy/<name>` directly. Budget: add loopy's `TOTAL USAGE` line (in the run log) to
`--tokens` at `merged`.
