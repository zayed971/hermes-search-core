#!/usr/bin/env python3
"""
AgentOS - Stage 4: The Builder

PREPARE, upgraded. Stages 1-3 produce a trigger card - they name the move and
make the human step small. The Builder goes one step further for moves whose
"95%" is actual construction: it produces a real artifact (code, docs, a draft)
in a sandboxed drafts/ folder, and leaves only the irreversible step - the push,
the send - as your move.

Hard rules (from the concept's autonomy boundary):
  - Writes ONLY into drafts/<slug>/. Never touches your live repos or files.
  - NEVER pushes, sends, deploys, or does anything irreversible.
  - Every artifact ships with a HANDOFF.md: what it is, how to run it, and the
    exact final human step. That step is the trigger card the loop hands you.

Resource-aware: the Builder is the thing you run when YOU are busy and prepaid
capacity would otherwise expire unused. It reads the resources block in
ground_state and refuses to spend API budget past its cap, but it will gladly
burn idle Claude-Pro capacity that refunds nothing.

Like the Questioner, the construction step needs a model. With one wired it
builds autonomously; with none, it writes a complete build brief to drafts/<slug>/
BUILD_BRIEF.md for you to hand to Claude Code. Same procedure either way.

Config: same env vars as questioner.py (AGENTOS_PROVIDER, AGENTOS_MODEL, key).

Usage:
  python builder.py "a second portfolio project: a FastAPI service that ..."
  python builder.py "..." --slug my-project --show-brief
"""

import argparse
import json
import os
import re
from datetime import date, datetime
from pathlib import Path

import state_layer as sl
import questioner as Q

DRAFTS_DIR = sl.ROOT / "drafts"
DRAFTS_DIR.mkdir(parents=True, exist_ok=True)


BUILDER_SYSTEM = """You are the BUILDER of a personal AgentOS. You produce a real, runnable artifact from a spec - nothing more, nothing less.

You will be given: the spec, plus context on the person you build for and their current goal. Build the smallest thing that fully satisfies the spec and would stand up to a reviewer's eyes. Favor a clean, honest, working artifact over an impressive-looking but broken one.

Output STRICT JSON only, describing files to create. Schema:
{
  "slug": "short-kebab-case-name",
  "summary": "one sentence: what this artifact is",
  "files": [
    {"path": "relative/path.ext", "content": "the full file contents"}
  ],
  "run_instructions": "exact steps to run it locally",
  "final_human_step": "the single irreversible action left for the human (e.g. the git push, the send) - written as a literal command or message"
}

Rules:
- Every file must be complete and correct. No placeholders like '# TODO' or '...'.
- Include a README.md and, for code, a requirements.txt and a .gitignore.
- NEVER include secrets. Use a .env.example with named-but-empty keys.
- Do not invent libraries or versions you are unsure exist; prefer the standard library and widely-known packages.
- final_human_step is the ONLY irreversible action; you never perform it.
"""


def model_available():
    provider = os.environ.get("AGENTOS_PROVIDER", "").lower()
    model = os.environ.get("AGENTOS_MODEL", "").strip()
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    return bool(provider in {"gemini", "anthropic"} and model and key)


# --- resource gate (the fix for the gap that produced the wrong verdict) -----
def resource_check(board):
    """Decide whether building now is the right use of resources, and on whose dime.
    Returns (ok, mode, note). mode is 'prepaid' or 'api' or 'blocked'."""
    r = board.get("resources", {})
    spent = r.get("api_spent_usd", 0.0)
    cap = r.get("api_budget_usd", 0.0)

    if model_available() and os.environ.get("AGENTOS_PROVIDER", "").lower() == "anthropic" \
            and "ANTHROPIC_API_KEY" in os.environ:
        # paid API path - respect the hard cap
        if spent >= cap:
            return False, "blocked", f"API budget reached (${spent:.2f}/${cap:.2f}). Not spending more."
        return True, "api", f"Building on API budget (${spent:.2f}/${cap:.2f} used)."

    # prepaid / Pro path (or paste mode): this is the 'you are busy, capacity expires' case
    note = "Building on prepaid capacity"
    if r.get("operator_busy"):
        note += f" while you are busy ({r.get('operator_busy_reason','elsewhere')})"
    if r.get("capacity_resets_at"):
        note += f"; resets at {r['capacity_resets_at']} and refunds nothing if unused"
    return True, "prepaid", note + "."


