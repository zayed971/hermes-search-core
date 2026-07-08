#!/usr/bin/env python3
"""
AgentOS - Stage 6: The Transfer Schedule

This is the layer that makes the system self-erasing instead of a permanent
crutch - the thing that makes it higher than Jarvis. Every other layer does work
FOR you. This one measures whether you're absorbing the work, and hands more of
it back as you prove you can carry it.

The mechanism, plainly:
  - It reads the decisions/outcomes history (the ledger the loop + Questioner write).
  - For a given KIND of move (e.g. "push to GitHub"), it counts how many times you
    actually pulled that kind of trigger.
  - The more you've pulled it, the LESS the system scaffolds it:
        level 0 (0-1 pulls):  full hand-holding - exact commands, every step.
        level 1 (2-3 pulls):  the commands, but one blank you fill in.
        level 2 (4-6 pulls):  just the move + where, no commands. "You know this."
        level 3 (7+ pulls):   the system stops issuing this kind of card at all -
                              it assumes you own it now, and says so.

So the success metric INVERTS: the system is winning when it needs to do less.
A move you've done seven times is a move you've absorbed - it should leave the
board, because the capability now lives in you, not in the process.

This is deliberately near-inert at first (the Questioner flagged this honestly):
with one real outcome logged, almost everything is level 0. It sharpens with every
real move you record. That is the point - it earns its fade by watching you grow.

Usage:
  python transfer.py "push to github"     # show the current scaffolding level for a move-kind
  python transfer.py --report             # show absorption across all move-kinds seen
"""

import argparse
import json
import re
from pathlib import Path

import state_layer as sl

DECISIONS_PATH = sl.STATE_DIR / "decisions.json"

# Move-kinds: how we bucket triggers so "pushed 5 times" means something.
KIND_PATTERNS = {
    "push_to_github": ["push", "git ", "commit", "repo", "github"],
    "send_message":   ["send", "message", "email", "reply", "deliver", "mohammed", "outreach"],
    "publish":        ["publish", "post", "deploy"],
    "submit_app":     ["apply", "submit", "application", "cv"],
    "edit_file":      ["clean", "fix", "edit", "readme", "update"],
}

LEVELS = {
    0: "FULL  - exact commands, every step spelled out.",
    1: "GUIDED - the commands, but one blank you fill in yourself.",
    2: "LIGHT - just the move and where. No commands. You know this.",
    3: "OWNED - the system stops issuing this kind of card. It's yours now.",
}


def classify_kind(text):
    t = (text or "").lower()
    best, best_hits = None, 0
    for kind, kws in KIND_PATTERNS.items():
        hits = sum(1 for k in kws if k in t)
        if hits > best_hits:
            best, best_hits = kind, hits
    return best or "other"


def load_pulls():
    """Count, per move-kind, how many recorded outcomes were actually pulled."""
    counts = {}
    # Source 1: the live board's last_outcome (one entry).
    if sl.GROUND_STATE_PATH.exists():
        s = json.loads(sl.GROUND_STATE_PATH.read_text(encoding="utf-8"))
        out = s.get("last_outcome", {})
        if out.get("did_you_pull_it"):
            k = classify_kind(out.get("trigger_issued", ""))
            counts[k] = counts.get(k, 0) + 1
    # Source 2: the decisions ledger, if outcomes get appended there over time.
    if DECISIONS_PATH.exists():
        for d in json.loads(DECISIONS_PATH.read_text(encoding="utf-8")):
            det = d.get("detail", {}) or {}
            # only count entries that represent a pulled outcome, not a verdict
            if det.get("did_you_pull_it"):
                k = classify_kind(d.get("action", "") or det.get("trigger_issued", ""))
                counts[k] = counts.get(k, 0) + 1
    return counts


def level_for(pulls):
    if pulls >= 7:
        return 3
    if pulls >= 4:
        return 2
    if pulls >= 2:
        return 1
    return 0


def scaffold(move_text, full_commands=""):
    """Return the trigger-card ACTION for a move, scaffolded to the human's level."""
    kind = classify_kind(move_text)
    pulls = load_pulls().get(kind, 0)
    lvl = level_for(pulls)

    if lvl == 0:
        action = full_commands or move_text
        note = "(first times - full commands)"
    elif lvl == 1:
        action = (full_commands or move_text)
        action += "\n   (you've done this before - fill the blanks yourself)"
        note = "(guided)"
    elif lvl == 2:
        action = move_text + "  -  you know the commands. Do it."
        note = "(light - no commands)"
    else:  # 3
        action = None
        note = "OWNED - the system no longer scaffolds this. It assumes you've got it."

    return {
        "kind": kind,
        "pulls": pulls,
        "level": lvl,
        "level_name": LEVELS[lvl],
        "action": action,
        "note": note,
    }


def report():
    counts = load_pulls()
    line = "=" * 64
    print(f"\n{line}\nABSORPTION REPORT  -  how much you've taken back\n{line}\n")
    if not counts:
        print("No pulled outcomes recorded yet. Everything starts at level 0 (full")
        print("hand-holding). Record real moves with `state_layer.py outcome` and this")
        print("fills in - the system fades as you grow.\n")
        return
    for kind in sorted(KIND_PATTERNS) + ["other"]:
        if kind in counts:
            n = counts[kind]
            lvl = level_for(n)
            print(f"  {kind:<16} pulled {n}x  ->  level {lvl}: {LEVELS[lvl]}")
    print()
    owned = [k for k, n in counts.items() if level_for(n) == 3]
    if owned:
        print("These are YOURS now (system will stop carrying them): " + ", ".join(owned))
        print()


def main():
    ap = argparse.ArgumentParser(description="AgentOS Stage 6 - the Transfer Schedule")
    ap.add_argument("move", nargs="?", help="a move to scaffold to your current level")
    ap.add_argument("--report", action="store_true", help="absorption across all move-kinds")
    args = ap.parse_args()

    if args.report or not args.move:
        report()
        return

    s = scaffold(args.move)
    line = "=" * 64
    print(f"\n{line}\nTRANSFER  -  '{args.move}'\n{line}")
    print(f"\nmove-kind:  {s['kind']}")
    print(f"you've pulled this kind:  {s['pulls']}x")
    print(f"scaffolding level:  {s['level']} - {s['level_name']}")
    if s["action"]:
        print(f"\nACTION (scaffolded to you):\n  {s['action']}")
    else:
        print(f"\n{s['note']}")
    print()


if __name__ == "__main__":
    main()
