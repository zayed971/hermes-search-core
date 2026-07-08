#!/usr/bin/env python3
"""
AgentOS - Stage 2: The Questioner

The senior function. It does NOT execute. Given a CANDIDATE ACTION, it judges
whether that action should happen at all, by running one fixed four-step
procedure over the world model (Stage 1 corpus) and the live board (Stage 1
ground state):

  1. Derive the goal        - the ACTUAL goal, not the stated one.
  2. Find the cheapest path  - lowest-cost route to that goal, given the board NOW.
  3. Test the candidate      - on the path, or a detour?
  4. Demand the falsifier    - what would have to be true for the candidate to win?

Verdict: GREENLIGHT | DOWNGRADE | REFUSE.

Unlike Stage 1, the judgment here cannot be done with keyword matching - steps 2
and 4 are irreducibly a matter of judgment, which needs a language model. So this
module calls a model IF one is configured, and otherwise writes the exact
reasoning prompt to a file for you to paste into Claude. The procedure is
identical either way. Validate the verdicts in paste mode first; wire a key for
autonomy once you trust them.

Config (environment variables):
  AGENTOS_PROVIDER   gemini | anthropic | (unset -> paste mode)
  AGENTOS_MODEL      the model id to use (YOU set this - see your provider's docs;
                     this module deliberately does not hardcode a model name)
  GEMINI_API_KEY  or  ANTHROPIC_API_KEY

Usage:
  python questioner.py "build stages 3-6 of the AgentOS now"
  python questioner.py "send the RAG demo link to Mohammed" --top 4
  python questioner.py "..." --show-prompt      # assemble + print, do not call
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# P3-A fix: call_model() reads ANTHROPIC_API_KEY/AGENTOS_PROVIDER/AGENTOS_MODEL
# from os.environ, but nothing guaranteed .env was ever loaded into it - that
# only happened by accident when the caller (mcp_server.py, or an ad-hoc test
# script) loaded it first. Direct/CLI use (`python governor.py "..."`) had no
# such caller, so it silently degraded to HOLD ("no model wired") even with a
# valid key sitting right here in .env. Load it ourselves so this module is
# correct standalone, not just when something upstream happens to help it.
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

import state_layer as sl   # Stage 1, in the same folder

DECISIONS_PATH = sl.STATE_DIR / "decisions.json"
PROMPT_DUMP_PATH = sl.STATE_DIR / "questioner_prompt.txt"

SYSTEM_PROMPT = """You are the QUESTIONER - the senior function of a personal AgentOS. You do not execute. You judge whether a proposed action should be executed at all.

You operate from a WORLD MODEL (excerpts provided) describing one specific person, and a LIVE BOARD describing their situation right now. Your loyalty is to that person's ACTUAL goal - the one derived from who they are and what actually moves them - not to their stated request, and not to whatever sounds most ambitious. A builder that builds the wrong thing faster is worse than no builder. Your job is to stop that.

Run this exact four-step procedure on the CANDIDATE ACTION:

1. DERIVE THE GOAL. From the world model and board, state the actual goal this action would serve. Quantify the stakes if the board gives you numbers (deadlines, money owed, who is waiting).

2. FIND THE CHEAPEST PATH. Given the board exactly as it is right now, what is the lowest-cost route to that goal? Cheapest in time-to-a-real-outcome and in effort - not cheapest in money. Name the single move that most moves the ground.

3. TEST THE CANDIDATE. Is the candidate ON the cheapest path, or a detour? Be specific about what it produces, whether a real human sees a real artifact as a result, and when.

4. DEMAND THE FALSIFIER. State precisely what would have to be TRUE for the candidate to beat the cheapest path. Then check the board and world model: does it hold? If you cannot find that it holds, the candidate loses.

Then return a verdict:
- GREENLIGHT: the candidate IS the cheapest path, or close enough that the difference does not matter. Proceed.
- DOWNGRADE: the candidate serves the goal but is not the cheapest path. Name the smaller action that should happen first.
- REFUSE: the candidate is a detour, a sophistication trap, or serves a stated goal that conflicts with the derived one. Say so plainly and name what to do instead.

Rules:
- You MUST be able to GREENLIGHT. If the candidate is genuinely the right move, say so without hedging. A Questioner that refuses everything is as broken as one that approves everything.
- Do NOT moralize. Do NOT psychoanalyze the person or narrate their patterns. Judge the action against the goal, nothing else.
- Cite the specific board item or world-model fact that drives the verdict.
- Be brief and decisive: roughly one sentence per step.

