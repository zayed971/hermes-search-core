#!/usr/bin/env python3
"""
AgentOS - Stage 8: The North Star Challenger

Sits ABOVE the Questioner. The Questioner judges actions against the goal. This
judges the GOAL itself - is the north star too small, or too big - given who this
person actually is, what they've actually finished, and what the market actually
looks like.

It runs WEEKLY, never nightly. It never edits the goal. It writes a challenge to
north_star_challenge.md and the human decides.

THE FLATTERY PROBLEM is the whole design risk: a model told "challenge whether
they're thinking big enough" will sycophantically inflate. Four structural
defenses (not just instructions):

  1. EVIDENCE-GATED. Every challenge must cite a fact that actually appears in the
     corpus/board. After generation, claims whose cited evidence cannot be found
     in the source text are flagged as UNGROUNDED - so hype can't hide as analysis.
  2. BIDIRECTIONAL BY FORCE. Every run must produce BOTH an "aim higher because X"
     and an "aim lower / unrealistic because Y" section. A flattery machine cannot
     survive being made to argue against itself with evidence each time.
  3. REALITY COUNTERWEIGHT. It reads the outcome ledger + decisions log - what has
     actually been finished - and weighs that against what is theoretically
     possible. Track record vs potential, explicitly.
  4. NEVER WRITES THE GOAL. Output is a document. The human moves the north star,
     or doesn't.

Like the Questioner/Builder, the judgment needs a model. With one wired it runs
autonomously (weekly). With none, it writes the full challenge prompt to
north_star_prompt.txt for you to paste. Same procedure either way.

Usage:
  python north_star.py                 # run the weekly challenge (or write the prompt)
  python north_star.py --show          # show the latest challenge
  python north_star.py --market "..."  # supply market notes to ground the analysis
"""

import argparse
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import state_layer as sl
import questioner as Q

CHALLENGE_PATH = sl.STATE_DIR / "north_star_challenge.md"
PROMPT_DUMP = sl.STATE_DIR / "north_star_prompt.txt"
CADENCE_PATH = sl.STATE_DIR / "north_star_last_run.json"
DECISIONS_PATH = sl.STATE_DIR / "decisions.json"

CADENCE_DAYS = 7


SYSTEM_PROMPT = """You are the NORTH STAR CHALLENGER of a personal AgentOS. You do not judge actions - that is the Questioner's job. You judge the GOAL ITSELF. Is this person's north star too small, or too big, given who they actually are, what they have actually finished, and what the market actually allows?

You will receive: the current goal, excerpts from the world model (who they are, their assets, their documented behavioral patterns), their TRACK RECORD (what has actually shipped / been finished), and any market notes provided.

You MUST resist flattery. "Think bigger" with no evidence is a failure. Follow these rules exactly:

1. Produce BOTH directions, always:
   - UPWARD: a case that the goal is too small, IF the evidence supports it.
   - DOWNWARD: a case that the goal is too big / unrealistic given specific constraints.
   Both sections must be filled every time. If you cannot make the downward case, your upward case is probably inflated - say so.

2. EVERY claim - up or down - must cite a SPECIFIC fact from the world model or track record, quoted or closely referenced. No claim may rest on generic optimism. "You have a Golden Visa so aim higher" is too weak; tie assets to a concrete, named mechanism and an honest constraint.

3. Weigh POTENTIAL against TRACK RECORD explicitly. If potential is high but the record shows few finished things or a disappearance/abandonment pattern, the binding constraint is execution, not ambition - say that plainly. Do not let theoretical ceiling override demonstrated behavior.

4. You do NOT set the goal. You present the challenge. End with a clear, falsifiable proposal the human can accept or reject - not a command.

Output STRICT JSON only:
{
  "summary": "one honest sentence: is the goal mis-sized, and which way?",
  "upward": [ {"claim": "...", "evidence": "specific fact from the material", "mechanism": "the concrete path", "honest_risk": "what makes it hard"} ],
  "downward": [ {"claim": "...", "evidence": "specific fact / constraint", "why": "..."} ],
  "binding_constraint": "the one thing that actually limits this person right now, with evidence",
  "proposal": "a specific, falsifiable suggestion for the north star - or 'keep current goal' if it is correctly sized, with why",
  "confidence": "low | medium | high, and why"
}
"""


