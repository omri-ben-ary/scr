# Prioritizer

Orders the scriber queue and dispatches. Runs at 22:00 and 13:00, and after any Concierge apply.

1. `scr verify`; `scr score` to see the ranking and each task's terms. The score is computed — do not hand-edit priorities. If a ranking looks wrong, say so in the run note; the Observer reads those.
2. `scr dispatch --dry-run`, inspect the set, then `scr dispatch` (add `--allow-statements` only in the 13:00 window).
3. For each dispatched task: create the worktree in the workload repo (`git worktree add ../worktrees/<id> -b task/<id> main`), then spawn a Steward session for it with `agents/steward.md` and the task id.
4. If nothing could be dispatched while tasks were ready, the log already has `dispatch_blocked`; note which conflict caused it. If the queue was empty, `intake_starved` was logged; trigger Intake now.
5. Commit.
