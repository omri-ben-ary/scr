# Intake

Fills both queues. Runs at 22:00 nightly, after any task reaches `merged`/`rejected`, and whenever `queues/omri.md` has lines under `## Inbox`.

## Sources, in order
1. `## Inbox` in `queues/omri.md` — one task per line; Omri's words become the goal; remove the line once the record exists.
2. `unlocks` blocks at the end of every `report.md` of tasks that reached `merged` or `rejected` since the last run (find them with `scr replay`).
3. `system` events named `blocker_rejected`, `intake_starved`, `verify_failed` — each becomes an Omri-queue item.
4. The workload itself: `sorry`s, failing builds, TODO markers, and the audit's deviation list. Only when the ready queue is below `wip_ready_scribers`.

## Triage rules
- Dedup by scope: if an open task names the same declaration, do not create a new one; add an `informs` edge from the proposal's source instead and log a `system` event `duplicate`.
- Respect `max_successors_per_task` and `max_depth`. A proposal past `max_depth` becomes `kind: decide` in Omri's queue.
- Write `done_when.mechanical` for every scriber task. If you cannot write a mechanical line, set `verifiability` ≤ 0.5 and say why in spec.md.
- Set `may_touch_statements: true` whenever the goal could plausibly require changing a signature. It costs a slower dispatch; it saves a wasted night.
- Every `blocks` edge you add must share a declaration with its blocker or carry `--reason`.
- Origin is the task whose report proposed it, or `omri` / `intake` / `observer`.
- After creating: `scr task set <id> triaged`, then `ready` if no open blockers, else `blocked`.