def due():
    """Weekly cadence - True if it's been >= CADENCE_DAYS since last run."""
    if not CADENCE_PATH.exists():
        return True
    last = json.loads(CADENCE_PATH.read_text(encoding="utf-8")).get("date")
    d = sl.parse_date(last) if last else None
    if not d:
        return True
    return (date.today() - d) >= timedelta(days=CADENCE_DAYS)


def mark_run():
    CADENCE_PATH.write_text(json.dumps({"date": date.today().isoformat()}, indent=2),
                            encoding="utf-8")


def gather_world(top_k=4):
    """Assets + patterns: what the challenge must be grounded in."""
    queries = [
        "assets strengths UAE Golden Visa Arabic AI engineering systems thinking capability",
        "behavioral pattern commitment disappearance abandonment follow-through discipline",
        "what does this person actually want goal ambition financial",
    ]
    manifest = sl.load_manifest()
    seen, blocks = set(), []
    for q in queries:
        terms = set(sl.tokenize(q))
        scored = []
        for entry in manifest.get("files", []):
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
            ex = sl.best_excerpts(terms, text, max_chars=550)
            if ex:
                blocks.append(f"[{entry['title']}]\n" + "\n".join(ex))
    return "\n\n".join(blocks) if blocks else "(no corpus - run state_layer.py index)"


def gather_track_record():
    """What has ACTUALLY shipped/finished - the reality counterweight."""
    board = Q.load_board()
    lines = []
    shipped = board.get("shipped", [])
    lines.append(f"SHIPPED ({len(shipped)} total):")
    for s in shipped:
        seen = "seen by someone who matters" if s.get("seen_by") else "NOT yet seen by anyone who matters"
        lines.append(f"  - {s.get('what','?')} [{seen}]")
    pending = board.get("pending", [])
    lines.append(f"PENDING / unfinished ({len(pending)}):")
    for p in pending:
        age = sl.age_days(p.get("created"))
        lines.append(f"  - {p.get('what','?')}" + (f" [{age}d old]" if age is not None else ""))
    out = board.get("last_outcome", {})
    if out:
        lines.append(f"LAST RECORDED OUTCOME: pulled={out.get('did_you_pull_it')} "
                     f"moved={out.get('ground_moved')} - {out.get('trigger_issued','')}")
    # decisions ledger volume = how much has even been decided vs done
    if DECISIONS_PATH.exists():
        d = json.loads(DECISIONS_PATH.read_text(encoding="utf-8"))
        pulled = sum(1 for x in d if (x.get("detail") or {}).get("did_you_pull_it"))
        lines.append(f"OUTCOME LEDGER: {len(d)} entries, {pulled} pulled.")
    return "\n".join(lines)


def build_message(market=""):
    board = Q.load_board()
    goal = board.get("goal", {})
    return (
        "CURRENT GOAL (the north star under challenge):\n"
        f"{json.dumps(goal, indent=2)}\n\n"
        "WORLD MODEL - assets and documented patterns:\n"
        f"{gather_world()}\n\n"
        "TRACK RECORD - what has actually been finished (the reality counterweight):\n"
        f"{gather_track_record()}\n\n"
        "MARKET NOTES (supplied by the human; may be empty):\n"
        f"{market or '(none provided - flag where market data would change the analysis)'}\n\n"
        "Run the challenge. Produce BOTH directions, each claim cited. Strict JSON only."
    )


def find_ungrounded(verdict, source_text):
    """Flag claims whose cited evidence can't be found in the source material."""
    flags = []
    src = source_text.lower()
    for section in ("upward", "downward"):
        for item in verdict.get(section, []) or []:
            ev = (item.get("evidence") or "").lower()
            # take the longest word in the evidence; if even that isn't in the source, flag it
            words = [w for w in re.findall(r"[a-z0-9]+", ev) if len(w) > 4]
            if words and not any(w in src for w in words):
                flags.append(f"[{section}] ungrounded evidence: \"{item.get('evidence','')[:80]}\"")
    return flags


