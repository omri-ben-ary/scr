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
- `scr task set <id> submitted`.
- Run the workload's `done_when_checker` on the scope decls and `statement_diff` against main. Fill review.md.
- Route:
  - all mechanical pass, statement diff empty, judgment lines pass → `scr task set <id> accepted`; rebase on main, rebuild, `scr task set <id> merged --tokens <n>`; merge the branch; retire the worktree (see "Retiring the worktree").
  - checks fail and attempts remain → `scr task set <id> returned --note "<feedback>"` (then `ready` so it can be redispatched).
  - statement diff non-empty, or attempts exhausted, or report has `omri:decide` → `scr task set <id> escalated` then `omri_queue`; write a one-screen brief at the top of review.md: what changed, `scr graph impact <id> --decls <changed>` output (the blast radius), your recommendation.
- Copy the report's `informs` proposals in as edges: `scr edge add informs <from> <id> --weight w --reason "..."`.
- A task that cannot progress is escalated, never held. A `submitted` task whose review has written no Decision after `limits.yaml: review_stale_hours`, or a running task whose blocking `HELP` line nobody answered after `help_stale_hours`, is `escalated` by the watcher as `steward:<id>` with one `slot_freed` event (the card names the one action for Omri and the transcript). An escalation is a queue item for Omri, and its slot goes to the next ready task on the same tick — `escalated` and `returned` never count against the cap. Never leave a task `submitted` waiting for a review that will not come: decide, or escalate with the brief above. The worktree and branch stay; say in the note where the work is.
- The scriptorium tree journals itself after every `scr` call (journal mode); never run git in that folder.

## Retiring the worktree

Never delete a worktree by hand. Run `scr retire <worktree> --execute`, which
quarantines the tree instead of deleting it (a move into `<repo>-RETIRED/`), after
checking it is merged. Full rationale: `docs/worktree-retirement.md`.

Route its exit code like any other check:
- **0** — retired. Note the quarantine path in review.md. Do not delete the branch and do not empty the quarantine; both are Omri's.
- **2** — the branch is not merged into main, so the merge did not actually land. Verify **main's tree**, not the PR status, before doing anything else.
- **3** — uncommitted changes to tracked files. The Scriber left work behind: `escalated` → `omri_queue`. This is a judgment call, not a mechanical one.

Why it never deletes: an unguarded `delete the worktree` step let `quantum-insertion` reach 95 GB across 14 worktrees, 7 of them merged and dead, while one worktree held a mission folder that existed in no commit anywhere. See the doc for the two bugs — a `rm -rf` that accepted `../..`, and a "tidy" `git worktree remove` that emptied the quarantine while its own verification reported OK.

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
