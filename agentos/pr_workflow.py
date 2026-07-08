#!/usr/bin/env python3
"""
AgentOS - PR Workflow

Extends the action gate from "push to a repo" to the full pull-request loop:
create a PR, check its status, respond to review comments, and (one-tap only)
merge. Uses the GitHub REST API directly via httpx - no new heavy dependency.

THE SAFETY LINE, stated plainly:
  - create_pr / check_pr_status / respond_to_review are REVERSIBLE or read-only.
    A PR can be closed, a comment edited/deleted. These are AUTO-capable: the
    agent may do them unattended (still through the action gate's notify path).
  - merge_pr writes to your default branch. It is the one PR action with no clean
    undo (a revert is a new commit; CI/deploys may already have fired). So merge
    is routed as TAP - one tap from your phone - NEVER autonomous. You keep merge
    convenience; you don't get auto-merge-to-main while you sleep.

Needs GITHUB_TOKEN in the environment (a classic or fine-grained PAT with `repo`
/ pull-request write). Without it, every call returns a clear, non-crashing
message instead of failing - same fail-safe philosophy as the rest of AgentOS.

repo format: "owner/name"  (e.g. "zayed971/rag-demo")

Usage:
  python pr_workflow.py create  owner/name --branch feat-x --title "..." --body "..."
  python pr_workflow.py status  owner/name --pr 12
  python pr_workflow.py respond owner/name --pr 12 --comment "addressed in abc123"
  python pr_workflow.py merge   owner/name --pr 12        # routes to TAP queue, not direct
"""

import argparse
import json
import os

try:
    import httpx
except ImportError:
    httpx = None  # degrade gracefully; _request reports it instead of crashing

import action_gate as ag

API = "https://api.github.com"


def _token():
    return os.environ.get("GITHUB_TOKEN", "").strip()


