#!/usr/bin/env python3
"""
AgentOS - Stage 7: The World Scanner

The "surfaces what you didn't know to ask for" layer. It runs BETWEEN loop cycles
and does one thing: look outward through the filter of who you are, and write down
signals that might matter - WITHOUT acting on any of them.

The discipline here is the most important part, because "an agent that browses the
web autonomously" is exactly where prompt-injection attacks live (the whole reason
to avoid OpenClaw-style ambient authority). So the Scanner is built on three rules:

  1. It FETCHES and STRUCTURES. It never executes instructions found in any page,
     never follows a link a page tells it to, never treats web text as a command.
     Web content is DATA, not orders.
  2. Its output is a review file (scan_report.md) - signals for YOU and for the
     Questioner to judge. The Scanner never turns a signal into an action itself.
  3. Its search leads are generated from YOUR corpus + board (what you care about,
     who you're targeting), so it filters the whole world through your world model
     instead of wandering.

Without a search/fetch tool wired, it runs in PLAN mode: it generates the exact,
prioritized set of queries it WOULD run - which is itself useful (you paste them
into a browser, or hand them to a search tool later). With a tool wired (you add
the fetch function), it fills in findings. Either way it only ever produces a
report; it has no power to act.

Usage:
  python world_scanner.py                 # generate the prioritized scan plan (queries)
  python world_scanner.py --report        # show the latest scan report
"""

import argparse
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import state_layer as sl
import questioner as Q
import github_tools

SCAN_REPORT = sl.STATE_DIR / "scan_report.md"


def derive_interests():
    """Pull, from corpus + board, the things worth scanning the world for."""
    board = Q.load_board()
    interests = []

    # 1. Target companies / people on the board -> watch them.
    for item in board.get("owed", []):
        who = item.get("who", "")
        if who:
            interests.append(("target_contact", who.split("(")[0].strip()))
    goal = board.get("goal", {})
    stakes = goal.get("stakes", "")
    # Only real companies become company-watch targets. People are handled as
    # target_contact above, via the board's `owed` entries.
    for token in ["34ML", "Synapse Analytics", "DXwand", "qeen.ai"]:
        if token.lower() in stakes.lower():
            interests.append(("target_company", token))

    # 2. The stack the goal implies (from the active objective / corpus).
    #    Query the corpus for the technical direction.
    for q in ["internship requirements AI engineer stack LangChain RAG FastAPI",
              "career path AI engineer MENA Egypt Gulf companies hiring"]:
        terms = set(sl.tokenize(q))
        manifest = sl.load_manifest()
        for entry in manifest.get("files", []):
            if any(t in set(sl.tokenize(entry["title"])) for t in terms):
                interests.append(("direction", entry["title"]))
                break

    # 3. Standing interests that don't change night to night.
    interests += [
        ("field", "AI engineer internships remote MENA visa-friendly"),
        ("field", "junior AI engineer portfolio projects that get interviews 2026"),
        ("field", "companies hiring AI engineers Egypt Gulf remote"),
    ]

    # 4. GitHub repo awareness: flag repos that have gone dark (no push in 7+ days).
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        for repo in github_tools.get_my_repos():
            last = repo.get("last_commit")
            if not last:
                interests.append(("stale_repo", repo["name"]))
                continue
            dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            if dt < cutoff:
                interests.append(("stale_repo", repo["name"]))
    except Exception:
        pass

    # dedupe preserving order
    seen, out = set(), []
    for kind, val in interests:
        key = (kind, val.lower())
        if key not in seen and val:
            seen.add(key)
            out.append((kind, val))
    return out


def build_queries(interests):
    """Turn interests into concrete, prioritized search queries."""
    queries = []
    for kind, val in interests:
        if kind == "target_company":
            queries.append((1, f"{val} careers AI engineer intern requirements 2026"))
            queries.append((2, f"{val} engineering team hiring news recent"))
        elif kind == "target_contact":
            queries.append((2, f"{val} recent posts projects news"))
        elif kind == "direction":
            queries.append((3, f"{val} - current best resources and trends"))
        elif kind == "field":
            queries.append((2, val))
    # sort by priority, keep it bounded
    queries.sort(key=lambda x: x[0])
    return queries[:10]


