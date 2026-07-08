#!/usr/bin/env python3
"""
AgentOS - Stage 10: The Verifier

Every other stage can be made to lie by a model that hallucinates success: a job
returns "done" with nothing to show for it. The Builder, the Backbone, and any
future automated job all share one failure mode - self-reported completion - so
the fix belongs in one place, not six.

The contract: a job does its work and self-reports a CLAIM plus an ARTIFACT_REF
(the one piece of evidence that should exist on disk if the claim is true).
run_job() never trusts the claim - it calls verify_claim() against the real
filesystem and only stamps "done" when that comes back ok:true. Anything else -
missing file, empty file, an exception - is stamped FAILED, with the reason kept
alongside it. No embeddings, no API, no judgment call: a path either exists with
real content or it doesn't.

Every run appends one JSON line to state/job_results.jsonl:
  {job, claim, artifact_ref, verify_result, ts, status}

verify_claim() itself is NOT duplicated here - it's imported from verify_claim.py
(P1-A, GATE 1a-tested). This module only adds the schema + the refuse-unless-ok
wrapper around it; two copies of the verification logic is exactly the kind of
unverified-by-construction drift this whole spine exists to prevent.

Commands:
  python verifier.py --demo          Run a sample honest job and a sample lying
                                      job, side by side, so you can see the wrapper
                                      catch the lie.
  python verifier.py --show-log      Print the job-results log.
"""

import argparse
import json
from datetime import datetime

import state_layer as sl
from verify_claim import verify_claim

JOB_RESULTS_LOG = sl.STATE_DIR / "job_results.jsonl"


def _stamp(record):
    with open(JOB_RESULTS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# --- the wrapper ---------------------------------------------------------------
def run_job(job_name, fn, *args, quiet=False, **kwargs):
    """Run fn(*args, **kwargs), which must return {"claim": ..., "artifact_ref": ...}.

    Refuses to stamp "done" unless verify_claim(artifact_ref) comes back ok:true.
    quiet=True still writes the job_results.jsonl record but skips the stdout
    print - for cron scripts (Budget Guard, Bootstrap Sync) whose stdout is
    delivered verbatim to Telegram and must stay silent on routine runs.
    """
    ts = datetime.now().isoformat(timespec="seconds")
    try:
        outcome = fn(*args, **kwargs) or {}
    except Exception as e:
        outcome = {"claim": f"job raised an exception: {e}", "artifact_ref": ""}

    claim = outcome.get("claim", "")
    artifact_ref = outcome.get("artifact_ref", "")
    verify_result = verify_claim(artifact_ref)
    status = "done" if verify_result["ok"] else "FAILED"

    record = {
        "job": job_name,
        "claim": claim,
        "artifact_ref": artifact_ref,
        "verify_result": verify_result,
        "ts": ts,
        "status": status,
    }
    _stamp(record)
    if not quiet:
        print(f"[{status}] {job_name}: {verify_result['evidence']}")
    return record


# --- sample jobs for the demo / DoD check --------------------------------------
def _sample_honest_job():
    """Actually writes the file it's about to claim."""
    out = sl.STATE_DIR / "sample_honest_output.txt"
    out.write_text("real output, written before the claim was made\n", encoding="utf-8")
    return {"claim": f"wrote {out.name}", "artifact_ref": str(out)}


def _sample_lying_job():
    """Claims a file it never writes - the case the wrapper must catch."""
    fake = sl.STATE_DIR / "sample_lying_output.txt"
    if fake.exists():
        fake.unlink()  # make sure we're not accidentally verifying a stale file
    return {"claim": f"wrote {fake.name}", "artifact_ref": str(fake)}


def demo():
    print("Running sample honest job (writes the file it claims):")
    run_job("sample_honest_job", _sample_honest_job)
    print("\nRunning sample lying job (claims a file it never writes):")
    run_job("sample_lying_job", _sample_lying_job)


def show_log():
    if not JOB_RESULTS_LOG.exists():
        print("No job results logged yet.")
        return
    for line in JOB_RESULTS_LOG.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        vr = rec.get("verify_result", {})
        detail = vr.get("evidence", vr.get("reason", ""))
        print(f"[{rec['status']}] {rec['ts']}  {rec['job']}  -  {detail}")


def main():
    ap = argparse.ArgumentParser(description="AgentOS Stage 10 - the Verifier")
    ap.add_argument("--demo", action="store_true", help="run the honest + lying sample jobs")
    ap.add_argument("--show-log", action="store_true", help="print state/job_results.jsonl")
    args = ap.parse_args()

    if args.demo:
        demo()
    elif args.show_log:
        show_log()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