def _headers():
    return {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _need_token():
    if not _token():
        return {"ok": False, "error": "GITHUB_TOKEN not set in environment. "
                "Export it, then retry. (No call was made.)"}
    return None


def _request(method, path, **kw):
    """One place for the HTTP call, with honest error handling."""
    if httpx is None:
        return {"ok": False, "error": "httpx not installed (pip install httpx). No call made."}
    miss = _need_token()
    if miss:
        return miss
    try:
        with httpx.Client(timeout=30) as c:
            r = c.request(method, f"{API}{path}", headers=_headers(), **kw)
        if r.status_code >= 400:
            return {"ok": False, "status": r.status_code,
                    "error": r.json().get("message", r.text) if r.text else f"HTTP {r.status_code}"}
        return {"ok": True, "status": r.status_code, "data": r.json() if r.text else {}}
    except Exception as e:
        return {"ok": False, "error": f"request failed: {e}"}


# --- AUTO-capable: reversible / read-only -----------------------------------
def create_pr(repo, branch, title, body, base="main"):
    """Open a PR from `branch` into `base`. Reversible (a PR can be closed)."""
    res = _request("POST", f"/repos/{repo}/pulls",
                   json={"title": title, "head": branch, "base": base, "body": body})
    if res.get("ok"):
        d = res["data"]
        return {"ok": True, "pr_number": d.get("number"), "url": d.get("html_url"),
                "state": d.get("state")}
    return res


def check_pr_status(repo, pr_number):
    """Read-only: PR state, mergeability, review comments, and CI/check results."""
    pr = _request("GET", f"/repos/{repo}/pulls/{pr_number}")
    if not pr.get("ok"):
        return pr
    d = pr["data"]
    sha = d.get("head", {}).get("sha", "")
    reviews = _request("GET", f"/repos/{repo}/pulls/{pr_number}/reviews")
    checks = _request("GET", f"/repos/{repo}/commits/{sha}/check-runs") if sha else {"data": {}}
    runs = checks.get("data", {}).get("check_runs", []) if checks.get("ok") else []
    return {
        "ok": True,
        "number": pr_number,
        "state": d.get("state"),
        "mergeable": d.get("mergeable"),
        "mergeable_state": d.get("mergeable_state"),
        "approved": any(rv.get("state") == "APPROVED"
                        for rv in (reviews.get("data", []) if reviews.get("ok") else [])),
        "review_count": len(reviews.get("data", [])) if reviews.get("ok") else 0,
        "ci": [{"name": r.get("name"), "status": r.get("status"),
                "conclusion": r.get("conclusion")} for r in runs],
        "ci_all_passed": bool(runs) and all(r.get("conclusion") == "success" for r in runs),
        "url": d.get("html_url"),
    }


def respond_to_review(repo, pr_number, comment):
    """Post a top-level comment on the PR thread. Reversible (editable/deletable)."""
    res = _request("POST", f"/repos/{repo}/issues/{pr_number}/comments",
                   json={"body": comment})
    if res.get("ok"):
        return {"ok": True, "comment_url": res["data"].get("html_url")}
    return res


# --- TAP-only: irreversible. Routed through the gate, never fired directly ---
def merge_pr(repo, pr_number, method="squash"):
    """
    Merge a PR. THIS IS IRREVERSIBLE (writes to your default branch), so it does
    NOT call the GitHub merge API directly. It routes through the action gate as
    a one-tap action: the agent stages it; you tap merge from your phone.
    """
    # Pull current status so the queued card shows you what you're approving.
    status = check_pr_status(repo, pr_number)
    safe = status.get("approved") and status.get("ci_all_passed")
    payload = (
        f"MERGE {repo} PR #{pr_number} ({method})\n"
        f"  approved: {status.get('approved')}   CI passed: {status.get('ci_all_passed')}\n"
        f"  mergeable_state: {status.get('mergeable_state')}\n"
        f"  url: {status.get('url')}\n"
        f"  -> to merge, confirm from your phone. The agent will NOT auto-merge.\n"
        f"  (api: PUT /repos/{repo}/pulls/{pr_number}/merge  merge_method={method})"
    )
    # Force the irreversible tier explicitly; do not rely on token classification.
    ag.queue_for_tap(f"merge {repo} PR #{pr_number}", payload, recipient="you (GitHub)")
    return {"ok": True, "routed": "TAP",
            "note": "Merge queued for one-tap confirmation, not auto-merged.",
            "looks_ready": bool(safe)}


# --- gate integration --------------------------------------------------------
def gate_github_action(action_text, repo=None, **kw):
    """
    Entry point the action gate / loop calls for a GitHub move. Decides which PR
    operation an AUTO GitHub action implies, and keeps merge on the TAP rail.
    """
    t = action_text.lower()
    if "merge" in t:
        # never auto: hand to TAP regardless of tier
        return merge_pr(repo, kw.get("pr_number"), kw.get("method", "squash"))
    if "respond" in t or "review" in t:
        return respond_to_review(repo, kw.get("pr_number"), kw.get("comment", ""))
    if "status" in t or "check" in t:
        return check_pr_status(repo, kw.get("pr_number"))
    if "pr" in t or "pull request" in t:
        return create_pr(repo, kw.get("branch"), kw.get("title", "Automated PR"),
                          kw.get("body", ""), kw.get("base", "main"))
    return {"ok": False, "error": "no PR operation matched the action text"}


def main():
    ap = argparse.ArgumentParser(description="AgentOS PR workflow")
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("create"); c.add_argument("repo"); c.add_argument("--branch", required=True)
    c.add_argument("--title", required=True); c.add_argument("--body", default=""); c.add_argument("--base", default="main")
    s = sub.add_parser("status"); s.add_argument("repo"); s.add_argument("--pr", type=int, required=True)
    r = sub.add_parser("respond"); r.add_argument("repo"); r.add_argument("--pr", type=int, required=True); r.add_argument("--comment", required=True)
    m = sub.add_parser("merge"); m.add_argument("repo"); m.add_argument("--pr", type=int, required=True); m.add_argument("--method", default="squash")
    a = ap.parse_args()

    if a.cmd == "create":
        print(json.dumps(create_pr(a.repo, a.branch, a.title, a.body, a.base), indent=2))
    elif a.cmd == "status":
        print(json.dumps(check_pr_status(a.repo, a.pr), indent=2))
    elif a.cmd == "respond":
        print(json.dumps(respond_to_review(a.repo, a.pr, a.comment), indent=2))
    elif a.cmd == "merge":
        print(json.dumps(merge_pr(a.repo, a.pr, a.method), indent=2))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