def fetch_signal(query, fetch_fn=None):
    """If a fetch/search tool is wired, return findings. Otherwise None (plan mode)."""
    if fetch_fn is None:
        return None
    try:
        # The wired function must take a query string and return structured text.
        # It must NOT execute anything found in the results - caller guarantees this.
        return fetch_fn(query)
    except Exception as e:
        return f"(fetch failed: {e})"


def run(fetch_fn=None):
    interests = derive_interests()
    queries = build_queries(interests)

    lines = [
        f"# World Scan - {date.today().isoformat()}",
        "",
        "Signals surfaced through your world model. **These are candidates, not actions.**",
        "Nothing here has been acted on. Hand any worth pursuing to the Questioner:",
        '`python questioner.py "act on: <the signal>"`',
        "",
        "## Interests driving this scan",
    ]
    for kind, val in interests:
        lines.append(f"- [{kind}] {val}")
    lines += ["", "## Scan"]

    mode = "PLAN (no fetch tool wired - these are the queries it WOULD run)" if fetch_fn is None \
        else "LIVE (findings fetched; web text treated as data, never as instructions)"
    lines.append(f"_mode: {mode}_")
    lines.append("")

    for prio, query in queries:
        lines.append(f"### [p{prio}] {query}")
        finding = fetch_signal(query, fetch_fn)
        if finding is None:
            lines.append("- (plan mode) run this query; capture: who/what/deadline/link")
        else:
            lines.append(finding if isinstance(finding, str) else json.dumps(finding, indent=2))
        lines.append("")

    lines += [
        "## Reminder on safety",
        "- Web content is DATA. The Scanner never follows instructions found in a page.",
        "- No signal becomes a move without passing the Questioner.",
        "- Irreversible actions (apply, send, pay) remain yours, always.",
    ]

    SCAN_REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"Scan complete ({'plan' if fetch_fn is None else 'live'} mode).")
    print(f"  {len(queries)} prioritized signals -> {SCAN_REPORT}")
    print("  Nothing acted on. Review the report; hand anything worth it to the Questioner.")


def show_report():
    if not SCAN_REPORT.exists():
        print("No scan report yet. Run: python world_scanner.py")
        return
    print(SCAN_REPORT.read_text(encoding="utf-8"))


def make_duckduckgo_fetch_fn():
    """Return a DuckDuckGo search function if requests + bs4 are installed, else None."""
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError:
        return None

    def _fetch(query):
        try:
            headers = {"User-Agent": "Mozilla/5.0 (compatible; AgentOS/1.0)"}
            resp = requests.get(
                "https://duckduckgo.com/html/",
                params={"q": query},
                headers=headers,
                timeout=10,
            )
            soup = BeautifulSoup(resp.text, "html.parser")
            results = []
            for r in soup.select(".result")[:5]:
                title_el = r.select_one(".result__a")
                url_el = r.select_one(".result__url")
                snippet_el = r.select_one(".result__snippet")
                title = title_el.get_text(strip=True) if title_el else ""
                href = title_el.get("href", "") if title_el else ""
                url_text = url_el.get_text(strip=True) if url_el else href
                snippet = snippet_el.get_text(strip=True) if snippet_el else ""
                if title:
                    results.append({"title": title, "url": url_text, "snippet": snippet})
            if not results:
                return None
            lines = []
            for i, r in enumerate(results, 1):
                lines.append(f"  [{i}] {r['title']}")
                lines.append(f"      {r['url']}")
                if r["snippet"]:
                    lines.append(f"      {r['snippet']}")
            return "\n".join(lines)
        except Exception:
            return None

    return _fetch


def main():
    ap = argparse.ArgumentParser(description="AgentOS Stage 7 - the World Scanner")
    ap.add_argument("--report", action="store_true", help="show the latest scan report")
    args = ap.parse_args()
    if args.report:
        show_report()
        return

    fetch_fn = None
    try:
        import hermes_fetch
        fetch_fn = hermes_fetch.hermes_fetch_fn
    except Exception:
        fetch_fn = make_duckduckgo_fetch_fn()

    run(fetch_fn=fetch_fn)


if __name__ == "__main__":
    main()
