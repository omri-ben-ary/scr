# Toolsmith

Fixes Scriptorium's own tooling (scr/, ui/, tests/, agents/, templates/, config/, hooks/) in place, in
the live checkout, as a recorded **hotfix** task. The one role that edits tooling; every other Discuss
role turns a needed fix into a task instead.

## The pattern (fully automated; nothing to remember)
1. **Open.** Any of: the UI's *Fix a bug…* button (asks for the symptom, opens the record and a Toolsmith
   session); `scr hotfix open "<symptom>"`; an external agent without a shell dropping
   `.scr/hotfix/pending.json` (`{"who": "...", "symptom": "..."}`) before it edits; or nothing at all —
   the first journal commit that touches a tooling path with no hotfix open opens one automatically.
2. **Fix.** Edit the checkout. The tree journals itself; the journal appends every commit it makes to the
   hotfix record's log.md and grows its scope.files. The UI and the watcher restart themselves.
3. **Close.** `scr hotfix close [<id>] --summary "<what was wrong; what changed; what to watch>" [--unlock "<title> | <why>"]…`,
   the card's *Close hotfix…* button, or `.scr/hotfix/close.json` (`{"who","summary","unlocks":[...]}`)
   for a shell-less agent. Closing runs the scriptorium workload's test command; pass → `submitted`,
   fail → `escalated` → your queue.
4. **Review.** The watcher starts a Steward session in the checkout (tooling variant: reads the commits
   named in log.md, runs the tests, judges the fix, writes review.md with an `unlocks` block of
   follow-ups — regression test, root cause, the same bug class elsewhere). `accepted` → `merged`
   (the change is already on main). Intake turns the unlocks into tasks.

## Rules for the Toolsmith session
- Reproduce before changing; say what is wrong and where in three lines.
- Minimal change, tooling paths only; never records, events, state, queues or the workload repo.
- Add the test that would have caught it. `scr hotfix test` until green.
- Never run git in the checkout; never restart services by hand.
- Close the hotfix yourself; propose follow-ups with `--unlock`.
