"""What a task's review.md says, read one way for everyone (T-0081).

T-0091 gave the watcher a parser for the Decision a Steward writes in review.md; T-0081 makes acceptance
depend on it. T-0021 went `submitted -> accepted -> merged` (PR #49, 2026-09-15) while its review.md was the
untouched template, because `ops.land` applied `accepted` itself and nothing looked at the review. Both
`land` and `scr task set <id> accepted` now refuse a `submitted` task unless the review states `accepted`
and its mechanical table has at least one row — and both ask THIS module, so the guard and the watcher's
auto-decide can never disagree on what a Decision is. `scr/watch.py` imports `scr/ops.py`, so the parser
lives here, below both, and `watch.py` re-exports it.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

# ---- the Decision written in review.md (T-0091) ------------------------------------------------------
VERDICTS = ("accepted", "returned", "escalated")
# the explicit form the review brief asks for, anchored at column zero: `Decision: escalated (Gate 2, reason)`
_DECISION_LINE = re.compile(r"^Decision:")
_DECISION_VERDICT = re.compile(r"^Decision:\s*[*_`]*(accepted|returned|escalated)\b")
# the template form: the first non-blank line under a `## Decision` heading, its first word the verdict
_DECISION_HEADING = re.compile(r"^#{1,6}\s*Decision\b")
_ANY_HEADING = re.compile(r"^#{1,6}\s")
_SECTION_VERDICT = re.compile(r"^\s*[*_`]*(accepted|returned|escalated)\b")
# templates/review.md's placeholder: `accepted | returned (feedback below) | escalated (Gate N, reason)`
_PLACEHOLDER = re.compile(r"^\s*accepted\s*\|\s*returned\b.*\|\s*escalated\b")
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
DECISION_NOTE = "applied from review.md by the watcher"
# templates/review.md's `## Mechanical checks` table: `| check | result |` then `|---|---|`, rows below
_MECHANICAL_HEADING = re.compile(r"^#{1,6}\s*Mechanical\b")
_TABLE_ROW = re.compile(r"^\s*\|")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}")


def _unfenced(text: str) -> list[str]:
    """The lines of a markdown text that are not inside a fenced code block (a review that quotes a draft,
    like T-0050's transcript, must not be read as a verdict)."""
    out, fence = [], None
    for line in text.splitlines():
        m = _FENCE.match(line)
        if m and (fence is None or m.group(1) == fence):
            fence = None if fence else m.group(1)
            continue
        if fence is None:
            out.append(line)
    return out


def _verdict(line: str, rx: re.Pattern) -> str | None:
    """The verdict a candidate line states, or None when it names none or more than one
    (`accepted | returned` left over from the placeholder is ambiguous, not a verdict)."""
    m = rx.match(line)
    if not m:
        return None
    others = [v for v in VERDICTS if v != m.group(1) and re.search(rf"\|\s*[*_`]*{v}\b", line)]
    return None if others else m.group(1)


def read_decision(review_md: str) -> tuple[str | None, str | None]:
    """(verdict, problem) from a review.md text. The verdict comes from the FIRST `Decision:` line at column
    zero outside a code fence (the explicit form the review brief asks for) or, failing that, from the
    first non-blank line under a `## Decision` heading whose first word is a verdict (the template form
    T-0062's review used). The untouched placeholder is no decision. When both forms are present they
    must agree. `problem` is set — with the offending line — when a Decision is written but cannot be
    read (`Decision: maybe`, a section that opens with prose, the two forms disagreeing): that is for
    Omri, never a transition."""
    lines = _unfenced(review_md)
    explicit = next((l for l in lines if _DECISION_LINE.match(l)), None)
    section = None
    for i, line in enumerate(lines):
        if _DECISION_HEADING.match(line):
            for nxt in lines[i + 1:]:
                if _ANY_HEADING.match(nxt):
                    break
                if nxt.strip():
                    section = nxt
                    break
            break
    if section is not None and _PLACEHOLDER.match(section):
        section = None
    if explicit is not None and _PLACEHOLDER.match(explicit[len("Decision:"):]):   # the brief's own wording, copied
        explicit = None
    if explicit is None and section is None:
        return None, None
    found: dict[str, str | None] = {}
    if explicit is not None:
        found[explicit] = _verdict(explicit, _DECISION_VERDICT)
    if section is not None:
        found[section] = _verdict(section, _SECTION_VERDICT)
    bad = [l for l, v in found.items() if v is None]
    if bad:
        return None, f"unreadable Decision line {bad[0].strip()[:120]!r}"
    verdicts = set(found.values())
    if len(verdicts) > 1:
        a, b = list(found.items())[:2]
        return None, f"Decision lines disagree: {a[0].strip()[:60]!r} vs {b[0].strip()[:60]!r}"
    return verdicts.pop(), None


def decision_of(review_md: str) -> str | None:
    """`accepted` / `returned` / `escalated` when review.md states one verdict unambiguously, else None."""
    return read_decision(review_md)[0]


def review_digest(review_md: str) -> str:
    """Names one review text in the transition's note so the same review is never applied twice."""
    return hashlib.sha256(review_md.encode("utf-8")).hexdigest()[:12]


# ---- the review as a whole: what acceptance needs (T-0081) -------------------------------------------
def review_text(task_dir: Path) -> str | None:
    """The task's review.md, or None when there is none."""
    p = Path(task_dir) / "review.md"
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8", errors="replace")


def review_decision(task_dir: Path) -> str | None:
    """The verdict written in `<task_dir>/review.md` — `accepted` / `returned` / `escalated` — or None: no
    file, the untouched template, an empty `## Decision` section, or a Decision the parser cannot read
    (`acceptance_problem` tells those apart). The same `read_decision` the watcher's auto-decide uses."""
    text = review_text(task_dir)
    return None if text is None else decision_of(text)


def mechanical_rows(review_md: str) -> list[str]:
    """The body rows of the table under `## Mechanical checks` (the header `| check | result |` and the
    `|---|---|` rule excluded), outside code fences. The template's table is empty: a review that ran no
    check has no row here."""
    lines = _unfenced(review_md)
    rows: list[str] = []
    for i, line in enumerate(lines):
        if not _MECHANICAL_HEADING.match(line):
            continue
        header_seen = False
        for nxt in lines[i + 1:]:
            if _ANY_HEADING.match(nxt):
                break
            if not _TABLE_ROW.match(nxt):
                continue
            cells = [c.strip() for c in nxt.strip().strip("|").split("|")]
            if all(_TABLE_RULE.match(c) or c == "" for c in cells) and any(_TABLE_RULE.match(c) for c in cells):
                continue                                                    # the |---|---| rule
            if not header_seen:
                header_seen = True                                          # the first row is the header
                continue
            if any(cells):
                rows.append(nxt.strip())
        break
    return rows


def acceptance_problem(task_dir: Path) -> str | None:
    """Why `<task_dir>/review.md` does not support `accepted`, as one sentence naming review.md — or None
    when it does: the Decision reads `accepted` and the mechanical table has at least one row. This is
    the guard `ops.land`, `ops.land_all_submitted` and `scr task set <id> accepted` all apply to a
    `submitted` task (T-0081); `--actor omri --force-accept` is the only way past it, and that is logged."""
    p = Path(task_dir) / "review.md"
    text = review_text(task_dir)
    if text is None:
        return f"no review written: {p} is missing"
    verdict, problem = read_decision(text)
    if problem:
        return f"{p.name} has a Decision that cannot be read ({problem})"
    if verdict is None:
        return f"no Decision written in {p.name} (the `## Decision` section is the template placeholder or empty)"
    if verdict != "accepted":
        return f"{p.name} decides `{verdict}`, not `accepted`"
    if not mechanical_rows(text):
        return f"{p.name} decides `accepted` but its `## Mechanical checks` table has no row"
    return None
