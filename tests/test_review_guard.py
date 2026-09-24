"""T-0081: acceptance requires a written review. T-0021 went submitted -> accepted -> merged (PR #49,
2026-09-15) with review.md still the untouched template: `ops.land` applied `accepted` itself and nothing
read the review. Now `land`, `land_all_submitted` and `scr task set <id> accepted` all ask
`scr.review.acceptance_problem` — the same parser the watcher's auto-decide uses (T-0091) — and refuse a
`submitted` task unless review.md decides `accepted` and its mechanical table has at least one row.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scr.answers import apply
from scr.events import EventLog
from scr.gitops import Git
from scr.ops import land, land_all_submitted, spawn
from scr.review import acceptance_problem, mechanical_rows, review_decision
from scr.store import RepoError, Store
from test_journal_ops import REPO, git, scr, world  # noqa: F401  (scriptorium + workload repo + origin)

TEMPLATE = (REPO / "templates" / "review.md").read_text(encoding="utf-8")
PLACEHOLDER = "accepted | returned (feedback below) | escalated (Gate N, reason)"
ROW = "| `pytest tests -q` | 95 passed |"
LOG = Path("reports") / "steward_watch.log"


def _review(decision: str, rows: tuple[str, ...] | None = (ROW,), tail: str = "") -> str:
    """The template with the Decision placeholder replaced, `rows` under the mechanical table's rule, and
    `tail` appended (an explicit `Decision:` line, say)."""
    text = TEMPLATE.replace(PLACEHOLDER, decision)
    if rows:
        text = text.replace("|---|---|\n", "|---|---|\n" + "\n".join(rows) + "\n")
    return text + (tail + "\n" if tail else "")


def _submit(root: Path, tid: str = "T-0001", worktree: bool = True):
    for st in ("dispatched", "in_progress"):
        scr(root, "--quiet", "--actor", "s", "task", "set", tid, st)
    if worktree:
        spawn(root, [tid], lake=False)
    t = Store(root).get(tid)
    (t.dir / "report.md").write_text("# report\n\n## Result\n\nProved a.\n\n## Statement changes\n\nnone\n", encoding="utf-8")
    scr(root, "--quiet", "--actor", "s", "task", "set", tid, "submitted")
    return Store(root).get(tid)


def _events(root: Path, tid: str, kind: str, **match) -> list[dict]:
    return [e for e in EventLog(root / "events" / "events.jsonl").read()
            if e.get("kind") == kind and e.get("task") == tid and all(e.get(k) == v for k, v in match.items())]


# ---- the reading: review_decision / mechanical_rows / acceptance_problem -----------------------------------
def test_review_decision_reads_the_decision_section(tmp_path: Path):
    d = tmp_path / "T-0001-x"; d.mkdir()
    assert review_decision(d) is None                                            # no review.md at all
    (d / "review.md").write_text(TEMPLATE, encoding="utf-8")
    assert review_decision(d) is None                                            # the untouched template
    for v, line in (("accepted", "accepted"), ("returned", "returned (feedback below)"), ("escalated", "escalated (Gate 1, statement changed)")):
        (d / "review.md").write_text(_review(line), encoding="utf-8")
        assert review_decision(d) == v
    (d / "review.md").write_text(_review(PLACEHOLDER, tail="Decision: returned (the tests are red)"), encoding="utf-8")
    assert review_decision(d) == "returned"                                      # the explicit form
    (d / "review.md").write_text(TEMPLATE.replace(PLACEHOLDER, ""), encoding="utf-8")
    assert review_decision(d) is None                                            # an empty section
    (d / "review.md").write_text(_review("Not yet: the checks did not run."), encoding="utf-8")
    assert review_decision(d) is None                                            # unreadable: None, not a guess


def test_mechanical_rows_counts_only_the_table_body():
    assert mechanical_rows(TEMPLATE) == []
    assert mechanical_rows(_review("accepted")) == [ROW]
    assert mechanical_rows(_review("accepted", rows=(ROW, "| `scr verify` | 0 errors |"))) == [ROW, "| `scr verify` | 0 errors |"]
    # an aligned rule, a blank row and a table under another heading are not checks
    text = TEMPLATE.replace("|---|---|", "|:--|--:|").replace("## Statement diff", "| | |\n\n## Statement diff\n| a | b |")
    assert mechanical_rows(text) == []
    # a row quoted inside a code fence is not a check
    fenced = TEMPLATE.replace("|---|---|\n", "|---|---|\n```\n| pytest | passed |\n```\n")
    assert mechanical_rows(fenced) == []


def test_acceptance_problem_names_review_md(tmp_path: Path):
    d = tmp_path / "T-0001-x"; d.mkdir()
    p = acceptance_problem(d)
    assert p and "review.md" in p and "missing" in p
    (d / "review.md").write_text(TEMPLATE, encoding="utf-8")
    p = acceptance_problem(d)
    assert p and "review.md" in p and "no Decision" in p
    (d / "review.md").write_text(_review("returned (feedback below)"), encoding="utf-8")
    p = acceptance_problem(d)
    assert p and "review.md" in p and "`returned`" in p
    (d / "review.md").write_text(_review("accepted", rows=None), encoding="utf-8")
    p = acceptance_problem(d)
    assert p and "review.md" in p and "no row" in p
    (d / "review.md").write_text(_review("accepted", tail="Decision: maybe"), encoding="utf-8")
    p = acceptance_problem(d)
    assert p and "review.md" in p and "cannot be read" in p
    (d / "review.md").write_text(_review("accepted"), encoding="utf-8")
    assert acceptance_problem(d) is None
    (d / "review.md").write_text(_review("**accepted** — statement diff empty"), encoding="utf-8")
    assert acceptance_problem(d) is None


# ---- land -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("case", ["blank_template", "missing_file", "returned"])
def test_land_refuses_a_submitted_task_without_an_accepting_review(world, case):
    """DoD: land on a submitted task whose review decision is not `accepted` errors naming the task and
    review.md, opens no PR, pushes nothing, commits nothing, and leaves the status `submitted`."""
    root, repo, _ = world
    t = _submit(root)
    rev = t.dir / "review.md"
    if case == "blank_template":
        rev.write_text(TEMPLATE, encoding="utf-8")
    elif case == "missing_file":
        if rev.exists():
            rev.unlink()
    else:
        rev.write_text(_review("returned (feedback below)"), encoding="utf-8")
    wt = repo / ".worktrees" / "T-0001"
    (wt / "QuantumInsertion" / "A.lean").write_text("theorem a : True := by trivial\n", encoding="utf-8")
    head = Git(wt).head()
    n_state = len(_events(root, "T-0001", "state"))
    with pytest.raises(RepoError) as ei:
        land(root, "T-0001")
    msg = str(ei.value)
    assert "T-0001" in msg and "review.md" in msg, msg
    assert Store(root).get("T-0001").status == "submitted"
    assert Git(wt).head() == head and Git(wt).dirty()                          # nothing committed
    assert "task/T-0001" not in git(repo, "branch", "-r")                        # nothing pushed
    assert not (t.dir / "pr-body.md").exists()                                   # no PR
    assert len(_events(root, "T-0001", "state")) == n_state and not _events(root, "T-0001", "system", name="pr")


def test_land_accepts_with_a_written_review(world):
    root, repo, _ = world
    t = _submit(root)
    (t.dir / "review.md").write_text(_review("accepted"), encoding="utf-8")
    r = land(root, "T-0001")
    assert r["ok"], r["out"]
    assert Store(root).get("T-0001").status == "accepted"
    assert "task/T-0001" in git(repo, "branch", "-r")
    assert _events(root, "T-0001", "state", to="accepted")


def test_land_all_submitted_skips_unreviewed_tasks_and_reports_them(world):
    """DoD: land_all skips tasks that fail the guard and reports them, not fatal — the reviewed one lands."""
    root, repo, _ = world
    scr(root, "--quiet", "task", "new", "Prove b", "--id", "T-0002", "--files", "QuantumInsertion/B.lean", "--origin", "omri")
    for st in ("triaged", "ready"):
        scr(root, "--quiet", "task", "set", "T-0002", st)
    t1 = _submit(root, "T-0001")
    (t1.dir / "review.md").write_text(TEMPLATE, encoding="utf-8")
    t2 = _submit(root, "T-0002")
    (t2.dir / "review.md").write_text(_review("accepted"), encoding="utf-8")
    out = land_all_submitted(root)
    text = "\n".join(out)
    skipped = [l for l in out if l.startswith("T-0001: skipped")]
    assert len(skipped) == 1 and "review.md" in skipped[0] and "no Decision" in skipped[0], text
    assert "=== T-0002 [submitted]" in text and "T-0002: NOT landed" not in text and "T-0002: failed" not in text, text
    assert Store(root).get("T-0001").status == "submitted" and Store(root).get("T-0002").status == "accepted"
    assert "task/T-0001" not in git(repo, "branch", "-r") and "task/T-0002" in git(repo, "branch", "-r")


# ---- scr task set <id> accepted ----------------------------------------------------------------------------
def test_task_set_accepted_is_refused_until_the_review_is_written(world):
    root, _, _ = world
    t = _submit(root, worktree=False)
    (t.dir / "review.md").write_text(TEMPLATE, encoding="utf-8")
    n_state = len(_events(root, "T-0001", "state"))
    for actor in ("steward:T-0001", "s", "omri"):
        r = scr(root, "--quiet", "--actor", actor, "task", "set", "T-0001", "accepted", check=False)
        assert r.returncode == 2 and "T-0001" in r.stderr and "review.md" in r.stderr, (actor, r.stderr)
    # --force-accept is Omri's alone
    r = scr(root, "--quiet", "--actor", "steward:T-0001", "task", "set", "T-0001", "accepted", "--force-accept", check=False)
    assert r.returncode == 2 and "omri" in r.stderr and "review.md" in r.stderr, r.stderr
    # --force (the illegal-transition override) does not get past the review guard either
    r = scr(root, "--quiet", "--actor", "steward:T-0001", "task", "set", "T-0001", "accepted", "--force", check=False)
    assert r.returncode == 2 and "review.md" in r.stderr, r.stderr
    assert Store(root).get("T-0001").status == "submitted"
    assert len(_events(root, "T-0001", "state")) == n_state and not _events(root, "T-0001", "system", name="accept_override")
    # a decision other than accepted is refused too; a written acceptance goes through for any actor
    (t.dir / "review.md").write_text(_review("returned (feedback below)"), encoding="utf-8")
    r = scr(root, "--quiet", "--actor", "steward:T-0001", "task", "set", "T-0001", "accepted", check=False)
    assert r.returncode == 2 and "`returned`" in r.stderr, r.stderr
    (t.dir / "review.md").write_text(_review("accepted"), encoding="utf-8")
    r = scr(root, "--quiet", "--actor", "steward:T-0001", "task", "set", "T-0001", "accepted", check=False)
    assert r.returncode == 0, r.stderr
    assert Store(root).get("T-0001").status == "accepted"
    ev = _events(root, "T-0001", "state", to="accepted")
    assert len(ev) == 1 and ev[0]["actor"] == "steward:T-0001"
    assert not _events(root, "T-0001", "system", name="accept_override")


def test_omri_force_accept_records_an_override_event(world):
    root, _, _ = world
    t = _submit(root, worktree=False)
    (t.dir / "review.md").write_text(TEMPLATE, encoding="utf-8")
    r = scr(root, "--quiet", "--actor", "omri", "task", "set", "T-0001", "accepted", "--force-accept", "--note", "read the transcript myself", check=False)
    assert r.returncode == 0, r.stderr
    assert Store(root).get("T-0001").status == "accepted"
    ov = _events(root, "T-0001", "system", name="accept_override")
    assert len(ov) == 1 and ov[0]["actor"] == "omri", ov
    assert "review.md" in ov[0]["note"] and "read the transcript myself" in ov[0]["note"], ov[0]
    st = _events(root, "T-0001", "state", to="accepted")
    assert len(st) == 1 and st[0]["seq"] > ov[0]["seq"] and st[0]["actor"] == "omri"
    # the other transitions are untouched: omri_queue -> accepted (the gate card's path) needs no review
    assert "0 errors" in scr(root, "verify").stdout


def test_gate_accept_from_omri_queue_is_not_guarded(world):
    """The UI's gate card accepts an escalated task through omri_queue (scr/answers.py), where Omri's click
    IS the decision; the guard is on submitted -> accepted, the transition T-0021 slipped through."""
    root, _, _ = world
    t = _submit(root, worktree=False)
    (t.dir / "review.md").write_text(TEMPLATE, encoding="utf-8")
    for st in ("escalated", "omri_queue"):
        scr(root, "--quiet", "--actor", "steward:T-0001", "task", "set", "T-0001", st)
    r = scr(root, "--quiet", "--actor", "omri", "task", "set", "T-0001", "accepted", "--note", "Gate decision via UI: accept", check=False)
    assert r.returncode == 0, r.stderr
    assert Store(root).get("T-0001").status == "accepted"


# ---- T-0129: the gate answer on a task still `submitted` either lands (Omri's override) or says why not ---
def test_gate_accept_on_submitted_task_by_omri_is_the_override(world):
    """Omri's gate click on a submitted task with a blank review is the explicit override T-0081 designed:
    apply passes --force-accept with the answer as the note, and the accept_override event records it."""
    root, _, _ = world
    t = _submit(root, worktree=False)
    (t.dir / "review.md").write_text(TEMPLATE, encoding="utf-8")
    r = apply(root, "T-0001", "accept", kind="gate", actor="omri", concierge=False)
    assert r["ok"] is True, r
    assert Store(root).get("T-0001").status == "accepted"
    ov = _events(root, "T-0001", "system", name="accept_override")
    assert len(ov) == 1 and ov[0]["actor"] == "omri", ov
    assert "review.md" in ov[0]["note"] and "Gate decision via UI" in ov[0]["note"], ov[0]
    st = _events(root, "T-0001", "state", to="accepted")
    assert len(st) == 1 and st[0]["actor"] == "omri" and st[0]["seq"] > ov[0]["seq"], st


def test_gate_accept_on_submitted_task_by_another_actor_is_refused(world):
    """A Discuss session or the Concierge answering on Omri's behalf gets T-0081's refusal, not the
    override: ok False, the refusal in log, the task still submitted, no event appended."""
    root, _, _ = world
    t = _submit(root, worktree=False)
    (t.dir / "review.md").write_text(TEMPLATE, encoding="utf-8")
    r = apply(root, "T-0001", "accept", kind="gate", actor="concierge", concierge=False)
    assert r["ok"] is False, r
    assert Store(root).get("T-0001").status == "submitted"
    assert any("review.md" in m for m in r["log"]), r["log"]
    assert not _events(root, "T-0001", "system", name="accept_override")
    assert not _events(root, "T-0001", "state", to="accepted")


def test_gate_refused_answer_does_not_start_the_concierge(world, monkeypatch):
    """ok False means nothing to apply: start_concierge is not called; on the landed path it is."""
    root, _, _ = world
    t = _submit(root, worktree=False)
    (t.dir / "review.md").write_text(TEMPLATE, encoding="utf-8")
    calls: list[tuple] = []
    monkeypatch.setattr("scr.answers.start_concierge", lambda *a, **k: calls.append(a))
    r = apply(root, "T-0001", "accept", kind="gate", actor="concierge", concierge=True)
    assert r["ok"] is False and calls == [], (r, calls)
    assert Store(root).get("T-0001").status == "submitted"
    r = apply(root, "T-0001", "accept", kind="gate", actor="omri", concierge=True)
    assert r["ok"] is True and len(calls) == 1 and calls[0][1] == "T-0001", (r, calls)
    assert Store(root).get("T-0001").status == "accepted"


# ---- T-0128: the guard on every route into accepted, --force included --------------------------------------
# From the world fixture's `ready`; `triaged` is not reachable forward, so that one steps back with --force.
STEPS = {"ready": (), "triaged": ("triaged",), "dispatched": ("dispatched",),
         "in_progress": ("dispatched", "in_progress"), "checkpoint": ("dispatched", "in_progress", "checkpoint")}


def _bring_to(root: Path, status: str, tid: str = "T-0001"):
    for st in STEPS[status]:
        scr(root, "--quiet", "--actor", "s", "task", "set", tid, st, *(("--force",) if st == "triaged" else ()))
    t = Store(root).get(tid)
    assert t.status == status
    return t


@pytest.mark.parametrize("status", ["in_progress", "checkpoint", "dispatched", "triaged", "ready"])
def test_force_does_not_skip_the_review_guard(world, status):
    """`--force` overrides the lifecycle table, never the review: `task set <id> accepted --force` from a
    status other than submitted was the T-0081 review's open item (a) — the guard keyed on
    `old == "submitted"` and the forced route never read review.md."""
    root, _, _ = world
    t = _bring_to(root, status)
    (t.dir / "review.md").write_text(TEMPLATE, encoding="utf-8")
    n_state = len(_events(root, "T-0001", "state"))
    r = scr(root, "--quiet", "--actor", "steward:T-0001", "task", "set", "T-0001", "accepted", "--force", check=False)
    assert r.returncode != 0, r.stdout
    assert "T-0001" in r.stderr and "review.md" in r.stderr and f"{status} -> accepted" in r.stderr, r.stderr
    assert Store(root).get("T-0001").status == status
    assert not _events(root, "T-0001", "state", to="accepted") and len(_events(root, "T-0001", "state")) == n_state
    assert not _events(root, "T-0001", "system", name="accept_override")
    # a written acceptance passes the same forced call, and the forced transition is logged as such
    (t.dir / "review.md").write_text(_review("accepted"), encoding="utf-8")
    r = scr(root, "--quiet", "--actor", "steward:T-0001", "task", "set", "T-0001", "accepted", "--force", check=False)
    assert r.returncode == 0, r.stderr
    assert Store(root).get("T-0001").status == "accepted"
    ev = _events(root, "T-0001", "state", to="accepted")
    assert len(ev) == 1 and ev[0]["from"] == status and ev[0]["actor"] == "steward:T-0001" and ev[0]["note"].endswith("[forced]"), ev
    assert not _events(root, "T-0001", "system", name="accept_override")


def test_omri_force_accept_from_in_progress_records_an_override_event(world):
    """The only forced way past an unwritten review is Omri's --force-accept, on this route as on the
    submitted one: exactly one accept_override event, before the state event."""
    root, _, _ = world
    t = _bring_to(root, "in_progress")
    (t.dir / "review.md").write_text(TEMPLATE, encoding="utf-8")
    # --force alone (the transition override) is still refused, and so is --force-accept from anyone else
    r = scr(root, "--quiet", "--actor", "steward:T-0001", "task", "set", "T-0001", "accepted", "--force", "--force-accept", check=False)
    assert r.returncode == 2 and "omri" in r.stderr and "review.md" in r.stderr and "in_progress -> accepted" in r.stderr, r.stderr
    assert Store(root).get("T-0001").status == "in_progress" and not _events(root, "T-0001", "system", name="accept_override")
    r = scr(root, "--quiet", "--actor", "omri", "task", "set", "T-0001", "accepted", "--force", "--force-accept", "--note", "why", check=False)
    assert r.returncode == 0, r.stderr
    assert Store(root).get("T-0001").status == "accepted"
    ov = _events(root, "T-0001", "system", name="accept_override")
    assert len(ov) == 1 and ov[0]["actor"] == "omri" and "review.md" in ov[0]["note"] and "why" in ov[0]["note"], ov
    st = _events(root, "T-0001", "state", to="accepted")
    assert len(st) == 1 and st[0]["seq"] > ov[0]["seq"] and st[0]["from"] == "in_progress" and st[0]["note"] == "why [forced]", st


# ---- the watcher's auto-decide goes through the same guard ------------------------------------------------
def test_watcher_auto_decide_accepted_without_checks_is_refused(world):
    """`Decision: accepted` with an empty mechanical table: the watcher's `scr task set` is refused, the log
    says FAILED, a NEEDS-YOU line names the task, and the status stays submitted (no transition)."""
    from scr.watch import Watcher
    root, _, _ = world
    t = _submit(root, worktree=False)
    (t.dir / "review.md").write_text(_review("accepted", rows=None), encoding="utf-8")
    n_state = len(_events(root, "T-0001", "state"))
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert res["ok"], res
    assert Store(root).get("T-0001").status == "submitted" and res["decided"] == []
    assert len(_events(root, "T-0001", "state")) == n_state
    log = (root / LOG).read_text(encoding="utf-8")
    assert "FAILED" in log and "review.md" in log, log
    assert any(n.startswith("T-0001") and "could not be applied" in n for n in res["needs"]), res["needs"]
    # the Steward adds the rows: the next tick applies it
    (t.dir / "review.md").write_text(_review("accepted"), encoding="utf-8")
    res = Watcher(root, quiet=True, auto_decide=True).tick()
    assert Store(root).get("T-0001").status == "accepted" and res["decided"] == [{"task": "T-0001", "decision": "accepted"}]


# ---- T-0021, replayed ---------------------------------------------------------------------------------------
def test_t0021_record_replay_is_refused(world):
    """DoD: T-0021's record folder (its review.md is the untouched template) copied in with status
    `submitted`; land refuses, and so does `task set accepted`."""
    root, repo, _ = world
    src = next(REPO.glob("tasks/T-0021-*"))
    dst = root / "tasks" / src.name
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("session-*", "*.txt", "pr-body.md"))   # pr-body.md: PR #49's, the landing under test
    assert (dst / "review.md").read_text(encoding="utf-8").strip() == TEMPLATE.strip()      # the blank template, still
    rec = yaml.safe_load((dst / "task.yaml").read_text(encoding="utf-8"))
    rec["status"] = "submitted"; rec.pop("worktree", None)
    (dst / "task.yaml").write_text(yaml.safe_dump(rec, sort_keys=False, allow_unicode=True), encoding="utf-8")
    spawn(root, ["T-0021"], lake=False)
    wt = repo / ".worktrees" / "T-0021"
    assert wt.is_dir() and Git(wt).branch() == "task/T-0021"
    with pytest.raises(RepoError) as ei:
        land(root, "T-0021", merge=True)
    assert "T-0021" in str(ei.value) and "review.md" in str(ei.value), str(ei.value)
    assert Store(root).get("T-0021").status == "submitted"
    assert "task/T-0021" not in git(repo, "branch", "-r") and not (dst / "pr-body.md").exists()
    r = scr(root, "--quiet", "--actor", "steward:T-0021", "task", "set", "T-0021", "accepted", check=False)
    assert r.returncode == 2 and "review.md" in r.stderr, r.stderr
    assert Store(root).get("T-0021").status == "submitted"
    out = land_all_submitted(root)
    assert any(l.startswith("T-0021: skipped") and "review.md" in l for l in out), out


# ---- one parser, re-exported ------------------------------------------------------------------------------
def test_one_read_decision_definition_and_watch_reexports_it():
    defs = [p for p in (REPO / "scr").glob("*.py") if re.search(r"(?m)^def read_decision\(", p.read_text(encoding="utf-8"))]
    assert [p.name for p in defs] == ["review.py"], defs
    r = subprocess.run([sys.executable, "-c", "from scr.watch import decision_of, read_decision, review_digest, DECISION_NOTE; "
                        "from scr.review import decision_of as d2; assert decision_of is d2; print('ok')"],
                       cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr


# ---- T-0193: `Decision: <verdict>` as the first line under `## Decision` ----------------------------------
def test_explicit_line_under_the_decision_heading_is_read():
    from scr.review import read_decision
    assert read_decision(_review("Decision: accepted")) == ("accepted", None)
    assert read_decision(_review("Decision: returned (the tests are red)")) == ("returned", None)
    assert read_decision(_review("Decision: **escalated** (Gate 1, statement changed)")) == ("escalated", None)
    # the brief's placeholder copied under the heading with the prefix is still no decision
    assert read_decision(_review("Decision: " + PLACEHOLDER)) == (None, None)
    # still unreadable / still disagreeing where it was before
    v, problem = read_decision(_review("Decision: maybe"))
    assert v is None and "unreadable" in problem
    v, problem = read_decision(_review("accepted", tail="Decision: returned (fix the lemma)"))
    assert v is None and "disagree" in problem, problem
    assert read_decision(_review("Decision: accepted", tail="Decision: returned")) == ("accepted", None)   # first explicit line wins, as before
    assert read_decision(_review("Decision: accepted", tail="Decision: accepted")) == ("accepted", None)
    v, problem = read_decision("Decision: returned\n\n" + _review("Decision: accepted"))
    assert v is None and "disagree" in problem, problem   # an earlier explicit line vs the heading's


# ---- T-0130: the Discuss brief shows the Decision the same parser reads -------------------------------------
HEADING = "review.md — Decision"


@pytest.mark.parametrize("form, present, absent", [
    ("section", ("accepted",), ()),                                            # (1) template form
    ("explicit", ("returned",), (PLACEHOLDER,)),                               # (2) explicit line, placeholder untouched
    ("template", (), (HEADING,)),                                              # (3) the blank template
    ("fenced", (), (HEADING,)),                                                # (4) a verdict inside a fence only
    ("feedback", ("returned (feedback below)", "- lemma `a` is misstated"), ()),
    ("unreadable", ("unreadable", "Not yet"), ()),
])
def test_discuss_brief_shows_the_decision_the_parser_reads(world, form, present, absent):
    """`scr.discuss._task_context` asks `scr.review.read_decision` — the guard's and the watcher's parser —
    and shows the verdict on one line, feedback lines under it; never the placeholder, never a fence."""
    from scr.discuss import _task_context
    root, _, _ = world
    t = Store(root).get("T-0001")
    text = {"section": _review("accepted"),
            "explicit": _review(PLACEHOLDER, tail="Decision: returned (fix the lemma)"),
            "template": TEMPLATE,
            "fenced": TEMPLATE.replace("## Decision\n" + PLACEHOLDER + "\n", "") + "```\n## Decision\naccepted\n\nDecision: accepted\n```\n",
            "feedback": _review("returned (feedback below)\n- lemma `a` is misstated"),
            "unreadable": _review("Not yet: the checks did not run.")}[form]
    (t.dir / "review.md").write_text(text, encoding="utf-8")
    brief = _task_context(root, "T-0001")
    for s in present:
        assert s in brief, (s, brief)
    for s in absent:
        assert s not in brief, (s, brief)
    if present:                                                                # the block: heading, then one verdict line
        lines = brief[brief.index(HEADING):].splitlines()
        assert lines[1].startswith(present[0]), lines[:3]
        assert review_decision(t.dir) in (None, lines[1].split()[0]), (review_decision(t.dir), lines[1])
