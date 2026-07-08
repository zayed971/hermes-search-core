#!/usr/bin/env python3
"""
AgentOS - The Governor

Turns the Questioner from a tool Hermes CAN call into a gate it MUST pass.

Every autonomous (AUTO) action is run through the Questioner BEFORE it executes.
Only a GREENLIGHT proceeds. Anything else - DOWNGRADE, REFUSE, or "no verdict
available" - does NOT auto-execute; it is held for the human. Enforcement is
structural: the action gate calls the governor on its AUTO path, so the only way
to perform an autonomous action is through a check that can stop it.

The fail-safe rule is the important one: if there is no model wired (so the
Questioner can't produce a verdict unattended), the governor returns HOLD, not
PROCEED. Principle: no judge, no autonomous action. Wire the Questioner's model
(AGENTOS_PROVIDER / AGENTOS_MODEL / key) and AUTO actions get real verdicts and
proceed on GREENLIGHT; leave it unwired and AUTO safely degrades to human review.

Decisions:
  PROCEED   - Questioner GREENLIT. Execute.
  DOWNGRADE - Questioner says there's a better/cheaper move. Hold; surface do_instead.
  BLOCK     - Questioner REFUSED. Do not execute. Surface why.
  HOLD      - No verdict available (no model / parse failure). Fail safe to human.

Usage:
  python governor.py "push the cleaned rag-demo to my repo"
"""

import argparse
import json
from datetime import datetime

import state_layer as sl
import questioner as Q
import verifier  # P1-B verification spine - every decision gets logged through it

GOVERNOR_LOG = sl.STATE_DIR / "governor_log.md"


def _model_wired():
    import os
    provider = os.environ.get("AGENTOS_PROVIDER", "").lower()
    model = os.environ.get("AGENTOS_MODEL", "").strip()
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    return bool(provider in {"gemini", "anthropic"} and model and key)


def _log(action, decision, verdict):
    stamp = datetime.now().isoformat(timespec="seconds")
    v = verdict.get("verdict") if verdict else "(no verdict)"
    with open(GOVERNOR_LOG, "a", encoding="utf-8") as f:
        f.write(f"\n- **{stamp}** {decision}  ({v})  :: {action}")


def consult(action):
    """Run the Questioner on the action. Returns (verdict_dict_or_None, error_or_None)."""
    if not _model_wired():
        return None, "no model wired (AGENTOS_PROVIDER / AGENTOS_MODEL / key not set in .env)"
    user = Q.build_user_message(action)
    text, err = Q.call_model(Q.SYSTEM_PROMPT, user)
    if text is None:
        return None, err
    return Q.parse_verdict(text), None


def _spine_log(action, out):
    """P1-B: every governor decision gets a verify_claim-checked record in
    state/job_results.jsonl, not just the markdown governor_log.md append.
    Logs the decision, not whether the underlying action succeeded - that's
    verify_claim's job at the action's own call site, not the governor's."""
    def _job():
        return {
            "claim": f"governed action '{action}' -> {out['decision']}",
            "artifact_ref": str(GOVERNOR_LOG),
        }
    verifier.run_job("Governor Check", _job, quiet=True)


def decide(action):
    """
    The gate every AUTO action passes. Returns a decision dict:
      {decision, verdict, do_instead, reason}

    decision is one of:
      PROCEED   - Questioner GREENLIT. Execute.
      DOWNGRADE - Questioner says there's a better/cheaper move.
      HOLD      - Questioner REFUSED (risky/destructive/irreversible), or no
                  verdict was available at all (no model wired, or the API
                  call itself failed). Either way: do not auto-execute,
                  surface to the human.
      BLOCKED   - The Opus/Sonnet hard cost-guard tripped. Distinct from HOLD:
                  this is a configuration problem (wrong model wired), not a
                  judgment call about the action itself.
    """
    verdict, err = consult(action)

    if verdict is None:
        if err and err.startswith("BLOCKED:"):
            out = {
                "decision": "BLOCKED",
                "verdict": None,
                "do_instead": "Set AGENTOS_MODEL=claude-haiku-4-5-20251001 in .env.",
                "reason": err,
            }
        else:
            # FAIL SAFE: no judge available -> do not auto-execute.
            out = {
                "decision": "HOLD",
                "verdict": None,
                "do_instead": "Review and run manually, or wire the Questioner's model "
                              "(AGENTOS_PROVIDER/AGENTOS_MODEL/key) so autonomous actions can be judged.",
                "reason": err or "No model wired, so the Questioner could not judge this action. "
                          "No judge, no autonomous action.",
            }
        _log(action, out["decision"], None)
        _spine_log(action, out)
        return out

    v = verdict.get("verdict", "").upper()
    if v == "GREENLIGHT":
        out = {"decision": "PROCEED", "verdict": verdict, "do_instead": None,
               "reason": verdict.get("reasoning", "")}
    elif v == "DOWNGRADE":
        out = {"decision": "DOWNGRADE", "verdict": verdict,
               "do_instead": verdict.get("do_instead", ""),
               "reason": verdict.get("reasoning", "")}
    elif v == "REFUSE":
        # Scoped narrowly (P3-A): the governor judges risky/destructive/
        # irreversible actions. A REFUSE on one of those is a HOLD - it
        # blocks the action, pending human review - not a silent BLOCK.
        out = {"decision": "HOLD", "verdict": verdict,
               "do_instead": verdict.get("do_instead", ""),
               "reason": verdict.get("reasoning", "")}
    else:
        # unparsed / unexpected -> fail safe
        out = {"decision": "HOLD", "verdict": verdict,
               "do_instead": "Verdict unclear; review manually.",
               "reason": "Questioner returned an unparseable verdict."}
    _log(action, out["decision"], verdict)
    _spine_log(action, out)
    return out


def main():
    ap = argparse.ArgumentParser(description="AgentOS - the Governor")
    ap.add_argument("action")
    args = ap.parse_args()
    d = decide(args.action)
    print(json.dumps(d, indent=2))


if __name__ == "__main__":
    main()
