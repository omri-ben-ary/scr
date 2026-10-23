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

## What not to do any more

- Edit files under the runtime folder, or edit code inside the state folder.
- Write task records by hand; use `scr` so the change is journaled.
- Kill a landing to land it by hand; landings are PR merges, and a stuck one is a known issue.
