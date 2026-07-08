#!/usr/bin/env python3
"""
AgentOS MCP Server

Exposes the AgentOS brain as a stdio MCP server for Hermes to call.

Tools:
  questioner_judge      - GREENLIGHT / DOWNGRADE / REFUSE a candidate action
  north_star_challenge  - challenge the current goal (weekly challenger)
  get_board             - return the live board as JSON
  record_outcome        - record whether a trigger was pulled and ground moved
  agentos_ask           - query the world-model corpus
  agentos_next_move     - run one full loop cycle
  agentos_report        - write back to the board what Hermes just did
  agentos_governor_check - gate check before any autonomous action

Usage: python mcp_server.py
Register via hermes_mcp_config.json.
"""

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

# Guarantee imports resolve even when the server is launched from a different CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402

# MCP launchers (Hermes, Claude Desktop) don't inherit a shell env, so the
# key has to be loaded from .env here rather than assumed to be set already.
load_dotenv(Path(__file__).resolve().parent / ".env")

import questioner            # noqa: E402
import north_star            # noqa: E402
import state_layer as sl     # noqa: E402
import loop                  # noqa: E402
import hermes_feedback as hf # noqa: E402
import governor              # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("agentos-brain")


@mcp.tool()
def questioner_judge(action_text: str) -> str:
    """
    Judge whether a candidate action should happen.

    Runs the Questioner's four-step procedure (derive goal → cheapest path →
    test candidate → demand falsifier) and returns a verdict.

    Returns JSON with keys:
      verdict           GREENLIGHT | DOWNGRADE | REFUSE
      reasoning         plain-English explanation
      do_instead        alternative move (non-null on DOWNGRADE / REFUSE)
      derived_goal      the actual goal the Questioner derived
      cheapest_path     the lowest-cost route to that goal right now
      candidate_on_path whether the action is on that path
      falsifier         what would have to be true for the action to win
      falsifier_holds   whether the falsifier actually holds
    """
    try:
        verdict, err = questioner.judge(action_text)
    except Exception as e:
        return json.dumps({"error": str(e)})

    if verdict is None:
        return json.dumps({"error": err or "no verdict returned - model may not be configured"})

    return json.dumps({
        "verdict": verdict.get("verdict"),
        "reasoning": verdict.get("reasoning"),
        "do_instead": verdict.get("do_instead"),
        "derived_goal": verdict.get("derived_goal"),
        "cheapest_path": verdict.get("cheapest_path"),
        "candidate_on_path": verdict.get("candidate_on_path"),
        "falsifier": verdict.get("falsifier"),
        "falsifier_holds": verdict.get("falsifier_holds"),
    })


@mcp.tool()
def north_star_challenge(market_notes: str = "") -> str:
    """
    Run the North Star Challenger against the current goal.

    Produces a bidirectional challenge (aim higher / aim lower) grounded in the
    world model and track record. Never changes the goal - outputs a document
    for Zayed to accept or reject.

    Args:
        market_notes: Optional market context to ground the analysis.

    Returns the full challenge report as Markdown, or a status message if no
    model is configured (in which case a prompt file is written for pasting).
    """
    prior_mtime = (
        north_star.CHALLENGE_PATH.stat().st_mtime
        if north_star.CHALLENGE_PATH.exists()
        else None
    )

    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            north_star.run(market=market_notes, force=True)
    except Exception as e:
        return json.dumps({"error": str(e)})

    # Return the freshly written challenge if one was produced this run.
    if north_star.CHALLENGE_PATH.exists():
        new_mtime = north_star.CHALLENGE_PATH.stat().st_mtime
        if prior_mtime is None or new_mtime > prior_mtime:
            return north_star.CHALLENGE_PATH.read_text(encoding="utf-8")

    stdout_output = buf.getvalue().strip()
    return stdout_output or "(no challenge produced - check that AGENTOS_PROVIDER and AGENTOS_MODEL are set)"


