#!/usr/bin/env python3
"""
AgentOS - Stage 3: The Loop

This is where Stage 1 (state) and Stage 2 (the Questioner) stop being two tools
you invoke by hand and become one process. One run = one cycle:

  SENSE    - read the live board (and the world model if a move needs it).
  RECORD   - did the last issued trigger get pulled? Surface it. (The learning signal.)
  QUESTION - generate candidate moves FROM STATE, then judge them. The cheapest
             path wins. The Questioner gates this - nothing gets prepared until a
             move survives it.
  PREPARE  - turn the chosen move into a trigger-ready package.
  HAND OFF - emit exactly ONE Trigger Card: the single human action for today,
             made as small and literal as possible.
  UPDATE   - record what was issued, so the next cycle can close the loop.

Candidates are NOT read from a task queue you maintain. They are generated from
the board itself - your pending items, the humans you owe, the artifacts that are
shipped but unseen - plus (when a model is wired) net-new moves the board doesn't
list yet. That difference is the whole point: this REASONS about what to do; the
old council read a list or fell back to a hardcoded default.

The loop closes for $0 with no model: it picks via a transparent cheapest-path
PROXY and emits a real trigger card. Wire a model (see questioner.py config) and
the Questioner replaces the proxy, judges the pick, and drafts any message.

Usage:
  python loop.py            # run one cycle
  python loop.py --quiet    # just print the trigger card
"""

import argparse
import json
import os
from datetime import date, datetime, timedelta

import state_layer as sl
import questioner as Q


# --- model availability ------------------------------------------------------
def model_available():
    provider = os.environ.get("AGENTOS_PROVIDER", "").lower()
    model = os.environ.get("AGENTOS_MODEL", "").strip()
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    return bool(provider in {"gemini", "anthropic"} and model and key)


def judge(action, top_k=4):
    """Reuse Stage 2 to get a verdict dict (or None if no model / failure)."""
    user = Q.build_user_message(action, top_k=top_k)
    text, err = Q.call_model(Q.SYSTEM_PROMPT, user)
    if text is None:
        return None, err
    return Q.parse_verdict(text), None


# --- SENSE -------------------------------------------------------------------
def sense():
    return Q.load_board()


def load_recent_moves(days=7):
    """Return a set of move strings issued in the last N days from backbone_runlog.md."""
    log_path = sl.STATE_DIR / "backbone_runlog.md"
    if not log_path.exists():
        return set()
    try:
        text = log_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return set()
    cutoff = date.today() - timedelta(days=days)
    moves = set()
    for section in text.split("\n## Run ")[1:]:
        lines = section.splitlines()
        if not lines:
            continue
        try:
            run_date = date.fromisoformat(lines[0].split("T")[0])
        except (ValueError, IndexError):
            continue
        if run_date < cutoff:
            continue
        for line in lines:
            if line.startswith("THE MOVE:"):
                move = line[len("THE MOVE:"):].strip()
                if move:
                    moves.add(move.lower())
    return moves


# --- candidate generation (FROM STATE, not a queue) --------------------------
def build_candidates(board):
    """Generate candidate moves from the board's own contents."""
    candidates = []

    # 1. Shipped-but-unseen artifacts -> get them in front of a human.
    for item in board.get("shipped", []):
        if not (item.get("seen_by")):
            candidates.append({
                "kind": "land_shipped",
                "move": f"Get '{item.get('what','?')}' in front of someone who matters",
                "trigger": f"Send the link ({item.get('url','?')}) to a target person/company",
                "context": item.get("url"),
                "age": None,
            })

    # 2. Humans you owe -> deliver / respond.
    for item in board.get("owed", []):
        candidates.append({
            "kind": "owed",
            "move": f"Deliver to {item.get('who','?')}",
            "trigger": item.get("what", "respond / deliver"),
            "context": item.get("who"),
            "age": sl.age_days(item.get("since")),
        })

    # 3. Pending items -> their own named trigger.
    for item in board.get("pending", []):
        candidates.append({
            "kind": "pending",
            "move": item.get("what", "?"),
            "trigger": item.get("trigger", "(no trigger set)"),
            "blocked_on": item.get("blocked_on"),
            "context": item.get("where"),
            "age": sl.age_days(item.get("created")),
        })

    # Deduplicate: exclude moves already issued in the last 7 days.
    recent = load_recent_moves(days=7)
    if recent:
        candidates = [c for c in candidates if c.get("move", "").lower() not in recent]

    return candidates


