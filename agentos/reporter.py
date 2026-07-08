#!/usr/bin/env python3
"""
AgentOS - Stage 11: The Dual-Sink Reporter

Decision #9 (Hermes HQ work orders, P1-D): Obsidian is a logging *destination*,
not a system to maintain. This module takes the job-result records P1-B
(verifier.py) already writes to state/job_results.jsonl and pushes them out
two sinks, every run:

  1. Telegram - one terse ping (counts + which jobs failed, if any).
  2. Obsidian - a readable, dated status note with a full table: every job
     that ran, what it claimed, and the verifier's verdict.

Neither sink is a new source of truth. job_results.jsonl (P1-B) is. This
module only formats and pushes what's already there - it does not re-judge
anything, so a write failure here can never hide a FAILED verdict.

Credentials: the Telegram bot token + home channel are read live from the
existing Hermes .env - not copied into a second file. One key, one place
(per P0-B).

GATE 1d scope: this reports on P1-B's records directly (that's the artifact
P1-D consumes per the work order). Cross-checking those verdicts against a
full status-truth scan is P1-C's job once it exists; until then "matches
status-truth" reduces to "matches job_results.jsonl", which this module's
own table is built from line for line.

Runs from either side: this gets imported by Windows-side backbone.py AND
by WSL-side cron scripts (budget_guard.py). A hardcoded Windows path like
C:\... is NOT absolute under POSIX - pathlib treats the whole backslash
string as one literal filename and silently writes garbage relative to
whatever the caller's cwd happens to be. Resolve both paths per-OS instead.

Commands:
  python reporter.py --demo            Report today's job_results.jsonl to both sinks.
  python reporter.py --day 2026-06-18  Report a specific day instead of today.
  python reporter.py --no-telegram     Write the Obsidian note only, skip the ping.
"""

import argparse
import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import state_layer as sl

JOB_RESULTS_LOG = sl.STATE_DIR / "job_results.jsonl"

# P3-F: "today"/"day" is always Cairo-local, computed explicitly - not
# whatever the host OS happens to be set to. job_results.jsonl timestamps
# (verifier.py's _stamp(), via datetime.now() with no tz) are naive
# local-clock times; this only lines up with Cairo day boundaries because
# both the Windows host and the WSL box are themselves configured to
# Africa/Cairo. Computing the day explicitly here means a report run from
# either side agrees with budget_guard.py's Cairo-local day too, instead of
# three different "now"s (Windows local, WSL local, UTC) drifting apart at
# day boundaries - which is exactly the gap that let a cross-midnight night
# split across two day-files.
CAIRO_TZ = ZoneInfo("Africa/Cairo")

if os.name == "nt":
    OBSIDIAN_STATUS_DIR = Path(r"C:\Users\HP\Documents\Obsidian Vault\99-SYSTEM\Hermes-Status")
    HERMES_ENV = Path(r"\\wsl.localhost\Ubuntu\home\hp\.hermes\.env")
else:
    OBSIDIAN_STATUS_DIR = Path("/mnt/c/Users/HP/Documents/Obsidian Vault/99-SYSTEM/Hermes-Status")
    HERMES_ENV = Path.home() / ".hermes" / ".env"


# --- read the one copy of the Telegram credentials (Hermes' .env) ------------
def _read_hermes_env_var(name):
    if not HERMES_ENV.exists():
        return None
    for line in HERMES_ENV.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == name:
            return v.strip().strip('"').strip("'")
    return None


# --- load P1-B's records ------------------------------------------------------
def load_records(day):
    """Records from job_results.jsonl whose timestamp falls on `day` (YYYY-MM-DD)."""
    if not JOB_RESULTS_LOG.exists():
        return []
    records = []
    for line in JOB_RESULTS_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("ts", "").startswith(day):
            records.append(rec)
    return records


# --- sink 1: Telegram (terse) -------------------------------------------------
def _send_telegram(text):
    token = _read_hermes_env_var("TELEGRAM_BOT_TOKEN")
    chat_id = _read_hermes_env_var("TELEGRAM_HOME_CHANNEL")
    if not token or not chat_id:
        return False, "no TELEGRAM_BOT_TOKEN/TELEGRAM_HOME_CHANNEL in Hermes .env"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200, f"http {resp.status}"
    except urllib.error.URLError as e:
        return False, str(e)


def _telegram_text(records, note_path):
    total = len(records)
    failed = [r for r in records if r.get("status") != "done"]
    if total == 0:
        return f"Hermes status {note_path.stem}: no jobs ran."
    if failed:
        names = ", ".join(r["job"] for r in failed)
        return f"Hermes status {note_path.stem}: {len(failed)}/{total} FAILED ({names}). See {note_path.name}."
    return f"Hermes status {note_path.stem}: {total}/{total} ok. See {note_path.name}."


# --- sink 2: Obsidian (readable, dated) ---------------------------------------
def _render_note(records, day):
    lines = [
        f"# Hermes Status — {day}",
        "",
        f"Generated {datetime.now(CAIRO_TZ).isoformat(timespec='seconds')} by P1-D (dual-sink reporter).",
        "Source: `AgentOS/state/job_results.jsonl` (P1-B). This file is a destination, not "
        "edited by hand.",
        "",
        "| Time | Job | Claim | Verdict | Evidence |",
        "|---|---|---|---|---|",
    ]
    for r in records:
        ts = r.get("ts", "")
        time_part = ts.split("T")[1] if "T" in ts else ts
        job = r.get("job", "?")
        claim = (r.get("claim") or "").replace("|", "\\|")
        verdict = "OK" if r.get("status") == "done" else "FAILED"
        vr = r.get("verify_result", {})
        evidence = (vr.get("evidence") or vr.get("reason") or "").replace("|", "\\|")
        lines.append(f"| {time_part} | {job} | {claim} | {verdict} | {evidence} |")

    total = len(records)
    failed = sum(1 for r in records if r.get("status") != "done")
    if total:
        lines += ["", f"**Summary:** {total - failed}/{total} ok, {failed} failed."]
    else:
        lines += ["", "**Summary:** no jobs ran on this day."]
    return "\n".join(lines) + "\n"


def write_obsidian_note(records, day):
    OBSIDIAN_STATUS_DIR.mkdir(parents=True, exist_ok=True)
    note_path = OBSIDIAN_STATUS_DIR / f"{day}.md"
    note_path.write_text(_render_note(records, day), encoding="utf-8")
    return note_path


# --- the dual sink -------------------------------------------------------------
def report(day=None, send_telegram=True):
    day = day or datetime.now(CAIRO_TZ).date().isoformat()
    records = load_records(day)
    note_path = write_obsidian_note(records, day)

    if send_telegram:
        ok, detail = _send_telegram(_telegram_text(records, note_path))
    else:
        ok, detail = None, "skipped (--no-telegram)"

    print(f"Obsidian note: {note_path}")
    print(f"Telegram ping: {'sent' if ok else ('skipped' if ok is None else 'FAILED')} ({detail})")
    return note_path, ok


def main():
    ap = argparse.ArgumentParser(description="AgentOS Stage 11 - the Dual-Sink Reporter")
    ap.add_argument("--demo", action="store_true", help="report today's job_results.jsonl to both sinks")
    ap.add_argument("--day", help="report a specific day (YYYY-MM-DD) instead of today")
    ap.add_argument("--no-telegram", action="store_true", help="write the Obsidian note only")
    args = ap.parse_args()

    if args.demo or args.day or args.no_telegram:
        report(day=args.day, send_telegram=not args.no_telegram)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
