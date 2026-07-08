#!/usr/bin/env python3
"""
AgentOS - Stage 5a: The Guardrail

An always-on loop running while you sleep needs exactly two protections, and they
are the whole reason this layer is safe:

  1. THE IRREVERSIBLE-ACTION BOUNDARY. Unattended, the system may sense, question,
     build into drafts/, and emit a trigger card. It may NOT do anything that
     touches the world: no push, no send, no deploy, no spend past the cap, no
     deletion. Those wait for you. This file is the single place that boundary is
     defined and enforced - so it can't drift or be quietly widened.

  2. THE DARK-DAYS DEAD-MAN'S-SWITCH. If you go dark - no recorded outcomes for N
     days - a system that keeps building artifacts you'll never land is just
     funding your disappearance and burning capacity. So past a threshold the
     backbone stops building and changes mode: it routes you to a human, because
     the record is unambiguous that you re-enter through people, not tasks.

This module decides, before each unattended cycle, whether to RUN, RUN-REDUCED,
or HOLD - and what (if anything) the system is allowed to do this cycle.
"""

import json
from datetime import date, datetime

import state_layer as sl

# How many days without a recorded outcome before the dead-man's-switch trips.
DARK_DAYS_THRESHOLD = 3

# Actions that are NEVER permitted unattended. The Builder already refuses these,
# but the boundary is declared here too so there is one source of truth.
FORBIDDEN_UNATTENDED = [
    "push", "send", "deploy", "publish", "submit", "pay", "transfer",
    "delete", "rm ", "email", "post ",
]


def _last_activity_date(board):
    """Most recent date the human left a real signal (an outcome or a shipped item)."""
    dates = []
    out = board.get("last_outcome", {})
    if out.get("date"):
        dates.append(out["date"])
    for item in board.get("shipped", []):
        if item.get("date"):
            dates.append(item["date"])
    parsed = [sl.parse_date(d) for d in dates]
    parsed = [d for d in parsed if d]
    return max(parsed) if parsed else None


def days_dark(board):
    last = _last_activity_date(board)
    if last is None:
        return None
    return (date.today() - last).days


def budget_ok(board):
    r = board.get("resources", {})
    return r.get("api_spent_usd", 0.0) < r.get("api_budget_usd", 0.0)


def assess(board):
    """Return a decision dict the runner obeys before doing anything."""
    dark = days_dark(board)

    # Dead-man's-switch: too long with no signal -> stop building, route to a human.
    if dark is not None and dark >= DARK_DAYS_THRESHOLD:
        return {
            "mode": "HOLD",
            "allow_build": False,
            "allow_irreversible": False,
            "dark_days": dark,
            "message": (
                f"You've been dark {dark} days (no recorded outcome). "
                "Not building more - artifacts you won't land don't help. "
                "Re-entry is through a person, not a task: send your cousin a voice "
                "note, or reply to H. Then run the loop and the backbone resumes."
            ),
        }

    # Budget exhausted on the paid path -> reduced mode (prepaid/paste only, no API spend).
    if not budget_ok(board):
        return {
            "mode": "RUN_REDUCED",
            "allow_build": True,            # only on prepaid capacity / paste mode
            "allow_irreversible": False,
            "dark_days": dark,
            "message": "API budget reached. Running on prepaid capacity only; no API spend.",
        }

    return {
        "mode": "RUN",
        "allow_build": True,
        "allow_irreversible": False,        # ALWAYS false unattended - by design
        "dark_days": dark,
        "message": "Clear to run an unattended cycle. Irreversible actions remain yours.",
    }


def is_forbidden_unattended(action_text):
    """True if a proposed unattended action would touch the world."""
    t = (action_text or "").lower()
    return any(tok in t for tok in FORBIDDEN_UNATTENDED)


if __name__ == "__main__":
    board = sl.json.loads(sl.GROUND_STATE_PATH.read_text(encoding="utf-8")) \
        if sl.GROUND_STATE_PATH.exists() else {}
    decision = assess(board)
    print(json.dumps(decision, indent=2))
