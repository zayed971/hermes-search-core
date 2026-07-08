#!/usr/bin/env python3
"""
AgentOS - Hermes Feedback (board write-back)

The problem this closes: once Hermes acts autonomously (cron jobs, sends, PRs,
research), nothing tells AgentOS's board what happened - so ground_state.json
goes stale the moment the body starts working. That's the same staleness the
write-back path solved for YOU; this solves it for HERMES.

This module is the single channel Hermes uses to report back. It wraps the
existing state_layer write functions (no new state logic) and is callable two
ways:
  - CLI, so a Hermes cron script can append a line: `python hermes_feedback.py ...`
  - importable, so the MCP bridge can expose it as a tool Hermes calls directly.

Every report also lands in a human-readable journal (state/hermes_activity.md)
so you can see, in the morning, exactly what the body did while you were gone.

Usage:
  python hermes_feedback.py shipped "RAG demo deployed" --url https://...
  python hermes_feedback.py outcome "sent Mohammed the link" --pulled --moved --notes "he replied"
  python hermes_feedback.py pending "CI is failing on PR 3" --trigger "fix the failing test"
  python hermes_feedback.py done 0
  python hermes_feedback.py note "scanned MENA AI roles; nothing new worth surfacing"
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

import state_layer as sl

ACTIVITY_LOG = sl.STATE_DIR / "hermes_activity.md"


def _journal(kind, detail):
    stamp = datetime.now().isoformat(timespec="seconds")
    with open(ACTIVITY_LOG, "a", encoding="utf-8") as f:
        f.write(f"\n- **{stamp}** [{kind}] {detail}")


def report_shipped(what, url=None, seen_by=None):
    """Hermes shipped something real into the world -> record it as shipped."""
    sl.cmd_ship(what, url=url, seen_by=seen_by)
    _journal("shipped", f"{what}" + (f" ({url})" if url else ""))
    return {"ok": True, "recorded": "shipped", "what": what}


def report_outcome(move, pulled=True, moved=True, notes=None):
    """Record the outcome of a move - the learning signal the system runs on."""
    sl.cmd_outcome(move, pulled, moved, notes=notes)
    _journal("outcome", f"{move} | pulled={pulled} moved={moved}"
             + (f" | {notes}" if notes else ""))
    return {"ok": True, "recorded": "outcome", "move": move}


def report_pending(what, trigger=None, blocked_on=None, where=None):
    """Hermes surfaced something that now needs a human hand -> add to pending."""
    sl.cmd_pending(what, trigger=trigger, blocked_on=blocked_on, where=where)
    _journal("pending", f"{what}" + (f" | trigger: {trigger}" if trigger else ""))
    return {"ok": True, "recorded": "pending", "what": what}


def report_done(index):
    """A pending item is resolved -> remove it from the board."""
    sl.cmd_resolve(index)
    _journal("done", f"resolved pending[{index}]")
    return {"ok": True, "recorded": "done", "index": index}


def note(text):
    """A freeform activity note (e.g. 'scanned X, nothing actionable'). No board change."""
    _journal("note", text)
    return {"ok": True, "recorded": "note"}


def report(blob):
    """
    Single structured entry point for the MCP bridge. Hermes hands a dict like:
      {"kind": "outcome", "move": "...", "pulled": true, "moved": true, "notes": "..."}
      {"kind": "shipped", "what": "...", "url": "..."}
      {"kind": "pending", "what": "...", "trigger": "..."}
      {"kind": "done", "index": 0}
      {"kind": "note", "text": "..."}
    """
    if isinstance(blob, str):
        blob = json.loads(blob)
    kind = blob.get("kind")
    if kind == "shipped":
        return report_shipped(blob.get("what", "?"), blob.get("url"), blob.get("seen_by"))
    if kind == "outcome":
        return report_outcome(blob.get("move", "?"), blob.get("pulled", True),
                              blob.get("moved", True), blob.get("notes"))
    if kind == "pending":
        return report_pending(blob.get("what", "?"), blob.get("trigger"),
                              blob.get("blocked_on"), blob.get("where"))
    if kind == "done":
        return report_done(int(blob.get("index", 0)))
    if kind == "note":
        return note(blob.get("text", ""))
    return {"ok": False, "error": f"unknown kind: {kind}"}


def main():
    ap = argparse.ArgumentParser(description="AgentOS - Hermes feedback into the board")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("shipped"); s.add_argument("what"); s.add_argument("--url"); s.add_argument("--seen-by")
    o = sub.add_parser("outcome"); o.add_argument("move"); o.add_argument("--pulled", action="store_true")
    o.add_argument("--moved", action="store_true"); o.add_argument("--notes")
    p = sub.add_parser("pending"); p.add_argument("what"); p.add_argument("--trigger"); p.add_argument("--blocked-on"); p.add_argument("--where")
    d = sub.add_parser("done"); d.add_argument("index", type=int)
    n = sub.add_parser("note"); n.add_argument("text")
    a = ap.parse_args()

    if a.cmd == "shipped":
        print(json.dumps(report_shipped(a.what, a.url, a.seen_by)))
    elif a.cmd == "outcome":
        print(json.dumps(report_outcome(a.move, a.pulled, a.moved, a.notes)))
    elif a.cmd == "pending":
        print(json.dumps(report_pending(a.what, a.trigger, a.blocked_on, a.where)))
    elif a.cmd == "done":
        print(json.dumps(report_done(a.index)))
    elif a.cmd == "note":
        print(json.dumps(note(a.text)))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