# --- cheapest-path PROXY (only used when no model is wired) ------------------
def effort_hint(text):
    t = (text or "").lower()
    if any(k in t for k in ("5-min", "5 min", "minute", "one line", "quick")):
        return "tiny"
    if any(k in t for k in ("build", "export", "pdf", "finalize", "send to one")):
        return "medium"
    if any(k in t for k in ("git", "push", "commit", "repo")):
        return "small"
    return "small"


def proxy_score(c):
    """A rough, transparent stand-in for the Questioner. NOT intelligence - a proxy.
    Rewards: a real human seeing a real artifact > making one presentable > new public
    artifact; tiny effort; age. The Questioner replaces this entirely once wired."""
    text = f"{c.get('move','')} {c.get('trigger','')} {c.get('blocked_on','')}".lower()
    score, why = 0.0, []

    if c["kind"] in ("owed", "land_shipped"):
        score += 5; why.append("+5 a real human sees a real artifact")
    if c["kind"] == "pending" and any(k in text for k in ("clean", "readme", "fix")) \
            and "http" in str(c.get("context", "")):
        score += 3; why.append("+3 makes an existing artifact presentable")
    if c["kind"] == "pending" and any(k in text for k in ("push", "github", "repo")):
        score += 2; why.append("+2 creates a new public artifact")
    if c["kind"] == "pending" and any(k in text for k in ("send", "company", "in front")):
        score += 4; why.append("+4 puts work in front of a human")

    eff = effort_hint(text)
    bonus = {"tiny": 3, "small": 1, "medium": 0}[eff]
    score += bonus
    if bonus:
        why.append(f"+{bonus} {eff} effort")

    if c.get("age"):
        a = min(c["age"], 7) * 0.3
        score += a
        why.append(f"+{a:.1f} age ({c['age']}d)")

    return score, eff, why


# --- RECORD ------------------------------------------------------------------
def record(board):
    last = board.get("last_issued_trigger")
    if not last:
        print("RECORD   first cycle - no prior trigger to check.")
        return
    outcome = board.get("last_outcome", {})
    issued_date = last.get("date")
    resolved = (
        outcome.get("did_you_pull_it")
        and outcome.get("date")
        and issued_date
        and outcome["date"] >= issued_date
    )
    if resolved:
        print(f"RECORD   last move LANDED: \"{last.get('move')}\"  (ground moved: "
              f"{'YES' if outcome.get('ground_moved') else 'NO'})")
    else:
        print(f"RECORD   last move NOT yet recorded: \"{last.get('move')}\"")
        print("         if you did it:  python state_layer.py outcome "
              f"\"{last.get('move')}\" --pulled --moved")


# --- QUESTION (pick) ---------------------------------------------------------
def pick(candidates):
    """Choose the move. Questioner if a model is wired; cheapest-path proxy otherwise."""
    if not candidates:
        return None, "no candidates - board has no open moves; add state or wire a model to propose."

    if model_available():
        # Rank by proxy first to decide judging order, then let the Questioner gate.
        ranked = sorted(candidates, key=lambda c: proxy_score(c)[0], reverse=True)
        print("QUESTION running the Questioner over candidates (model wired)...")
        for c in ranked:
            verdict, err = judge(c["move"])
            if verdict is None:
                print(f"         (judge failed: {err}) - falling back to proxy.")
                break
            v = verdict.get("verdict")
            print(f"         {v:<10} {c['move']}")
            if v == "GREENLIGHT":
                return {"chosen": c, "verdict": verdict}, None
            if v == "DOWNGRADE" and verdict.get("do_instead"):
                c2 = dict(c)
                c2["move"] = verdict["do_instead"]
                c2["trigger"] = verdict["do_instead"]
                return {"chosen": c2, "verdict": verdict}, None
            # REFUSE -> try next candidate
        # if all refused or judge failed, fall through to proxy

    # proxy path
    scored = [(proxy_score(c), c) for c in candidates]
    scored.sort(key=lambda x: (x[0][0], x[0][1] == "tiny"), reverse=True)
    print("QUESTION cheapest-path proxy (no model wired). Candidates considered:")
    for (s, eff, why), c in scored:
        print(f"         {s:5.1f}  [{eff:<6}] {c['move']}")
    top = scored[0]
    return {"chosen": top[1], "verdict": None, "proxy": top[0]}, None


