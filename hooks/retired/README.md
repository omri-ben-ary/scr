# Retired hooks

These PowerShell/bash scripts were replaced on 2026-09-16 by `scr` subcommands, which run and are
tested on any OS and journal themselves:

| old                        | new                                   |
|----------------------------|---------------------------------------|
| steward_watch.ps1          | `scr watch [--once] [--unattended]`   |
| land_task.ps1              | `scr land <id> [--merge]`             |
| launch_dispatched.ps1      | `scr launch [--mode tabs]`            |
| spawn_worktrees.ps1        | `scr spawn [--tasks ...]`             |
| sweep_worktrees.ps1        | `scr sweep [--merge] [--dry-run]`     |
| intake_run.ps1             | `scr intake`                          |
| retire_worktree.sh         | `scr retire <name> [--execute]`       |

Kept for reference; nothing calls them.