Output STRICT JSON only, with these keys:
  derived_goal (string)
  cheapest_path (string)
  candidate_on_path (boolean)
  falsifier (string)
  falsifier_holds (boolean)
  verdict ("GREENLIGHT" | "DOWNGRADE" | "REFUSE")
  do_instead (string)
  reasoning (string)
"""


def gather_goal_context(top_k=4):
    """Pull the world-model material most relevant to deriving the real goal."""
    queries = [
        "what does zayed actually want beneath what he says financial guilt income father",
        "what blocks zayed execution dopamine commitment disappearance planning pattern",
    ]
    manifest = sl.load_manifest()
    seen, blocks = set(), []
    for q in queries:
        terms = set(sl.tokenize(q))
        scored = []
        for entry in manifest["files"]:
            p = sl.CORPUS_DIR / entry["path"]
            if not p.exists():
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
            s = sl.score_text(terms, text, entry["title"], entry["headings"])
            if s > 0:
                scored.append((s, entry, text))
        scored.sort(key=lambda x: x[0], reverse=True)
        for _, entry, text in scored[:2]:
            if entry["path"] in seen:
                continue
            seen.add(entry["path"])
            ex = sl.best_excerpts(terms, text, max_chars=600)
            if ex:
                blocks.append(f"[{entry['title']}]\n" + "\n".join(ex))
    return "\n\n".join(blocks) if blocks else "(no corpus loaded - run: python state_layer.py index)"


def load_board():
    if not sl.GROUND_STATE_PATH.exists():
        return {}
    return json.loads(sl.GROUND_STATE_PATH.read_text(encoding="utf-8"))


def build_user_message(action, top_k=4):
    board = load_board()
    goal_ctx = gather_goal_context(top_k=top_k)
    return (
        "WORLD MODEL (excerpts on who this person actually is):\n"
        f"{goal_ctx}\n\n"
        "LIVE BOARD (the situation right now):\n"
        f"{json.dumps(board, indent=2)}\n\n"
        "CANDIDATE ACTION to judge:\n"
        f"{action}\n\n"
        "Run the four-step procedure. Return strict JSON only."
    )


def _estimate_cost(provider, input_text, output_text):
    """Rough cost estimate: len/4 as token proxy."""
    input_tokens = len(input_text) / 4
    output_tokens = len(output_text) / 4
    if provider == "gemini":
        return (input_tokens / 1000) * 0.00015 + (output_tokens / 1000) * 0.0006
    if provider == "anthropic":
        return (input_tokens / 1000) * 0.003 + (output_tokens / 1000) * 0.015
    return 0.0


def _record_spend(cost):
    """Add cost to api_spent_usd in ground_state.json. Silent on any error."""
    try:
        state = json.loads(sl.GROUND_STATE_PATH.read_text(encoding="utf-8"))
        r = state.setdefault("resources", {})
        r["api_spent_usd"] = round(r.get("api_spent_usd", 0.0) + cost, 6)
        sl.save_state(state)
    except Exception:
        pass


def call_model(system, user):
    """Return (text, error). text is None when no model ran."""
    provider = os.environ.get("AGENTOS_PROVIDER", "").lower()
    model = os.environ.get("AGENTOS_MODEL", "").strip()

    # ── HARD GUARD: only Haiku is allowed (Sonnet/Opus burn credits) ──
    if provider == "anthropic" and model:
        model_lower = model.lower()
        if "sonnet" in model_lower or "opus" in model_lower:
            return None, (
                f"BLOCKED: model '{model}' is forbidden. "
                "Only claude-haiku-4-5-20251001 is allowed. "
                "Set AGENTOS_MODEL=claude-haiku-4-5-20251001 in .env"
            )

    if provider == "gemini":
        key = os.environ.get("GEMINI_API_KEY")
        if not key or not model:
            return None, "Gemini selected but GEMINI_API_KEY or AGENTOS_MODEL is missing."
        try:
            from google import genai
            client = genai.Client(api_key=key)
            response = client.models.generate_content(
                model=model,
                contents=user,
                config=genai.types.GenerateContentConfig(system_instruction=system)
            )
            text = response.text
            _record_spend(_estimate_cost("gemini", system + user, text))
            return text, None
        except Exception as e:
            return None, f"Gemini call failed: {e}"

    if provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key or not model:
            return None, "Anthropic selected but ANTHROPIC_API_KEY or AGENTOS_MODEL is missing."
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=key)
            resp = client.messages.create(
                model=model, max_tokens=1200, system=system,
                messages=[{"role": "user", "content": user}],
            )
            text = resp.content[0].text
            _record_spend(_estimate_cost("anthropic", system + user, text))
            return text, None
        except Exception as e:
            return None, f"Anthropic call failed: {e}"

    return None, "no-provider"


def parse_verdict(text):
    if not text:
        return None
    t = re.sub(r"^```(json)?", "", text.strip()).strip()
    t = re.sub(r"```$", "", t).strip()
    i, j = t.find("{"), t.rfind("}")
    if i == -1 or j == -1:
        return {"verdict": "UNPARSED", "reasoning": text}
    try:
        return json.loads(t[i:j + 1])
    except json.JSONDecodeError:
        return {"verdict": "UNPARSED", "reasoning": text}


def judge(action, top_k=4):
    """Run the Questioner on an action. Returns (verdict_dict, error_string)."""
    user = build_user_message(action, top_k=top_k)
    text, err = call_model(SYSTEM_PROMPT, user)
    if text is None:
        return None, err
    return parse_verdict(text), None


def record_decision(action, verdict):
    log = []
    if DECISIONS_PATH.exists():
        log = json.loads(DECISIONS_PATH.read_text(encoding="utf-8"))
    log.append({
        "date": datetime.now().isoformat(timespec="seconds"),
        "action": action,
        "verdict": verdict.get("verdict") if verdict else None,
        "detail": verdict,
    })
    DECISIONS_PATH.write_text(json.dumps(log, indent=2), encoding="utf-8")


def print_verdict(action, v):
    line = "=" * 64
    print(f"\n{line}\nQUESTIONER  -  verdict on:\n  {action}\n{line}")
    if not v:
        return
    print(f"\nVERDICT: {v.get('verdict', '?')}")
    if v.get("derived_goal"):
        print(f"\nDERIVED GOAL:\n  {v['derived_goal']}")
    if v.get("cheapest_path"):
        print(f"\nCHEAPEST PATH:\n  {v['cheapest_path']}")
    if "candidate_on_path" in v:
        print(f"\nON THE PATH?  {v['candidate_on_path']}")
    if v.get("falsifier"):
        print(f"\nFALSIFIER (what must be true to beat the cheapest path):\n  {v['falsifier']}")
    if "falsifier_holds" in v:
        print(f"  holds?  {v['falsifier_holds']}")
    if v.get("do_instead"):
        print(f"\nDO INSTEAD:\n  {v['do_instead']}")
    if v.get("reasoning"):
        print(f"\nREASONING:\n  {v['reasoning']}")
    print()


def decide(action, top_k=4, show_prompt=False):
    user = build_user_message(action, top_k=top_k)
    if show_prompt:
        print(SYSTEM_PROMPT)
        print("\n" + "=" * 64 + "\n")
        print(user)
        return
    text, err = call_model(SYSTEM_PROMPT, user)
    if text is None:
        PROMPT_DUMP_PATH.write_text(SYSTEM_PROMPT + "\n\n" + user, encoding="utf-8")
        print("No model configured (or the call failed). Wrote the full reasoning prompt to:")
        print(f"  {PROMPT_DUMP_PATH}")
        if err and err != "no-provider":
            print(f"  reason: {err}")
        print("Paste it into Claude for the verdict, or set AGENTOS_PROVIDER + AGENTOS_MODEL + a key for autonomy.")
        return
    verdict = parse_verdict(text)
    record_decision(action, verdict)
    print_verdict(action, verdict)


def main():
    ap = argparse.ArgumentParser(description="AgentOS Stage 2 - the Questioner")
    ap.add_argument("action", help="the candidate action to judge")
    ap.add_argument("--top", type=int, default=4)
    ap.add_argument("--show-prompt", action="store_true",
                    help="assemble and print the reasoning prompt without calling a model")
    args = ap.parse_args()
    decide(args.action, top_k=args.top, show_prompt=args.show_prompt)


if __name__ == "__main__":
    main()