def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:40] or "artifact")


def build_user_message(spec):
    board = Q.load_board()
    goal = board.get("goal", {})
    ctx = Q.gather_goal_context(top_k=2)
    return (
        "PERSON + GOAL:\n"
        f"{json.dumps(goal, indent=2)}\n\n"
        "WORLD MODEL (excerpts):\n"
        f"{ctx}\n\n"
        "SPEC to build:\n"
        f"{spec}\n\n"
        "Return strict JSON only."
    )


def parse_build(text):
    if not text:
        return None
    t = re.sub(r"^```(json)?", "", text.strip()).strip()
    t = re.sub(r"```$", "", t).strip()
    i, j = t.find("{"), t.rfind("}")
    if i == -1 or j == -1:
        return None
    try:
        return json.loads(t[i:j + 1])
    except json.JSONDecodeError:
        return None


def write_artifact(plan, spec):
    slug = plan.get("slug") or slugify(spec)
    target = DRAFTS_DIR / slug
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for f in plan.get("files", []):
        fp = target / f["path"]
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(f.get("content", ""), encoding="utf-8")
        written.append(str(fp.relative_to(sl.ROOT)))

    handoff = (
        f"# HANDOFF - {plan.get('summary','(artifact)')}\n\n"
        f"Built by the AgentOS Builder on {date.today().isoformat()}.\n"
        f"Location: drafts/{slug}/  (nothing here is live - review before anything irreversible)\n\n"
        f"## What it is\n{plan.get('summary','')}\n\n"
        f"## Run it\n{plan.get('run_instructions','(see README)')}\n\n"
        f"## THE ONE HUMAN STEP (irreversible - the Builder did not do this)\n"
        f"{plan.get('final_human_step','(review, then push/send yourself)')}\n"
    )
    (target / "HANDOFF.md").write_text(handoff, encoding="utf-8")
    written.append(f"drafts/{slug}/HANDOFF.md")
    return slug, written, plan.get("final_human_step", "")


def write_brief(spec, slug):
    """No-model fallback: a complete build brief to hand to Claude Code."""
    target = DRAFTS_DIR / slug
    target.mkdir(parents=True, exist_ok=True)
    brief = BUILDER_SYSTEM + "\n\n" + build_user_message(spec)
    (target / "BUILD_BRIEF.md").write_text(brief, encoding="utf-8")
    return target / "BUILD_BRIEF.md"


def build(spec, slug=None, show_brief=False):
    board = Q.load_board()

    ok, mode, note = resource_check(board)
    print(f"RESOURCE  {note}")
    if not ok:
        print("BUILDER   blocked. Not building.")
        return

    if model_available():
        verdict, err = Q.judge(spec)
        if verdict and verdict.get('verdict') == 'REFUSE':
            print(f"QUESTIONER REFUSED: {verdict.get('do_instead', '(no alternative given)')}")
            return

    if show_brief or not model_available():
        path = write_brief(spec, slug or slugify(spec))
        print(f"BUILDER   no model wired - wrote a complete build brief to:\n  {path}")
        print("          hand it to Claude Code, or set a provider for autonomous building.")
        print(f"          (artifact would land in drafts/{slug or slugify(spec)}/)")
        return

    user = build_user_message(spec)
    text, err = Q.call_model(BUILDER_SYSTEM, user)
    if text is None:
        path = write_brief(spec, slug or slugify(spec))
        print(f"BUILDER   model call failed ({err}); wrote build brief to:\n  {path}")
        return

    plan = parse_build(text)
    if not plan:
        print("BUILDER   could not parse a build plan from the model output.")
        return

    built_slug, files, final_step = write_artifact(plan, spec)
    print(f"\nBUILDER   built artifact: drafts/{built_slug}/")
    for f in files:
        print(f"          + {f}")
    print(f"\nTHE ONE HUMAN STEP (Builder did not do this):\n  {final_step}\n")


def main():
    ap = argparse.ArgumentParser(description="AgentOS Stage 4 - the Builder")
    ap.add_argument("spec", help="what to build")
    ap.add_argument("--slug", help="folder name under drafts/")
    ap.add_argument("--show-brief", action="store_true",
                    help="write the build brief without calling a model")
    args = ap.parse_args()
    build(args.spec, slug=args.slug, show_brief=args.show_brief)


if __name__ == "__main__":
    main()
