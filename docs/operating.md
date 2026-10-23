# Operating Scriptorium

The code lives in this repo; the state (tasks, events, queues, config, reports) lives in the scriptorium folder and
journals itself. The PC runs a tagged release of this repo, never its source.

## Layout on the PC

```
Desktop\scriptorium\            the state root (git, journal mode)
Desktop\scr-runtime\            the releases, side by side ($SCR_RUNTIME overrides the location)
    v0.2.0\  v0.2.1\  ...       one non-editable venv per release
    current\                    junction to the live one: current\Scripts\scr.exe is what everything runs
    history.jsonl               every switch (upgrade, rollback, auto-rollback)
```

The logon tasks, the desktop shortcut, the hooks and every session the system launches resolve
`current\Scripts\scr.exe`. Before the first upgrade there is no runtime folder and they use the state root's `.venv`.

## See what is live

```
scr version          # running release, code and state schema versions, releases installed side by side
scr issue list       # known issues waiting for the next release
```

## Make a tooling change

1. File it like any task, in the tooling workload (Inbox, a Discuss session, or `scr task new ... --workload scr`).
2. A scriber or the cloud pool works on `task/T-xxxx` in this repo and opens a PR.
3. CI runs; the Steward reviews; the PR is merged into `main`. Nothing on the PC changes yet.

An accepted tooling task is never merged into the state folder: the watcher raises one `land_tooling` card, and once
the PR is merged the record is closed with `scr --actor omri task set <id> merged --note <PR url>`.

## Release and upgrade

```
scr release --minor          # in a clean checkout of main: tag HEAD as the next version, notes from the merged PRs
git push origin v0.3.0       # (printed by scr release; the version comes from the tag, nothing is committed)
scr upgrade v0.3.0           # install side by side, smoke, switch, smoke; rolls back on failure
scr upgrade --latest
scr rollback                 # back to the release that was live before the last switch
```

`scr upgrade` steps, each logged as an event:

1. install the release into `<runtime>\<tag>\` (`pip install git+https://github.com/omri-ben-ary/scr@<tag>`);
2. smoke it read-only against the live state: `verify`, and `watch --dry-run` (one tick on a throwaway copy);
3. stop the watcher and the UI;
4. run the state migrations with the new code (`scr migrate`; an incompatible one is refused while sessions run);
5. point `current` at the new release;
6. start the watcher and the UI;
7. smoke again. Any failure after step 3 points `current` back, restarts, and raises one `upgrade_failed` card.

Running scriber sessions are not interrupted: each release reads what the previous one wrote.

## Report a bug without fixing it live

```
scr issue add "landing job queued with nothing running"
scr issue close I-0003 --note "fixed in v0.3.1"
```

`queues/known-issues.md` is the list, rewritten from the `issue_add` / `issue_close` events.

## Fast track for a real outage

1. `scr issue add "<what broke>" --urgent` files a ready tooling task at the top of the queue.
2. The fix goes through a PR and CI as usual.
3. `scr release --patch`, push the tag, then `scr upgrade --latest`.

## State schema

`state/STATE.json` carries `schema_version`. `scr migrate --dry-run` lists the migrations a release would apply;
`scr upgrade` runs them. Releases expand before they contract, so a session started on the old release stays valid.

## The first switch: rehearsal and cut-over

The PC's first release replaces the editable checkout. Two scripts in `tools/` do it, run in Windows PowerShell
from a clone of this repo (or a download of the two files):

```
powershell -NoProfile -ExecutionPolicy Bypass -File tools\rehearse.ps1 -Tag v0.2.0
powershell -NoProfile -ExecutionPolicy Bypass -File tools\cutover.ps1  -Tag v0.2.0
```

Both default to `-State %USERPROFILE%\Desktop\scriptorium` and `-Runtime <state>\..\scr-runtime`.

- **`rehearse.ps1`** (gate G2) installs the release into `<runtime>\<tag>\` and runs it against a copy of the state in
  `%TEMP%`: `migrate --dry-run`, `migrate`, `verify`, `watch --dry-run`, the UI on port 8766, a print-only Discuss and
  `version`. It writes a report into the copy and prints its path. It never writes to the live state, never creates
  `current`, and leaves the live `.venv`, the logon tasks and the shortcut alone. Run it as often as needed.
- **`cutover.ps1`** (the only downtime, about 10 minutes) checks that the live state verifies and no landing job runs,
  tags the state repo `pre-cutover`, installs the release, and has the release run `scr upgrade <tag>` (the old
  checkout predates `scr upgrade`). On success it re-registers the logon tasks and the desktop shortcut on
  `<runtime>\current` and smokes the result: `version`, `verify`, one dry tick, the UI. If anything after the switch
  fails it removes `current`, puts the services, logon tasks and shortcut back on the old `.venv`, and checks the old
  install. It prints PASS or FAIL and leaves a log in `reports\`.

Running sessions are not interrupted; they keep the old `.venv` until the Phase 4 drain.

## Known limits

- `scr upgrade`'s rollback does not revert a migration commit when the post-switch smoke fails after `migrate` ran.
  This is harmless while releases follow expand-then-contract (the previous release reads what the new one wrote);
  revisit it before the first real migration.
- `cutover.ps1` sets `SCR_RUNTIME` only for its own run. The logon tasks it registers resolve the default runtime
  folder (`<state>\..\scr-runtime`), so a non-default `-Runtime` also needs `SCR_RUNTIME` set for the user.

## What not to do any more

- Edit files under the runtime folder, or edit code inside the state folder.
- Write task records by hand; use `scr` so the change is journaled.
- Kill a landing to land it by hand; landings are PR merges, and a stuck one is a known issue.
