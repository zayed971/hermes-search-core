#!/usr/bin/env python3
"""
AgentOS - Stage 9: The Action Gate

Lets the agent ACT in the world - at full power where action is safe, and with an
honest human tap where it isn't. This is the considered answer to "give it full
permissions": not one blanket setting, but tiers matched to reversibility.

THREE TIERS:

  AUTO (notify-and-delay): the agent does it itself. It notifies you and waits a
    delay window for a STOP; absent STOP, it executes. ONLY for actions that are
    reversible and low-stakes:
      - push to YOUR OWN repos (you can force-push back, delete, make private)
      - install repos/skills INTO A SANDBOX (isolated venv; uninstall = delete it)
    The 10-minute STOP is genuinely sufficient here, because a bad outcome is
    undoable.

  TAP (one-tap send queue): the agent prepares the action to 100% - the full
    message, the exact recipient - and places it in a send queue. You execute with
    one tap. For IRREVERSIBLE, relationship- or reputation-bearing actions:
      - messages to people (WhatsApp/Telegram/email)
      - public posts
    Why not AUTO: a sent message can't be unsent, the board's model of a human
    relationship is the least reliable input in the system, and the STOP window is
    empty on exactly the nights autonomy runs (you're asleep). The agent removes
    100% of the drafting effort; you keep the 2-second act of exposure - which is
    also the act that builds the muscle the transfer schedule exists to grow.

  FORBIDDEN: never, by any path -
      - financial transactions / transfers / payments
      - deleting data outside the sandbox
      - credential entry, permission/sharing changes
    These are not gated; they are absent.

This module decides the tier for a proposed action and routes it. It does not
itself send messages or move money - it stages, queues, or (for AUTO) shells the
reversible command after the delay. Notification transport (Telegram) is pluggable;
without it wired, AUTO degrades to TAP (it queues instead of acting) - failing safe.
"""

import argparse
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

import state_layer as sl

SEND_QUEUE = sl.STATE_DIR / "send_queue.md"
ACTION_LOG = sl.STATE_DIR / "action_log.md"
STOP_FLAG = sl.STATE_DIR / "STOP"          # create this file to abort a pending AUTO action

DEFAULT_DELAY_SECONDS = 600                 # 10 minutes


# --- tier classification -----------------------------------------------------
FORBIDDEN_TOKENS = ["pay", "transfer", "withdraw", "deposit", "buy ", "sell ",
                    "credit card", "bank", "password", "api key", "token",
                    "rm -rf", "del /f", "permission", "sharing", "make public"]

AUTO_TOKENS = ["git push", "push to", "commit and push", "pip install", "npm install",
               "clone", "install skill", "install repo"]

TAP_TOKENS = ["send", "message", "whatsapp", "telegram", "email", "reply",
              "dm ", "post ", "publish", "tweet", "apply", "submit"]


def classify(action_text):
    t = (action_text or "").lower()
    if any(tok in t for tok in FORBIDDEN_TOKENS):
        return "FORBIDDEN"
    # TAP wins over AUTO when both appear (e.g. "push then message" -> the message gates)
    if any(tok in t for tok in TAP_TOKENS):
        return "TAP"
    # AUTO: a git push to a repo, or a clone/sandbox install. Match on the key
    # verbs even when words intervene ("push the cleaned X to my repo").
    is_push = "push" in t and ("repo" in t or "github" in t or "origin" in t or "branch" in t)
    is_install = any(tok in t for tok in ["pip install", "npm install", "clone",
                                          "install skill", "install repo"])
    if is_push:
        return "AUTO"
    if is_install:
        # sandbox installs are AUTO; anything not explicitly sandboxed needs your eyes
        if "sandbox" in t or "venv" in t:
            return "AUTO"
        return "TAP"
    return "TAP"            # default to the safe tier for anything unrecognized


# --- notification (pluggable) ------------------------------------------------
def notify(message, notifier=None):
    """Send a notification. notifier is a callable(str)->bool. None => no transport."""
    if notifier is None:
        return False
    try:
        return bool(notifier(message))
    except Exception:
        return False


def log(entry):
    with open(ACTION_LOG, "a", encoding="utf-8") as f:
        f.write(f"\n## {datetime.now().isoformat(timespec='seconds')}\n{entry}\n")


# --- TAP: queue for one-tap human send --------------------------------------
def queue_for_tap(action_text, payload, recipient=""):
    block = (
        f"\n---\n### QUEUED {datetime.now().isoformat(timespec='seconds')}\n"
        f"**action:** {action_text}\n"
        f"**to:** {recipient or '(set recipient)'}\n"
        f"**ready to send (one tap - this is yours):**\n\n{payload}\n"
    )
    with open(SEND_QUEUE, "a", encoding="utf-8") as f:
        f.write(block)
    print(f"TAP   queued for your one-tap send -> {SEND_QUEUE}")
    print("      The agent prepared it fully. The send is yours (2 seconds).")
    log(f"[TAP] queued: {action_text} -> {recipient}")


