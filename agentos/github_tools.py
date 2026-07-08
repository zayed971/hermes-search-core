#!/usr/bin/env python3
"""
AgentOS - GitHub Awareness Module

Public GitHub API only — no key required.
Fetches repo metadata for zayed971 and exposes three helpers the world_scanner
and other AgentOS stages can call.
"""

import base64
import json
from datetime import datetime, timedelta, timezone
from urllib import error, request

GITHUB_USER = "zayed971"
_API = "https://api.github.com"
_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "AgentOS/1.0",
}


def _get(url):
    """GET a GitHub API URL. Returns parsed JSON or None on any error."""
    req = request.Request(url, headers=_HEADERS)
    try:
        with request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except error.HTTPError as e:
        if e.code == 404:
            return None
        return None
    except Exception:
        return None


def get_my_repos():
    """
    Fetch public repos for zayed971.

    Returns a list of dicts:
      name        repo name
      stars       stargazers count
      last_commit ISO 8601 timestamp of last push (or None)
      description repo description (or '')
    """
    data = _get(f"{_API}/users/{GITHUB_USER}/repos?per_page=100&sort=pushed")
    if not data:
        return []
    repos = []
    for r in data:
        repos.append({
            "name": r["name"],
            "stars": r.get("stargazers_count", 0),
            "last_commit": r.get("pushed_at"),   # e.g. "2024-03-01T12:00:00Z"
            "description": r.get("description") or "",
        })
    return repos


def check_repo_exists(repo_name):
    """Return True if zayed971/<repo_name> exists on GitHub."""
    return _get(f"{_API}/repos/{GITHUB_USER}/{repo_name}") is not None


def get_repo_readme(repo_name):
    """
    Fetch the raw README text for zayed971/<repo_name>.
    Returns the decoded string, or None if the repo / README does not exist.
    """
    data = _get(f"{_API}/repos/{GITHUB_USER}/{repo_name}/readme")
    if not data:
        return None
    raw = data.get("content", "")
    if raw:
        return base64.b64decode(raw).decode("utf-8", errors="ignore")
    return None


def stale_repos(days=7):
    """
    Return repos that have had no push in >= `days` days.
    Useful as a quick health check — these are the repos going dark.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stale = []
    for repo in get_my_repos():
        last = repo.get("last_commit")
        if not last:
            stale.append(repo)
            continue
        try:
            dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            if dt < cutoff:
                stale.append(repo)
        except ValueError:
            pass
    return stale


if __name__ == "__main__":
    print(f"Repos for {GITHUB_USER}:")
    for r in get_my_repos():
        print(f"  {r['name']:40s}  ★{r['stars']}  last: {r['last_commit']}")
    print()
    print("Stale (7+ days):")
    for r in stale_repos():
        print(f"  {r['name']}")
