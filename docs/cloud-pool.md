# Cloud pool — runbook (21 Oct 2026)

Design: `docs/proposals/cloud-pool.md` (approved by Omri 20–21 Oct). This file is what runs.

## The loop

1. **Claim (PC, every watcher tick).** `scr cloud claim` (scr/cloud.py, called from `Watcher.tick`) hands the
   highest-ranked ready, cloud-eligible tasks to the cloud up to `limits.cloud_pools` (3): `ready -> blocked`
   with the note `cloud lane: claimed by steward at <ts>` and `lane: cloud` on the record. `scr dispatch` never
   picks a claimed task. Eligibility (`cloud.eligible`): a scribers task of workload quantum-insertion, not a
   hotfix, not `lane: pc`, no kernel-replay certificate paths (`QuantumInsertion/FourQuery/`,
   `numerics/certificates`, `numerics/qi3/`), no `needs: [lp|local|gpu|pc]`; Lean tasks only when
   `limits.cloud_lean` is 1 (the build cache below makes them cheap).
2. **Run (cloud, the scheduled task "Scriptorium cloud pool", every 2 h, bound to Omri's PC).** A fresh session
   clones both repos, reads the claims, locks each one it takes by pushing an empty `cloud-locks/<id>` branch
   (a rejected push means another pool has it), restores the Lean cache if needed, runs the scriber as a
   subagent with the task's spec.md as brief, then pushes `task/<id>` (terminal commit `scriber done: <id> — …`)
   and an orphan `cloud-reports/<id>` branch holding `report.md` and `log.md`.
3. **Land (PC, every tick).** `Watcher.cloud_landing_checks`: fetch `task/<id>`; when its tip says
   `scriber done:` and a finished report.md is in the task dir (copied from `cloud-reports/<id>` if the pool
   delivered it there), attach `.worktrees/<id>`, clear `lane`, walk blocked → ready → dispatched → in_progress
   → submitted. Review and landing are the normal path; a returned task's next attempt runs on the PC lane.

## Lean build cache (`tools/cloud_cache.py`)

Store: `C:\Users\User\Desktop\quantum-insertion\.lake\cloud-cache\master\` — `qi-build.tar.zst.part00..`,
`qo-build.tar.zst`, `MASTER_SHA`, `SHA256SUMS` (≈ 143 MB for the full master build). A cloud session restores it
with `device_stage_files` on those files + `python tools/cloud_cache.py restore <repo> <dir>` (seconds), after
`lake exe cache get` (≈ 3 min) — instead of the ≈ 1 h cold build. A pool that finishes a green full `lake build` on
a newer master repacks (`pack`) and ships the parts back with `device_commit_files` (4 parts per call).
`.olean` files are platform-independent, so the PC and the cloud share one pack.

## Hosts the cloud needs allowed (Settings → Capabilities)

`releases.lean-lang.org`, `release.lean-lang.org`, `objects.githubusercontent.com`,
`lakecache.blob.core.windows.net`. arXiv e-prints are blocked: scribers cite theorem numbers from PDFs.

## Knobs and checks

- `config/limits.yaml`: `cloud_pools` (0 disables), `cloud_lean`.
- `scr cloud status` — claims; `scr cloud claim --dry-run` — what the next tick would hand over and why not.
- `reports/steward_watch.log`: `cloud claim:` and `cloud branch … landed into review` lines.
- A task that must stay local: `lane: pc` in its task.yaml.

## Encoding rule for scripts

Every Python script a scriber writes calls `sys.stdout.reconfigure(encoding="utf-8")` and `sys.stderr.reconfigure(encoding="utf-8")` and opens every text file with `encoding="utf-8"`. The PC reviews done_when scripts with `python -I <script>` on a cp1255 console without `-X utf8`, so a script that prints a non-ASCII character fails there even when it passed in the cloud (T-0559, T-0561).

- PC scribers get the rule from `scr task brief` (`scr/brief.py`).
- The cloud pool does not read `scr/brief.py`: it briefs its scriber subagent from the task's `spec.md`, so the rule must be pasted into the scheduled task "Scriptorium cloud pool" by hand.
- The Steward runs each done_when script as `python -I` without `-X utf8`, `PYTHONUTF8` and `PYTHONIOENCODING` unset; an encoding error returns the task (`agents/steward.md`, "On submission").

## Known limits

- One scriber per container (2 vCPU / 7 GB); parallelism = pool sessions.
- No independent reviewer inside the pool: the Steward's review on the PC is the first adversarial pass.
- Attempt 2 of a returned cloud task runs on the PC (its worktree is attached by then).