@mcp.tool()
def get_board() -> str:
    """
    Return the current live board (ground_state.json) as JSON.

    Includes: goal / north star, state color (capacity), deadlines, shipped
    artifacts, pending items, owed actions, last outcome, and resources.
    """
    try:
        state = sl.load_state()
        return json.dumps(state, indent=2)
    except SystemExit:
        return json.dumps({
            "error": "No ground_state.json found. Run `python state_layer.py` to set up Stage 1."
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def record_outcome(trigger: str, pulled: bool, moved: bool, notes: str = "") -> str:
    """
    Record what happened after a trigger was issued.

    This is the primary learning signal for the whole AgentOS system. Call it
    after completing (or consciously skipping) an action.

    Args:
        trigger: The trigger text that was issued.
        pulled:  True if you actually did the action.
        moved:   True if doing it moved the ground (produced a real-world effect).
        notes:   Optional free-text note about what happened.

    Returns a confirmation JSON.
    """
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            sl.cmd_outcome(trigger, pulled, moved, notes=notes or None)
        return json.dumps({
            "status": "recorded",
            "trigger": trigger,
            "pulled": pulled,
            "moved": moved,
            "notes": notes or None,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def agentos_ask(question: str) -> str:
    """
    Query the AgentOS world-model corpus for context relevant to a question.

    Runs a keyword search across all indexed dossiers/profile files and returns
    the top matching excerpts (same output as `python state_layer.py ask`).

    Args:
        question: A natural-language question about Zayed, his goals, or context.

    Returns the ranked corpus excerpts as plain text.
    """
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            sl.ask(question)
    except Exception as e:
        return json.dumps({"error": str(e)})
    return buf.getvalue().strip() or "(no matches found in corpus)"


@mcp.tool()
def agentos_next_move() -> str:
    """
    Run one full AgentOS loop cycle and return the trigger card.

    Executes sense → record → build_candidates → pick → prepare → hand_off,
    then reads and returns the freshly written trigger card from state/.

    Returns the trigger card Markdown text, or an error message if the loop
    cannot run (e.g. ground_state.json missing or no model configured).
    """
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            loop.run()
    except Exception as e:
        return json.dumps({"error": str(e)})

    card_path = sl.STATE_DIR / "trigger_card.md"
    if card_path.exists():
        return card_path.read_text(encoding="utf-8")

    stdout_output = buf.getvalue().strip()
    return stdout_output or "(no trigger card produced - check that AGENTOS_PROVIDER and AGENTOS_MODEL are set)"


@mcp.tool()
def agentos_report(
    action: str,
    status: str,
    url: str = "",
    notes: str = "",
) -> str:
    """
    Report back to the AgentOS board what Hermes just did.

    Call this after completing any action so ground_state.json stays current.
    The board is the learning signal — stale board = broken loop.

    Args:
        action: What was done (descriptive text, or a pending-item index as a
                string when status='done').
        status: One of:
                  'shipped'  — something real entered the world (artifact, deploy,
                               send). Optionally supply url.
                  'outcome'  — a move was attempted; marks it pulled + moved.
                               Optionally supply notes.
                  'done'     — a pending item is resolved; action must be its
                               integer index (as a string, e.g. "0").
        url:    Public URL for a shipped artifact (only used when status='shipped').
        notes:  Free-text annotation (used for 'outcome'; ignored otherwise).

    Returns a confirmation JSON string.
    """
    try:
        if status == "shipped":
            result = hf.report_shipped(action, url=url or None)
        elif status == "outcome":
            result = hf.report_outcome(action, pulled=True, moved=True,
                                       notes=notes or None)
        elif status == "done":
            try:
                idx = int(action)
            except ValueError:
                return json.dumps({"error": "status='done' requires action to be an integer index"})
            result = hf.report_done(idx)
        else:
            return json.dumps({"error": f"unknown status '{status}'. Use: shipped | outcome | done"})
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def agentos_governor_check(action_text: str) -> str:
    """
    Check with the Governor before taking any autonomous action.

    Runs the full Questioner gate (derive goal → cheapest path → test candidate
    → demand falsifier). Only a PROCEED response means the action is safe to
    execute autonomously. Any other response means stop and surface to the human.

    Call this BEFORE doing anything significant: sending messages, pushing code,
    making external API calls, or spending resources.

    Args:
        action_text: Plain-English description of what you are about to do.

    Returns a plain-text verdict:
      PROCEED   — Questioner GREENLIT. Safe to execute.
      DOWNGRADE — A better/cheaper move exists. Do that instead.
      BLOCK     — Questioner REFUSED. Do not execute.
      HOLD      — No model wired; cannot judge autonomously. Escalate to human.
    """
    try:
        d = governor.decide(action_text)
    except Exception as e:
        return f"HOLD — governor error: {e}"

    decision = d.get("decision", "HOLD")
    reason = d.get("reason", "")
    do_instead = d.get("do_instead", "")

    lines = [decision]
    if reason:
        lines.append(f"Reason: {reason}")
    if do_instead and decision != "PROCEED":
        lines.append(f"Do instead: {do_instead}")
    return "\n".join(lines)


if __name__ == "__main__":
    print("AgentOS MCP server ready (stdio transport).", file=sys.stderr)
    mcp.run()
