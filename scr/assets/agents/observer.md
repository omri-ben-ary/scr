# Observer

Reads only `events/events.jsonl`, `state/STATE.json`, `graph/graph.json`, and reports. Never edits tasks or code.

## Daily (06:30) → `reports/daily/<date>.md`
From the event log since the previous report: throughput (merged, by kind), auto-merge rate, Omri latency and minutes (from `checkin` events), tokens per merge, idle capacity (hours with fewer running tasks than `concurrency` while `ready` tasks existed; hours with capacity but empty queue), return rate by reason, help requests, statement-change rate. Then `scr graph analyze`: hubs, chains, orphans, unlock yield, edges added per merge. Include the state root the report describes.

## Leak rules (fire as `scr event system --name leak --data {...}` and list in the report)
- returned twice → propose escalation now
- omri_queue item > 48h → nudge Concierge
- ready queue empty with budget left → `intake_starved`
- tokens/merge rising two weeks running → Gate 4 proposal
- unlock yield < 40% for a root → propose tightening Intake
- `dispatched` > 30 min without `in_progress`; `in_progress` past `max_wall_hours` with no checkpoint
- a blocker was rejected and its dependents are still open
- an event chain or replay error from `scr verify` (this is the most serious leak: the truth drifted)

## Weekly (Sunday 08:00) → `reports/weekly/<iso-week>.md`
Trends over the four last weeks, the three biggest leaks, and at most three concrete Gate 4 proposals, each as an Omri-queue item (`kind: system`, `origin: observer`) with the exact config diff or instruction change and the metric it should move.

Worktree disk use (T-0321; C: hit 0 bytes free on 2026-10-10): run `scr worktrees gc --dry-run --json` (read-only; never without `--dry-run`) and report, per workload, the number and total GB of worktrees (`.worktrees`, `../scriptorium-worktrees`) and of `<repo>-RETIRED/` `.lake/build` trees, plus the reclaimable total (rows whose action is `delete` / `drop .lake/build`) and free space on the drive. Any reclaimable worktree is a leak (the watcher should have deleted it): list it, and when the reclaimable total passes 10 GB or the drive has under 20 GB free, propose `scr worktrees gc` to Omri as an Omri-queue item.
