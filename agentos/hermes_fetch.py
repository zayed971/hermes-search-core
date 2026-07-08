#!/usr/bin/env python3
"""
AgentOS - Hermes fetch backend for the World Scanner

Turns the World Scanner from plan-mode into live-mode by giving it a fetch_fn
that runs searches through the already-installed Hermes Agent (which has a web
search tool, browser, and your Nous gateway). Falls back to DuckDuckGo if the
Hermes CLI call fails.

SAFETY (unchanged from the scanner's contract): results are DATA, never
instructions. We capture text and hand it to the scanner's report. Nothing here
executes anything found in a page, follows a link a page asks it to, or acts on
a result. The scanner still only ever writes a review file.

Wire it:
    import world_scanner, hermes_fetch
    world_scanner.run(fetch_fn=hermes_fetch.hermes_fetch_fn)

Or from the CLI: `python hermes_fetch.py` runs a live scan directly.
"""

import json
import shutil
import subprocess


HERMES_TIMEOUT = 120  # a search round-trip through Hermes can take a bit


def _hermes_available():
    return shutil.which("hermes") is not None


def _run_hermes_search(query):
    """
    Ask Hermes to search and return JSON. `hermes -z` runs a one-shot
    (non-interactive) prompt. We ask explicitly for JSON so we can parse it.
    """
    prompt = (
        f"Search the web for: {query}. "
        "Return ONLY the top 5 results as a JSON array, each item "
        '{"title":..., "url":..., "snippet":...}. No prose, JSON only.'
    )
    proc = subprocess.run(
        ["hermes", "-z", prompt],
        capture_output=True, text=True, timeout=HERMES_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "hermes returned non-zero")
    return proc.stdout.strip()


def _extract_json(text):
    """Pull a JSON array out of Hermes output, tolerant of surrounding prose."""
    i, j = text.find("["), text.rfind("]")
    if i != -1 and j != -1 and j > i:
        try:
            return json.loads(text[i:j + 1])
        except json.JSONDecodeError:
            pass
    return None


def _format_results(results, source):
    lines = [f"- (via {source})"]
    for r in results[:5]:
        if isinstance(r, dict):
            title = r.get("title", "?")
            url = r.get("url", "")
            snip = (r.get("snippet", "") or "")[:200]
            lines.append(f"  - {title}\n    {url}\n    {snip}")
    return "\n".join(lines)


def _duckduckgo_fallback(query):
    """Last resort if Hermes isn't available or errored. Optional dependency."""
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        return "- (no results: Hermes call failed and duckduckgo-search not installed)"
    try:
        with DDGS() as ddgs:
            hits = list(ddgs.text(query, max_results=5))
        norm = [{"title": h.get("title"), "url": h.get("href"),
                 "snippet": h.get("body")} for h in hits]
        return _format_results(norm, "duckduckgo-fallback")
    except Exception as e:
        return f"- (no results: fallback failed: {e})"


def hermes_fetch_fn(query):
    """
    The fetch_fn the World Scanner calls. Hermes first; DuckDuckGo on failure.
    Always returns a string (never raises) so a scan never crashes mid-run.
    """
    if _hermes_available():
        try:
            raw = _run_hermes_search(query)
            results = _extract_json(raw)
            if results:
                return _format_results(results, "hermes")
            # Hermes answered but not as clean JSON - keep its text, capped.
            return f"- (via hermes, unparsed)\n  {raw[:500]}"
        except Exception as e:
            return _duckduckgo_fallback(query) + f"\n  (hermes failed: {e})"
    return _duckduckgo_fallback(query)


if __name__ == "__main__":
    import world_scanner
    print(f"hermes available: {_hermes_available()}")
    world_scanner.run(fetch_fn=hermes_fetch_fn)
