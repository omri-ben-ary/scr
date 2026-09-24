# Common rules for every Scriptorium agent

- The repo is the truth. Never keep state in your head between steps; read records, write records, append events. Use `scr` for every change so the event is logged and the snapshot refreshed. Never edit `graph/graph.json`, `state/STATE.json`, or `queues/*.md` by hand.
- Identify yourself: every `scr` call uses `--actor <role>` (e.g. `--actor steward:T-0042`).
- Start every run with `scr verify`. If it fails, do not proceed: log `scr event system --name verify_failed --note "<errors>"` and stop. A failed verify is an Omri-queue item, not something to repair silently.
- End every run with `scr verify`. The scriptorium tree journals itself: every `scr` call commits with your `--actor` in the message, so never run `git` in the scriptorium folder.
- You may propose; only the Concierge applying Omri's answer, or Intake, may create tasks. Scribers and Stewards never run `scr task new`.
- Anything touching a theorem statement, definition, or axiom is Gate 1. Anything you are unsure belongs to you is Gate 2. When in doubt, escalate with a short brief — never guess and proceed.
- Budgets are hard. If a task's `max_tokens`, `max_attempts`, or `max_wall_hours` is exhausted, the next transition is `escalated`.
- Branch policy: the workload's `main_branch` belongs to the agents and is the complete history — every task branch is merged there, unreviewed content included, so it can be investigated in retrospect. The workload's `trusted_branch` is Omri's alone: never push to it, merge into it, or base work on it; only Omri promotes commits from `main_branch` to it.
- A task that cannot progress is escalated, never held. Stuck means: `submitted` with no Decision written after `review_stale_hours`, or blocked on a `HELP` line nobody answered after `help_stale_hours`, or out of budget. An escalation is a queue item for Omri (one card, one action), and its concurrency slot goes to the next ready task on the same tick: `escalated` and `returned` never count against the cap. Nothing is lost by escalating — the worktree, the branch and the transcript stay, and the note says where the work is.
