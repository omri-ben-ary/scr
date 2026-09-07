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
- Commit with `[steward:<id>]`.

## Retiring the worktree

Never delete a worktree by hand. Run the workload's `retire_worktree` hook
(reference implementation: `hooks/retire_worktree.sh`), which quarantines the
tree instead of deleting it, after checking it is merged and backing up any
untracked files. Full rationale: `docs/worktree-retirement.md`.

```sh
<retire_worktree> --repo <workload.repo> --worktrees <workload.worktrees> \
    --main <workload.main_branch> --name <worktree> --execute
```

Route its exit code like any other check:
- **0** — retired. Note the quarantine path in review.md. Do not delete the branch and do not empty the quarantine; both are Omri's.
- **2** — the branch is not merged into main, so the merge did not actually land. Verify **main's tree**, not the PR status, before doing anything else.
- **3** — uncommitted changes to tracked files. The Scriber left work behind: `escalated` → `omri_queue`. This is a judgment call, not a mechanical one.

Why it never deletes: an unguarded `delete the worktree` step let `quantum-insertion` reach 95 GB across 14 worktrees, 7 of them merged and dead, while one worktree held a mission folder that existed in no commit anywhere. See the doc for the two bugs — a `rm -rf` that accepted `../..`, and a "tidy" `git worktree remove` that emptied the quarantine while its own verification reported OK.
