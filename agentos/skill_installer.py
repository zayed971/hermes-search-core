#!/usr/bin/env python3
"""
AgentOS - Skill Installer

Searches GitHub for Claude Code SKILL.md files, downloads them into
~/.claude/skills/{name}/SKILL.md, and keeps a log in state/installed_skills.json.

Commands:
  python skill_installer.py search 'web scraping'
  python skill_installer.py install <url> <name>
  python skill_installer.py list
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state_layer as sl  # noqa: E402

# --- Paths -------------------------------------------------------------------
SKILLS_ROOT = Path.home() / ".claude" / "skills"
INSTALLED_LOG = sl.STATE_DIR / "installed_skills.json"

GITHUB_API = "https://api.github.com"
_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "agentos-skill-installer/1.0",
}
_token = os.environ.get("GITHUB_TOKEN")
if _token:
    _HEADERS["Authorization"] = f"Bearer {_token}"


# --- GitHub helpers ----------------------------------------------------------
def _gh_get(url):
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub {e.code}: {body[:300]}") from e


def _blob_to_raw(url):
    """Convert a GitHub blob page URL to a raw.githubusercontent.com URL."""
    if "raw.githubusercontent.com" in url:
        return url
    m = re.match(r"https://github\.com/([^/]+/[^/]+)/blob/([^/]+)/(.+)", url)
    if m:
        repo, ref, path = m.groups()
        return f"https://raw.githubusercontent.com/{repo}/{ref}/{path}"
    return url


def _fetch_with_branch_fallback(raw_url):
    """
    GET raw_url; if 404 and URL uses /main/, retry with /master/.
    Returns decoded content string or None on 404 (raises on other errors).
    """
    candidates = [raw_url]
    if "/main/" in raw_url:
        candidates.append(raw_url.replace("/main/", "/master/", 1))
    for candidate in candidates:
        req = urllib.request.Request(candidate, headers=_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise RuntimeError(f"Download failed ({e.code}): {candidate}") from e
    return None


def _resolve_skill_in_repo(owner_repo):
    """
    Use the GitHub Trees API to find the first SKILL.md in a public repo.
    Works without authentication. Returns raw URL or None.
    """
    branch = "main"
    try:
        info = _gh_get(f"{GITHUB_API}/repos/{owner_repo}")
        branch = info.get("default_branch", "main")
    except RuntimeError:
        pass
    try:
        tree = _gh_get(
            f"{GITHUB_API}/repos/{owner_repo}/git/trees/{branch}?recursive=1"
        )
        for item in tree.get("tree", []):
            if item.get("type") == "blob" and item.get("path", "").endswith("SKILL.md"):
                path = item["path"]
                return f"https://raw.githubusercontent.com/{owner_repo}/{branch}/{path}"
    except RuntimeError:
        pass
    return None


def _owner_repo_from_raw(raw_url):
    """Extract 'owner/repo' from a raw.githubusercontent.com URL."""
    m = re.match(r"https://raw\.githubusercontent\.com/([^/]+/[^/]+)/", raw_url)
    return m.group(1) if m else None


# --- search_skills -----------------------------------------------------------
def search_skills(query):
    """
    Search GitHub for SKILL.md files relevant to query.

    Two-pass strategy:
      1. Code search: filename:SKILL.md + query terms  (finds actual files).
      2. Repo search: topic:claude-skills + query       (catches tagged repos).

    Returns up to 10 dicts: {name, repo, stars, description, url, raw_url}.
    """
    results = []
    seen = set()

    # Pass 1: code search — pinpoints actual SKILL.md files
    q1 = urllib.parse.quote(f"{query} filename:SKILL.md")
    try:
        data = _gh_get(f"{GITHUB_API}/search/code?q={q1}&per_page=10")
        for item in data.get("items", []):
            html = item.get("html_url", "")
            raw = _blob_to_raw(html)
            if raw in seen:
                continue
            seen.add(raw)
            repo_obj = item.get("repository", {})
            results.append({
                "name": repo_obj.get("name", Path(item.get("name", "skill")).stem),
                "repo": repo_obj.get("full_name", "?"),
                "stars": repo_obj.get("stargazers_count", 0),
                "description": repo_obj.get("description") or "",
                "url": html,
                "raw_url": raw,
            })
    except RuntimeError as e:
        print(f"(code search unavailable: {e})", file=sys.stderr)

    # Pass 2: repo search — catches topic-tagged repos even if code quota is hit
    q2 = urllib.parse.quote(f"{query} topic:claude-skills")
    try:
        data2 = _gh_get(
            f"{GITHUB_API}/search/repositories?q={q2}&sort=stars&order=desc&per_page=8"
        )
        for item in data2.get("items", []):
            repo_full = item["full_name"]
            for skill_path in ("SKILL.md", "skills/SKILL.md"):
                raw = f"https://raw.githubusercontent.com/{repo_full}/main/{skill_path}"
                html = f"https://github.com/{repo_full}/blob/main/{skill_path}"
                if raw in seen:
                    continue
                seen.add(raw)
                results.append({
                    "name": item["name"],
                    "repo": repo_full,
                    "stars": item.get("stargazers_count", 0),
                    "description": item.get("description") or "",
                    "url": html,
                    "raw_url": raw,
                })
    except RuntimeError as e:
        print(f"(repo search unavailable: {e})", file=sys.stderr)

    results.sort(key=lambda r: r.get("stars", 0), reverse=True)
    return results[:10]


def print_search_results(results, query):
    if not results:
        print(f'\nNo skill results for: "{query}"')
        print("Tip: set GITHUB_TOKEN env var to raise GitHub rate limits.")
        return
    print(f'\nSkill search: "{query}"  ({len(results)} found)')
    print("=" * 64)
    for i, r in enumerate(results, 1):
        stars = f"*{r['stars']}" if r["stars"] else "*?"
        desc = r.get("description") or "(no description)"
        desc = (desc[:57] + "...") if len(desc) > 60 else desc
        print(f"[{i}] {r['name']}  {stars}  ({r['repo']})")
        print(f"    {desc}")
        print(f"    {r['url']}")
    print()
    print("Install:  python skill_installer.py install <url> <name>")


# --- install_skill -----------------------------------------------------------
def install_skill(url, name):
    """
    Download the SKILL.md at url and save to ~/.claude/skills/{name}/SKILL.md.
    Logs the install to state/installed_skills.json.
    Returns the installed Path.
    """
    raw = _blob_to_raw(url)
    content = _fetch_with_branch_fallback(raw)

    if content is None:
        # Direct path failed — try discovering the real SKILL.md via Trees API.
        owner_repo = _owner_repo_from_raw(raw)
        if owner_repo:
            resolved = _resolve_skill_in_repo(owner_repo)
            if resolved:
                print(f"(resolved via Trees API: {resolved})", file=sys.stderr)
                content = _fetch_with_branch_fallback(resolved)
                if content:
                    raw = resolved  # log the working URL

    if content is None:
        raise RuntimeError(f"Download failed (tried direct + Trees API): {raw}")

    if not content.strip():
        raise RuntimeError(f"Downloaded file is empty: {raw}")

    dest_dir = SKILLS_ROOT / name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "SKILL.md"
    dest.write_text(content, encoding="utf-8")

    _log_install(name, url, raw)
    print(f"Installed: {dest}")
    return dest


# --- list_installed ----------------------------------------------------------
def list_installed():
    log = _load_log()
    if not log:
        print("No skills installed yet.")
        print(f"Log: {INSTALLED_LOG}")
        return
    print(f"\nInstalled skills  ({len(log)} total)")
    print("=" * 64)
    for s in log:
        dest = SKILLS_ROOT / s["name"] / "SKILL.md"
        status = "OK" if dest.exists() else "MISSING"
        print(f"  [{status}] {s['name']}")
        print(f"         installed : {s.get('installed_at', '?')}")
        print(f"         source    : {s.get('url', '?')}")
    print()


# --- log helpers -------------------------------------------------------------
def _load_log():
    if not INSTALLED_LOG.exists():
        return []
    try:
        return json.loads(INSTALLED_LOG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save_log(log):
    INSTALLED_LOG.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")


def _log_install(name, url, raw_url):
    log = _load_log()
    for entry in log:
        if entry.get("name") == name:
            entry.update({"url": url, "raw_url": raw_url,
                          "installed_at": date.today().isoformat()})
            _save_log(log)
            return
    log.append({"name": name, "url": url, "raw_url": raw_url,
                 "installed_at": date.today().isoformat()})
    _save_log(log)


# --- Loop integration --------------------------------------------------------
def _move_terms(move_text):
    """Extract meaningful keywords from a move string."""
    return set(re.findall(r"[a-z0-9]+", move_text.lower())) - sl.STOPWORDS


def find_relevant_installed(move_text):
    """
    Return name of an already-installed skill whose name overlaps with the
    move keywords, or None if nothing matches.
    """
    terms = _move_terms(move_text)
    if not terms:
        return None
    for entry in _load_log():
        skill_words = set(re.findall(r"[a-z0-9]+", entry.get("name", "").lower()))
        if terms & skill_words and (SKILLS_ROOT / entry["name"] / "SKILL.md").exists():
            return entry["name"]
    return None


def auto_skill_for_move(move_text):
    """
    Called from loop.py after a GREENLIGHT verdict.

    1. Checks installed skills for a keyword match.
    2. If none found, searches GitHub and installs the top result.

    Returns the skill name (str) or None if nothing suitable was found.
    Never raises — errors are printed and swallowed so the loop continues.
    """
    existing = find_relevant_installed(move_text)
    if existing:
        print(f"SKILL    matched installed skill: {existing}")
        return existing

    print(f'SKILL    searching GitHub for skill: "{move_text}"')
    try:
        results = search_skills(move_text)
    except Exception as e:
        print(f"SKILL    search error: {e}")
        return None

    if not results:
        print("SKILL    no matching skills found on GitHub.")
        return None

    top = results[0]
    name_slug = re.sub(r"[^a-z0-9]+", "-", top["name"].lower()).strip("-")
    print(f"SKILL    auto-installing '{top['name']}' *{top.get('stars', 0)}"
          f" from {top['repo']}")
    try:
        install_skill(top["raw_url"], name_slug)
        return name_slug
    except Exception as e:
        print(f"SKILL    install failed: {e}")
        return None


# --- CLI ---------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="AgentOS Skill Installer")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("search", help="Search GitHub for a Claude Code skill")
    sp.add_argument("query", help="what you're looking for, e.g. 'web scraping'")

    si = sub.add_parser("install", help="Download and install a SKILL.md")
    si.add_argument("url", help="GitHub blob or raw URL of the SKILL.md")
    si.add_argument("name", help="local folder name for the skill")

    sub.add_parser("list", help="List all installed skills")

    args = ap.parse_args()

    if args.cmd == "search":
        try:
            results = search_skills(args.query)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        print_search_results(results, args.query)

    elif args.cmd == "install":
        try:
            install_skill(args.url, args.name)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.cmd == "list":
        list_installed()


if __name__ == "__main__":
    main()
