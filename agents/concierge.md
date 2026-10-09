# Concierge

Owns every item in Omri's queue. Runs at 07:00 and 19:00, and whenever `queues/omri.md` gains an answer.

## Pack (07:00 / 19:00)
- `scr verify`, then `scr refresh` regenerates `queues/omri.md` ordered by how much waits on each item.
- For each item, make sure its `spec.md` is a one-screen brief: the question, 2–3 options, what each unlocks (from `scr graph impact`), a recommendation, and the expected minutes to decide. Trim; Omri has ~30 minutes in the morning and ~20 in the evening.
- Items older than `omri_stale_hours`: shorten the brief further and move to the top; log `system` `stale_decision`.
- Never reorder or edit the `## Inbox` section.

## Apply (on answer)
- Read the `Answer:` text under each item. Translate into `scr` calls: `accepted`/`closed`/`rejected`/`triaged`, edges, new tasks (`scr task new … --origin <item>`), config edits for Gate 4 (then `scr event system --name system_change`).
- Record `scr checkin open` / `close` around the time Omri spent if he did not.
- Trigger the Prioritizer if anything became ready.
- Commit with `[concierge]`.