# --- AUTO: notify-and-delay, then execute reversible command -----------------
def auto_with_delay(action_text, command, notifier=None, delay=DEFAULT_DELAY_SECONDS,
                    dry_run=False):
    # clear any stale STOP
    if STOP_FLAG.exists():
        STOP_FLAG.unlink()

    msg = (f"AgentOS will run in {delay // 60} min (reversible):\n  {action_text}\n"
           f"Command: {command}\n"
           f"To abort: create a file named STOP in {sl.STATE_DIR}")
    delivered = notify(msg, notifier)
    print(f"AUTO  {action_text}")
    print(f"      notify delivered: {delivered}")

    if not delivered:
        # FAIL SAFE: no way to warn you => downgrade to TAP, do not act unattended.
        print("      no notification transport -> downgrading to TAP (queuing, not acting).")
        queue_for_tap(action_text, f"(was AUTO) run manually:\n{command}")
        return "DOWNGRADED_TO_TAP"

    print(f"      waiting {delay}s for STOP...")
    if not dry_run:
        waited = 0
        while waited < delay:
            if STOP_FLAG.exists():
                STOP_FLAG.unlink()
                print("      STOP received - aborted.")
                log(f"[AUTO-ABORTED] {action_text}")
                return "ABORTED"
            time.sleep(min(5, delay - waited))
            waited += 5

    if dry_run:
        print(f"      [dry-run] would execute: {command}")
        log(f"[AUTO-DRYRUN] {action_text}: {command}")
        return "DRYRUN"

    # execute the reversible command
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=120)
        ok = result.returncode == 0
        print(f"      executed (ok={ok}).")
        log(f"[AUTO-EXECUTED ok={ok}] {action_text}\n  cmd: {command}\n  out: {result.stdout[:300]}")
        return "EXECUTED" if ok else "FAILED"
    except Exception as e:
        print(f"      execution error: {e}")
        log(f"[AUTO-ERROR] {action_text}: {e}")
        return "ERROR"


# --- the gate ----------------------------------------------------------------
def gate(action_text, command=None, payload=None, recipient="", notifier=None,
         delay=DEFAULT_DELAY_SECONDS, dry_run=False):
    tier = classify(action_text)
    print(f"\nACTION GATE  -  '{action_text}'\n  tier: {tier}")

    if tier == "FORBIDDEN":
        print("  REFUSED. This class of action is absent from the system, not gated.")
        print("  (financial moves, out-of-sandbox deletion, credentials/permissions)")
        log(f"[FORBIDDEN] refused: {action_text}")
        return "REFUSED"

    if tier == "TAP":
        queue_for_tap(action_text, payload or "(no payload prepared)", recipient)
        return "QUEUED"

    # AUTO
    if not command:
        print("  AUTO action has no command to run; nothing to do.")
        return "NOOP"

    # GOVERNOR: every autonomous action must pass the Questioner first.
    # Only a GREENLIGHT proceeds; anything else is held for the human.
    try:
        import governor
        gov = governor.decide(action_text)
    except Exception as e:
        gov = {"decision": "HOLD", "do_instead": f"governor unavailable ({e})", "verdict": None}
    if gov["decision"] != "PROCEED":
        print(f"  GOVERNOR: {gov['decision']} - not auto-executing.")
        reason = gov.get("reason") or ""
        do_instead = gov.get("do_instead") or ""
        queue_for_tap(
            action_text,
            f"Governor verdict: {gov['decision']}.\n"
            f"{reason}\n"
            f"Suggested instead: {do_instead}\n"
            f"Original command (run manually if you still want it): {command}",
            recipient="you (governor held this)",
        )
        return f"HELD_BY_GOVERNOR:{gov['decision']}"

    return auto_with_delay(action_text, command, notifier=notifier, delay=delay, dry_run=dry_run)


def show_queue():
    print(SEND_QUEUE.read_text(encoding="utf-8") if SEND_QUEUE.exists()
          else "Send queue empty.")


def main():
    ap = argparse.ArgumentParser(description="AgentOS Stage 9 - the Action Gate")
    ap.add_argument("action", nargs="?", help="the action to route")
    ap.add_argument("--command", help="for AUTO: the reversible shell command")
    ap.add_argument("--payload", help="for TAP: the prepared message text")
    ap.add_argument("--to", default="", help="recipient (for TAP)")
    ap.add_argument("--delay", type=int, default=DEFAULT_DELAY_SECONDS)
    ap.add_argument("--dry-run", action="store_true", help="AUTO: don't actually execute")
    ap.add_argument("--show-queue", action="store_true")
    args = ap.parse_args()

    if args.show_queue or not args.action:
        show_queue()
        return
    gate(args.action, command=args.command, payload=args.payload,
         recipient=args.to, delay=args.delay, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