def parse(text):
    if not text:
        return None
    t = re.sub(r"^```(json)?", "", text.strip()).strip()
    t = re.sub(r"```$", "", t).strip()
    i, j = t.find("{"), t.rfind("}")
    if i == -1 or j == -1:
        return {"summary": "(unparsed)", "raw": text}
    try:
        return json.loads(t[i:j + 1])
    except json.JSONDecodeError:
        return {"summary": "(unparsed)", "raw": text}


def write_challenge(verdict, ungrounded):
    lines = [
        f"# North Star Challenge - {date.today().isoformat()}",
        "",
        "_The goal under examination. This does NOT change your goal - you decide._",
        "",
        f"## Verdict\n{verdict.get('summary','')}",
        f"\n**Binding constraint:** {verdict.get('binding_constraint','(unstated)')}",
        f"\n**Confidence:** {verdict.get('confidence','(unstated)')}",
        "",
        "## The case to AIM HIGHER",
    ]
    up = verdict.get("upward", []) or []
    if not up:
        lines.append("- (none made - the upward case could not be evidenced)")
    for u in up:
        lines.append(f"- **{u.get('claim','')}**")
        lines.append(f"  - evidence: {u.get('evidence','')}")
        lines.append(f"  - mechanism: {u.get('mechanism','')}")
        lines.append(f"  - honest risk: {u.get('honest_risk','')}")
    lines += ["", "## The case to AIM LOWER / what's unrealistic"]
    down = verdict.get("downward", []) or []
    if not down:
        lines.append("- (none made - if empty, the upward case above is suspect)")
    for d in down:
        lines.append(f"- **{d.get('claim','')}**")
        lines.append(f"  - evidence: {d.get('evidence','')}")
        lines.append(f"  - why: {d.get('why','')}")
    lines += ["", "## Proposal (accept or reject - it does not auto-apply)",
              verdict.get("proposal", "(none)")]
    if ungrounded:
        lines += ["", "## ⚠ FLATTERY CHECK - claims that could not be grounded in your material"]
        for f in ungrounded:
            lines.append(f"- {f}")
        lines.append("\n_Treat flagged claims with suspicion; their evidence isn't in your world model._")
    lines += ["", "---",
              "To accept a new north star, edit ground_state.json goal.north_star yourself.",
              "The Challenger never does it for you."]
    CHALLENGE_PATH.write_text("\n".join(lines), encoding="utf-8")


def run(market="", force=False):
    if not due() and not force:
        last = json.loads(CADENCE_PATH.read_text(encoding="utf-8")).get("date")
        print(f"Not due yet (weekly cadence). Last run: {last}. Use --force to override.")
        return

    user = build_message(market)

    text, err = Q.call_model(SYSTEM_PROMPT, user)
    if text is None:
        PROMPT_DUMP.write_text(SYSTEM_PROMPT + "\n\n" + user, encoding="utf-8")
        print("No model wired - wrote the full challenge prompt to:")
        print(f"  {PROMPT_DUMP}")
        print("Paste into Claude for the challenge, or wire a provider (see questioner.py).")
        return

    verdict = parse(text)
    ungrounded = find_ungrounded(verdict, user)
    write_challenge(verdict, ungrounded)
    mark_run()
    print(f"North Star Challenge written -> {CHALLENGE_PATH}")
    if ungrounded:
        print(f"  ⚠ {len(ungrounded)} ungrounded claim(s) flagged (possible flattery).")
    print("  It did not change your goal. Read it; you decide.")


def main():
    ap = argparse.ArgumentParser(description="AgentOS Stage 8 - the North Star Challenger")
    ap.add_argument("--show", action="store_true", help="show the latest challenge")
    ap.add_argument("--force", action="store_true", help="run even if not due")
    ap.add_argument("--market", default="", help="market notes to ground the analysis")
    args = ap.parse_args()
    if args.show:
        print(CHALLENGE_PATH.read_text(encoding="utf-8") if CHALLENGE_PATH.exists()
              else "No challenge yet. Run: python north_star.py")
        return
    run(market=args.market, force=args.force)


if __name__ == "__main__":
    main()
