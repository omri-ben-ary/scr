# Common rules for every Scriptorium agent

- The repo is the truth. Never keep state in your head between steps; read records, write records, append events. Use `scr` for every change so the event is logged and the snapshot refreshed. Never edit `graph/graph.json`, `state/STATE.json`, or `queues/*.md` by hand.
- Identify yourself: every `scr` call uses `--actor <role>` (e.g. `--actor steward:T-0042`).
- Start every run with `scr verify`. If it fails, do not proceed: log `scr event system --name verify_failed --note "<errors>"` and stop. A failed verify is an Omri-queue item, not something to repair silently.
- End every run with `scr verify` and a git commit whose message starts with `[<role>]` and ends with the state root printed by `scr refresh`.
- You may propose; only the Concierge applying Omri's answer, or Intake, may create tasks. Scribers and Stewards never run `scr task new`.
- Anything touching a theorem statement, definition, or axiom is Gate 1. Anything you are unsure belongs to you is Gate 2. When in doubt, escalate with a short brief — never guess and proceed.
- Budgets are hard. If a task's `max_tokens`, `max_attempts`, or `max_wall_hours` is exhausted, the next transition is `escalated`.