# --- PREPARE -----------------------------------------------------------------
def prepare(chosen, board):
    """Turn the chosen move into a trigger-ready package (a Trigger Card)."""
    c = chosen["chosen"]
    move = c["move"]
    trigger = c.get("trigger", "")

    # WHY NOW - tie to the nearest relevant deadline / owed human.
    why_now = ""
    owed = board.get("owed", [])
    deadlines = sorted(board.get("deadlines", []), key=lambda x: x.get("date", "9999"))
    soon = next((d for d in deadlines if (sl.days_until(d.get("date")) or 99) >= 0), None)
    if owed:
        o = owed[0]
        dleft = None
        for d in deadlines:
            if o.get("who", "").split()[0].lower() in d.get("what", "").lower():
                dleft = sl.days_until(d.get("date"))
        why_now = f"{o.get('who')} is owed this" + (f" (~{dleft}d out)" if dleft is not None else "") + "."
    elif soon:
        why_now = f"{soon.get('what')} is in {sl.days_until(soon.get('date'))}d."

    # THE ACTION - literal. Append a generic git block if the move involves a push.
    action = trigger
    if any(k in (trigger or "").lower() for k in ("push", "commit", "git")):
        action = trigger + "\n\n    git add -A\n    git commit -m \"...\"\n    git push"

    time_hint = {"tiny": "~5 min", "small": "~15 min", "medium": "~30-45 min"}[
        effort_hint(f"{move} {trigger}")
    ]

    return {
        "move": move,
        "why_now": why_now or "On the path to the active objective.",
        "action": action,
        "time": time_hint,
        "verdict": chosen.get("verdict"),
    }


# --- HAND OFF + UPDATE -------------------------------------------------------
def hand_off(card, board):
    line = "=" * 64
    text = (
        f"\n{line}\nTHE ONE MOVE  -  {date.today().isoformat()}\n{line}\n\n"
        f"THE MOVE:   {card['move']}\n\n"
        f"WHY NOW:    {card['why_now']}\n\n"
        f"THE ACTION:\n    {card['action']}\n\n"
        f"TIME:       {card['time']}\n"
    )
    if card.get("verdict"):
        text += f"\n(Questioner: {card['verdict'].get('verdict')})\n"
    text += line + "\n"
    print(text)

    sl.STATE_DIR.joinpath("trigger_card.md").write_text(text, encoding="utf-8")

    # UPDATE - record what was issued so the next cycle can close the loop.
    board["last_issued_trigger"] = {"move": card["move"], "date": date.today().isoformat()}
    sl.save_state(board)


# --- run one cycle -----------------------------------------------------------
# P3-B (single-executor reconciliation, option a): AgentOS's own build path
# (candidate generation -> pick -> prepare -> hand_off) is neutered. Hermes's
# "AgentOS Nightly" cron is now the sole executor producing trigger/task
# results - this loop and backbone.py's build path were a second, competing
# executor running the same hour, producing a second, conflicting trigger
# card nobody asked for. SENSE/RECORD stay: they're read-only visibility
# (board state, whether the last issued trigger landed), not "work."
def run(quiet=False):
    board = sense()
    if not board:
        print("No ground_state.json. Set up Stage 1 first.")
        return

    if not quiet:
        print("\nLOOP  -  one cycle (build path disabled, P3-B)\n" + "-" * 64)
        print(f"SENSE    board loaded. capacity: {board.get('state_color','?').upper()}  "
              f"| shipped {len(board.get('shipped', []))}  "
              f"| pending {len(board.get('pending', []))}  "
              f"| owed {len(board.get('owed', []))}")
        record(board)
        print("\nBUILD    disabled (P3-B: single-executor reconciliation - "
              "Hermes night-shift is the sole executor). No candidates "
              "generated, no trigger card written, no board state changed.")


def main():
    ap = argparse.ArgumentParser(description="AgentOS Stage 3 - the Loop")
    ap.add_argument("--quiet", action="store_true", help="print only the trigger card")
    args = ap.parse_args()
    run(quiet=args.quiet)


if __name__ == "__main__":
    main()
