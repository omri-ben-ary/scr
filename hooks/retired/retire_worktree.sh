#!/usr/bin/env bash
# retire_worktree.sh — reference implementation of the Steward's worktree
# retirement step. Workload-agnostic: every repo-specific fact is an argument.
#
# CORE INVARIANT: THIS SCRIPT NEVER DELETES ANYTHING.
# It quarantines by `mv` into a retirement folder outside the repo. Reclaiming
# disk is a separate, deliberate act by Omri. Every action here is reversible
# with one `mv` back.
#
# Why not `rm -rf`: see docs/worktree-retirement.md. Two bugs found while
# building this, both in versions that looked correct:
#   1. `rm -rf "$WT_ROOT/$NAME"` with `-n` and `-d` guards still accepted
#      `../..`, which resolved to the Desktop.
#   2. Calling `git worktree remove --force` as a "tidy path" before the `mv`
#      DELETED the tree and left an empty quarantine folder — the script's own
#      invariant violated on its first run, while its verification block
#      reported three OKs.
# Quarantine removes the whole class rather than out-guarding it, and is immune
# to the junction hazard (`mv` renames a directory; it never recurses into it).
#
# Does NOT merge, commit, or delete branches. Per the Steward's routing, the
# irreversible half stays with Omri (Gate 1).
#
# Usage:
#   hooks/retire_worktree.sh --repo <path> --worktrees <path> \
#       --main <branch> --name <worktree-name> [--execute]
#
# Exit codes:
#   0  retired (or dry run OK)      2  branch not merged into <main>
#   1  usage / safety refusal       3  uncommitted tracked changes present

set -uo pipefail

REPO="" WT_ROOT="" MAIN="" NAME="" MODE="--dry-run"
while [ $# -gt 0 ]; do
  case "$1" in
    --repo)      REPO="${2:-}"; shift 2 ;;
    --worktrees) WT_ROOT="${2:-}"; shift 2 ;;
    --main)      MAIN="${2:-}"; shift 2 ;;
    --name)      NAME="${2:-}"; shift 2 ;;
    --execute)   MODE="--execute"; shift ;;
    *) echo "ERROR: unknown argument: $1" >&2; exit 1 ;;
  esac
done

die()  { echo "ERROR: $*" >&2; exit 1; }
note() { echo "  $*"; }
act()  { if [ "$MODE" = "--execute" ]; then eval "$1" || die "step failed: $1"; else echo "  [dry-run] $1"; fi; }

[ -n "$REPO" ] && [ -n "$WT_ROOT" ] && [ -n "$MAIN" ] && [ -n "$NAME" ] \
  || die "usage: $0 --repo <path> --worktrees <path> --main <branch> --name <wt> [--execute]"
[ -d "$REPO/.git" ] || die "not a git repo root: $REPO"
REPO="$(cd "$REPO" && pwd -P)" || die "cannot resolve --repo"
WT_ROOT="$(cd "$WT_ROOT" && pwd -P)" || die "cannot resolve --worktrees"

# ------------------------------------------------------------ argument hardening
case "$NAME" in
  *[/\\]*|.|..|*..*|-*) die "illegal worktree name: '$NAME' (no slashes, dots-only, .. or leading -)" ;;
esac
printf '%s' "$NAME" | grep -Eq '^[A-Za-z0-9._-]+$' \
  || die "illegal worktree name: '$NAME' (allowed: A-Z a-z 0-9 . _ -)"

WT_PATH="$WT_ROOT/$NAME"
[ -e "$WT_PATH" ] || die "no such worktree: $WT_PATH"
[ -d "$WT_PATH" ] || die "not a directory: $WT_PATH"
[ -L "$WT_PATH" ] && die "refusing: '$NAME' is a symlink/junction, not a real worktree"

# Belt and braces: the resolved parent must be exactly the worktree root. This
# catches anything the name filter missed.
RESOLVED="$(cd "$WT_PATH" && pwd -P)" || die "cannot resolve $WT_PATH"
[ "$(dirname "$RESOLVED")" = "$WT_ROOT" ] \
  || die "refusing: '$NAME' resolves outside $WT_ROOT ($RESOLVED)"

STAMP="$(date +%Y%m%d-%H%M%S)"
BASE="$(basename "$REPO")"
BACKUP_ROOT="$(dirname "$REPO")/$BASE-BACKUP-$(date +%Y%m%d)"
RETIRED="$(dirname "$REPO")/$BASE-RETIRED"
DEST="$RETIRED/$STAMP-$NAME"

cd "$REPO" || die "cannot cd to $REPO"
BRANCH="$(git -C "$WT_PATH" rev-parse --abbrev-ref HEAD 2>/dev/null)" \
  || die "cannot read branch of $NAME"

echo "=== retire_worktree: $NAME (branch: $BRANCH, main: $MAIN) [$MODE] ==="

# ---------------------------------------------------------------- gate 1: merged
if git merge-base --is-ancestor "$BRANCH" "$MAIN" 2>/dev/null; then
  note "gate 1 OK: '$BRANCH' is merged into $MAIN."
