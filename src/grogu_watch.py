"""A live view of the agents Grogu has running, so the user can steer them.

Steering already works in one direction: `grogu plan steer` reaches agents that
have not started yet, and rides out to running ones on the output of whatever
`grogu` command they run next. What was missing is the other direction. The user
was expected to steer without being able to see anything — which agents exist,
what stage each is on, which one has been stuck for ten minutes, and which one
is about to do the wrong thing. Steering blind is guessing.

The activity feed is deliberately passive. Nothing asks an agent to report, and
no model spends a token producing this: every `grogu` invocation already passes
through one place on its way out, so it records *that it happened* — role, plan,
subcommand, working directory, exit status — and the board is assembled from
those records plus the plan state the agents were already writing anyway.

Two consequences worth stating. The log lives under `GROGU_HOME` rather than in
any repository, because agents work in separate worktrees and a per-worktree log
would give the user one window per agent instead of one board for all of them; it is
also therefore never committed. And only the *name* of the subcommand is
recorded, never its arguments — `grogu plan steer "<text>"` and
`grogu plan friction --note "<text>"` carry exactly the content that should not
be accumulating in a file nobody remembers exists.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

# An agent that has not run a grogu command in this long is probably thinking,
# running a build, or gone. The board says which, by showing the age rather
# than by pretending to know.
IDLE_AFTER_SECONDS = 300
GONE_AFTER_SECONDS = 3600
DEFAULT_WINDOW_MINUTES = 120
MAX_ACTIVITY_LINES = 4000


def activity_path(home: Optional[Path] = None) -> Path:
    base = Path(home or os.environ.get("GROGU_HOME") or (Path.home() / ".grogu"))
    return base.expanduser() / "activity.jsonl"


def record(
    *,
    command: str,
    role: str = "",
    agent: str = "",
    plan: str = "",
    repository: str = "",
    cwd: str = "",
    exit_code: int = 0,
    home: Optional[Path] = None,
) -> None:
    """Append one activity record. Never raises: watching must not break work.

    Called from the CLI's exit path for every grogu subcommand. `command` is the
    subcommand path only ("plan gate"), never the arguments.
    """
    path = activity_path(home)
    entry = {
        "at": time.time(),
        "command": command,
        "role": role,
        "agent": agent,
        "plan": plan,
        "repository": repository,
        "cwd": cwd,
        "exit": exit_code,
        "pid": os.getpid(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        return
    _trim(path)


def _trim(path: Path) -> None:
    """Keep the log bounded. Opportunistic: a failure here costs nothing."""
    try:
        if path.stat().st_size < 512_000:
            return
        lines = path.read_text(encoding="utf8").splitlines()
        if len(lines) <= MAX_ACTIVITY_LINES:
            return
        path.write_text(
            "\n".join(lines[-MAX_ACTIVITY_LINES:]) + "\n", encoding="utf8"
        )
    except OSError:
        return


def activity(
    *, window_minutes: int = DEFAULT_WINDOW_MINUTES, home: Optional[Path] = None
) -> list:
    path = activity_path(home)
    if not path.exists():
        return []
    cutoff = time.time() - window_minutes * 60
    entries = []
    try:
        for line in path.read_text(encoding="utf8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue  # a torn final line is not a reason to show nothing
            if float(entry.get("at", 0)) >= cutoff:
                entries.append(entry)
    except OSError:
        return []
    return entries


def sessions(
    *, window_minutes: int = DEFAULT_WINDOW_MINUTES, home: Optional[Path] = None
) -> list:
    """Collapse the feed into one row per agent.

    Identity is (cwd, role, plan, agent) rather than pid, because a subagent is
    a sequence of separate `grogu` processes in one working directory, and pid
    would show a hundred one-command agents instead of one working agent.

    The agent name is part of the key because parallel workstreams are the
    whole point of the fan-out: two engineers on one plan in one checkout
    collapsed into a single row, so the board could not show the thing it
    exists to show, and the relay hint could not name who to relay to.
    """
    rows: dict = {}
    for entry in activity(window_minutes=window_minutes, home=home):
        key = (
            entry.get("cwd", ""),
            entry.get("role", ""),
            entry.get("plan", ""),
            entry.get("agent", ""),
        )
        row = rows.setdefault(
            key,
            {
                "cwd": entry.get("cwd", ""),
                "role": entry.get("role", ""),
                "agent": entry.get("agent", ""),
                "plan": entry.get("plan", ""),
                "repository": entry.get("repository", ""),
                "calls": 0,
                "first": entry.get("at", 0),
                "last": 0,
                "last_command": "",
                "failures": 0,
            },
        )
        row["calls"] += 1
        row["first"] = min(row["first"], entry.get("at", 0))
        if entry.get("at", 0) >= row["last"]:
            row["last"] = entry.get("at", 0)
            row["last_command"] = entry.get("command", "")
        if entry.get("exit", 0) not in (0, None):
            row["failures"] += 1
    now = time.time()
    for row in rows.values():
        row["idle_seconds"] = int(now - row["last"])
        row["state"] = (
            "gone"
            if row["idle_seconds"] >= GONE_AFTER_SECONDS
            else "idle"
            if row["idle_seconds"] >= IDLE_AFTER_SECONDS
            else "working"
        )
    return sorted(rows.values(), key=lambda row: -row["last"])


def _age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def board(
    *,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    home: Optional[Path] = None,
    plan_summaries: Optional[dict] = None,
) -> dict:
    rows = [row for row in sessions(window_minutes=window_minutes, home=home)]
    agents = [row for row in rows if row["role"]]
    return {
        "agents": agents,
        "other": [row for row in rows if not row["role"]],
        "plans": plan_summaries or {},
    }


def waiting_on_you(state: dict) -> list:
    """The things that stop without the user, gathered from every plan.

    The board answered "who is running", which is the question you ask once.
    The question you ask every time you look is "is anything stuck on me", and
    that was buried under one plan block per plan, below a list of agents.
    """
    asks: list = []
    for plan_id, summary in sorted(state.get("plans", {}).items()):
        title = summary.get("title", "")
        if summary.get("review_required") and not summary.get("approved_at"):
            asks.append(
                f"approve the plan: grogu plan approve {plan_id}   ({title})"
            )
        if summary.get("escalated"):
            asks.append(
                f"the engineer and tester stopped converging on {plan_id}; "
                "the architect needs your call"
            )
        for note in summary.get("steering_undelivered", []):
            who = ", ".join(note.get("unread_by") or [])
            asks.append(
                f"steering #{note['seq']} has not reached {who} on {plan_id}: "
                + note.get("text", "")[:60]
            )
    return asks


def render(state: dict, *, window_minutes: int = DEFAULT_WINDOW_MINUTES) -> str:
    """The board as text. Reads top-down: what needs you, who is running, what blocks."""
    lines: list = []
    asks = waiting_on_you(state)
    if asks:
        lines.append("waiting on you")
        lines.append("")
        for ask in asks:
            lines.append(f"  ! {ask}")
        lines.append("")
    agents = state.get("agents", [])
    if not agents:
        lines.append(f"No agent has run a grogu command in the last {window_minutes}m.")
    else:
        lines.append(f"agents active in the last {window_minutes}m")
        lines.append("")
        for row in agents:
            mark = {"working": "●", "idle": "◐", "gone": "○"}.get(row["state"], "?")
            plan = row["plan"] or "-"
            failures = f"  {row['failures']} failed" if row["failures"] else ""
            # Naming the agent is what makes a fan-out legible: three engineers
            # on one plan were three identical lines saying "engineer".
            who = row["role"]
            if row.get("agent") and not row["agent"].startswith("/"):
                who = f"{row['role']}@{row['agent']}"
            lines.append(
                f"  {mark} {who:<9} plan {plan:<10} "
                f"last {row['last_command'] or '?'} {_age(row['idle_seconds'])} ago"
                f"  ({row['calls']} call{'' if row['calls'] == 1 else 's'}{failures})"
            )
            where = row["repository"] or row["cwd"]
            if where:
                lines.append(f"      {where}")

    for plan_id, summary in sorted(state.get("plans", {}).items()):
        lines.append("")
        lines.append(f"plan {plan_id}  {summary.get('status', '?')}  {summary.get('title', '')}")
        stage_state = summary.get("stage_state", {})
        if stage_state:
            lines.append(
                "  stages: "
                + ", ".join(f"{stage}={value}" for stage, value in stage_state.items())
            )
        if summary.get("escalated"):
            lines.append("  ! escalated to the architect: engineer/tester stopped converging")
        if summary.get("review_required") and summary.get("status") != "approved":
            lines.append("  ! waiting on you: `grogu plan approve " + plan_id + "`")
        for amendment in summary.get("open_amendments", []):
            lines.append(
                f"  amendment {amendment.get('id')} from {amendment.get('raised_by')}: "
                f"{amendment.get('claim', '')[:70]}"
            )
        for defect in summary.get("open_defects", []):
            lines.append(
                f"  defect {defect.get('id')} -> {defect.get('owner')} "
                f"({defect.get('route')}): {defect.get('report', '')[:60]}"
            )
        pending = {
            role: count
            for role, count in (summary.get("steering_pending") or {}).items()
            if count
        }
        for role, count in sorted(pending.items()):
            lines.append(f"  {count} steering note(s) the {role} has not read yet")

    lines.append("")
    lines.append("steer: grogu plan steer \"<text>\" [--plan <id>] [--role <role>] [--requires-replan]")
    return "\n".join(lines)
