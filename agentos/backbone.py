#!/usr/bin/env python3
"""
AgentOS - Stage 5b: The Backbone (unattended runner)

This is what runs while your laptop is shut or you're studying - the thing that
turns "prepaid capacity expires unused" into "the loop ran on its own." It does
NOT add new intelligence. It wraps the existing loop in three things an
unattended run needs:

  1. A guardrail check BEFORE doing anything (Stage 5a): RUN / RUN_REDUCED / HOLD.
  2. A run log, so you can read every unattended cycle in the morning.
  3. P3-B (single-executor reconciliation): the build path itself is disabled.
     Hermes's "AgentOS Nightly" cron is the sole executor now - this used to
     run a second, competing loop cycle the same hour. The dead-man's-switch
     guardrail check still runs and still writes a re-entry note on HOLD;
     it just never builds anything anymore.

Run it once by hand to test:
  python backbone.py

Register it to run on its own:
  python backbone.py --install-cron "0 23 * * *"     # Linux/Mac, 11pm daily
  python backbone.py --print-task-scheduler          # Windows: prints the schtasks command

It is deliberately a thin wrapper. The judgment lives in the Questioner; the
agenda in the loop; the safety in the guardrail. This just lets them run while
you're gone, and writes down what happened.
"""

import argparse
import io
import sys
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

import state_layer as sl
import guardrail
import loop as loop_mod

RUN_LOG = sl.STATE_DIR / "backbone_runlog.md"


def log(text):
    stamp = datetime.now().isoformat(timespec="seconds")
    entry = f"\n\n## Run {stamp}\n{text.strip()}\n"
    with open(RUN_LOG, "a", encoding="utf-8") as f:
        f.write(entry)


def run_once():
    board = loop_mod.sense()
    if not board:
        print("No ground_state.json - set up Stage 1 first.")
        return

    decision = guardrail.assess(board)
    header = f"[{decision['mode']}] {decision['message']}"
    print(header)

    if decision["mode"] == "HOLD":
        # Dead-man's-switch tripped: do not build. Surface the human re-entry move.
        card = (
            "\nTHE ONE MOVE (re-entry):\n"
            f"  {decision['message']}\n"
        )
        print(card)
        sl.STATE_DIR.joinpath("trigger_card.md").write_text(
            f"# RE-ENTRY  -  {datetime.now().date()}\n\n{decision['message']}\n",
            encoding="utf-8",
        )
        log(header + "\n" + card + "\n(no build - dead-man's-switch active)")
        return

    # RUN or RUN_REDUCED: P3-B - the build path is disabled (single-executor
    # reconciliation; Hermes's "AgentOS Nightly" cron is the sole executor
    # now). loop_mod.run() still does SENSE/RECORD (read-only visibility)
    # but no longer generates candidates or writes a trigger card.
    #
    # reporter.report() no longer fires from here either. This ran at 23:00,
    # BEFORE the night-shift (Hermes's "AgentOS Nightly" cron, also 23:00) had
    # even executed - the report was always missing that night's actual
    # results. Moved to a Hermes no_agent cron ("Morning Report", 06:30
    # Cairo), well after the night-shift window closes.
    buf = io.StringIO()
    with redirect_stdout(buf):
        loop_mod.run(quiet=False)
    cycle_output = buf.getvalue()
    print(cycle_output)

    log(header + "\n" + cycle_output + "\n(no build cycle - P3-B build path disabled)")


def install_cron(schedule):
    """Append a cron line that runs this backbone on schedule (Linux/Mac)."""
    py = sys.executable
    script = str(Path(__file__).resolve())
    line = f"{schedule} cd {Path(__file__).resolve().parent} && {py} {script} >> {RUN_LOG} 2>&1\n"
    print("Add this line to your crontab (run: crontab -e):\n")
    print("  " + line)
    print("Note: cron only fires while the machine is ON. For laptop-shut runs you "
          "need a wake timer (Task Scheduler --wake on Windows) or a cheap always-on VPS.")


def print_task_scheduler():
    """Print the Windows schtasks command for an 11pm daily wake-and-run."""
    script = str(Path(__file__).resolve())
    folder = str(Path(__file__).resolve().parent)
    bat = folder + r"\run_backbone.bat"
    print("1) Create run_backbone.bat with these two lines:\n")
    print(f"   cd /d {folder}")
    print(f"   python \"{script}\"\n")
    print("2) Register it to run at 11pm daily and WAKE the machine:\n")
    print(f'   schtasks /create /tn "AgentOS Backbone" /tr "{bat}" /sc daily /st 23:00 /f')
    print("\n3) In Task Scheduler GUI, open the task -> Conditions -> check "
          "'Wake the computer to run this task'. That's the laptop-shut piece.")


def main():
    ap = argparse.ArgumentParser(description="AgentOS Stage 5 - the Backbone")
    ap.add_argument("--install-cron", metavar="SCHEDULE",
                    help='print a crontab line, e.g. "0 23 * * *"')
    ap.add_argument("--print-task-scheduler", action="store_true",
                    help="print the Windows schtasks setup")
    args = ap.parse_args()

    if args.install_cron:
        install_cron(args.install_cron)
    elif args.print_task_scheduler:
        print_task_scheduler()
    else:
        run_once()


if __name__ == "__main__":
    main()