else
  AHEAD="$(git rev-list --count "$MAIN".."$BRANCH" 2>/dev/null || echo '?')"
  echo
  echo "REFUSING: '$BRANCH' is $AHEAD commit(s) ahead of $MAIN — not merged."
  echo "Retirement is for CLOSED tasks only. Land the work first, and verify"
  echo "$MAIN's TREE afterwards, not just the PR status."
  exit 2
fi

# ------------------------------------------------- gate 2: uncommitted tracked work
if [ -n "$(git -C "$WT_PATH" diff --stat HEAD 2>/dev/null)" ]; then
  echo
  echo "REFUSING: '$NAME' has uncommitted changes to TRACKED files:"
  git -C "$WT_PATH" diff --stat HEAD | sed 's/^/    /'
  echo "Commit, stash, or discard them deliberately. This script will not guess."
  exit 3
fi
note "gate 2 OK: no uncommitted tracked changes."

# --------------------------------------------------- step 1: back up untracked work
# Redundant with quarantine (files move intact), but cheap, and it puts
# never-committed work somewhere obvious rather than buried in a retired tree.
UNTRACKED="$(git -C "$WT_PATH" ls-files --others --exclude-standard 2>/dev/null)"
if [ -n "$UNTRACKED" ]; then
  COUNT="$(printf '%s\n' "$UNTRACKED" | wc -l | tr -d ' ')"
  note "step 1: $COUNT untracked file(s) exist in NO commit — copying to backup."
  note "        -> $BACKUP_ROOT/worktrees/$NAME/"
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    act "mkdir -p \"\$(dirname '$BACKUP_ROOT/worktrees/$NAME/$f')\""
    act "cp -p '$WT_PATH/$f' '$BACKUP_ROOT/worktrees/$NAME/$f'"
  done <<< "$UNTRACKED"

  if [ "$MODE" = "--execute" ]; then
    BAD=0
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      a="$(sha256sum "$WT_PATH/$f" 2>/dev/null | cut -d' ' -f1)"
      b="$(sha256sum "$BACKUP_ROOT/worktrees/$NAME/$f" 2>/dev/null | cut -d' ' -f1)"
      if [ -z "$b" ] || [ "$a" != "$b" ]; then echo "  HASH MISMATCH: $f"; BAD=1; fi
    done <<< "$UNTRACKED"
    [ "$BAD" -eq 0 ] || die "backup verification FAILED — nothing moved."
    note "        backup verified by sha256 ($COUNT/$COUNT match)."
  fi
else
  note "step 1: no untracked files."
fi

# ------------------------------------------------------- step 2: quarantine (no rm)
# `git worktree remove` is deliberately NOT used: it DELETES. Move first, then
# let git prune its own bookkeeping for a now-absent worktree.
note "step 2: quarantining (move, not delete)."
note "        $WT_PATH -> $DEST/worktree"
act "mkdir -p '$DEST'"
act "mv '$WT_PATH' '$DEST/worktree'"
act "git worktree prune 2>/dev/null || true"
# On Windows, prune can fail with 'Permission denied' (observed 2026-09-07 on
# 7 retirements). Move the metadata aside rather than forcing a delete.
if [ "$MODE" != "--execute" ] || [ -d "$REPO/.git/worktrees/$NAME" ]; then
  act "mv '$REPO/.git/worktrees/$NAME' '$DEST/git-worktree-meta' 2>/dev/null || true"
fi

# ---------------------------------- step 3: verify RECOVERABILITY, not disappearance
# "Did it vanish?" is the wrong question — bug 2 above passed that check. The
# right question is whether the content is still readable in quarantine.
if [ "$MODE" = "--execute" ]; then
  echo
  echo "=== verification ==="
  [ -d "$WT_PATH" ] && echo "  FAIL: $WT_PATH still present" \
                    || echo "  OK: no longer under $WT_ROOT"
  FILES="$(find "$DEST/worktree" -type f 2>/dev/null | wc -l | tr -d ' ')"
  if [ "${FILES:-0}" -gt 0 ]; then
    echo "  OK: $FILES file(s) readable in quarantine ($DEST/worktree)"
  else
    echo "  FAIL: quarantine is EMPTY — content was destroyed, not moved. STOP."
    exit 1
  fi
  if SHA="$(git rev-parse --short "$BRANCH" 2>/dev/null)"; then
    echo "  OK: branch '$BRANCH' intact at $SHA (commits preserved)"
  else
    echo "  FAIL: branch '$BRANCH' MISSING — investigate before anything else"
  fi
fi

cat <<EOF

=== remaining, for Omri (this script does not do these) ===
  1. Delete the branch only when sure:  git branch -d $BRANCH
     (-d refuses if unmerged; never use -D here.)
  2. Reclaim disk when satisfied nothing was lost.
     Inspect first:  ls -la "$RETIRED"
     Then delete what you choose, yourself. This script will not.
EOF

if [ "$MODE" != "--execute" ]; then
  echo
  echo "DRY RUN — nothing changed. Re-run with --execute to quarantine."
fi
