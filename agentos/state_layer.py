#!/usr/bin/env python3
"""
AgentOS - Stage 1: The State Layer

Two jobs, one file:
  1. CORPUS  (static, slow): make the world model (your dossiers + profile)
     queryable, so the rest of the system can reason over WHO YOU ARE.
  2. GROUND STATE (live, fast): hold the truth of the board RIGHT NOW, and
     - this is the part the old council lacked - let it be WRITTEN BACK to.

No embeddings. No API. No dependencies beyond the Python standard library.
That is a deliberate design choice, not a shortcut: Stage 1 must run instantly,
offline, for $0. Semantic search gets added later ONLY if keyword retrieval
proves insufficient. Evidence first, sophistication second.

Commands:
  python state_layer.py index                     Build/refresh the corpus catalog.
  python state_layer.py board                      Print the current board.
  python state_layer.py ask "your question"        Query the world model.
  python state_layer.py ship "<what>" --url U      Move something into 'shipped'.
  python state_layer.py pending "<what>" --trigger T   Add an item awaiting your hand.
  python state_layer.py resolve <index>            Mark a pending item done.
  python state_layer.py color green|yellow|red     Set your current capacity.
  python state_layer.py outcome "<trigger>" --pulled --moved   Record what happened.
"""

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

# --- Paths -------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
CORPUS_DIR = ROOT / "corpus"          # put your dossiers + profile here (or symlink them)
STATE_DIR = ROOT / "state"
GROUND_STATE_PATH = STATE_DIR / "ground_state.json"
MANIFEST_PATH = STATE_DIR / "corpus_manifest.json"

STATE_DIR.mkdir(parents=True, exist_ok=True)
CORPUS_DIR.mkdir(parents=True, exist_ok=True)

# --- Tokenisation ------------------------------------------------------------
STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "of", "to", "in", "on",
    "for", "with", "is", "are", "was", "were", "be", "been", "being", "this",
    "that", "these", "those", "it", "its", "as", "at", "by", "from", "he", "she",
    "they", "his", "her", "their", "you", "your", "i", "me", "my", "we", "our",
    "not", "no", "do", "does", "did", "has", "have", "had", "will", "would", "can",
    "could", "should", "what", "which", "who", "when", "where", "why", "how",
    "about", "into", "than", "more", "most", "some", "any", "all", "one", "two",
    "also", "up", "out", "so", "just",
}

IMPORTANCE_WEIGHTS = {
    "M_": 3.0,   # psychology
    "C_": 2.0,   # technical
    "I_": 2.0,   # internship/career
    "B_": 1.0,   # business
    "L_": 1.0,   # life
    "O_": 0.5,   # culture
    "Z_": 2.0,   # documentation
}


def _importance_weight(path_str):
    """Return score multiplier based on filename prefix convention."""
    filename = Path(path_str).name
    for prefix, w in IMPORTANCE_WEIGHTS.items():
        if filename.startswith(prefix):
            return w
    return 1.0


def tokenize(text):
    """Lowercase, split into word tokens, drop short words and stopwords."""
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    english_tokens = [t for t in tokens if len(t) > 2 and t not in STOPWORDS]
    arabic_tokens = re.findall(r'[؀-ۿ]+', text)
    return english_tokens + arabic_tokens


# --- Corpus: catalog ---------------------------------------------------------
def iter_corpus_files():
    for path in sorted(CORPUS_DIR.rglob("*")):
        if path.suffix.lower() in {".md", ".txt"} and path.is_file():
            yield path


def extract_title(text, fallback):
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return fallback


def extract_headings(text):
    headings = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("##"):
            headings.append(s.lstrip("#").strip())
    return headings


def build_manifest():
    """Scan the corpus folder and write a catalog the system can read fast."""
    entries = []
    for path in iter_corpus_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        rel = str(path.relative_to(CORPUS_DIR))
        title = extract_title(text, rel)
        headings = extract_headings(text)
        body_tokens = tokenize(text)
        top_terms = [t for t, _ in Counter(body_tokens).most_common(15)]
        entries.append({
            "path": rel,
            "title": title,
            "headings": headings,
            "tokens": len(body_tokens),
            "top_terms": top_terms,
        })
    manifest = {
        "built": datetime.now().isoformat(timespec="seconds"),
        "file_count": len(entries),
        "files": entries,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def load_manifest():
    if not MANIFEST_PATH.exists():
        return build_manifest()
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


# --- Corpus: retrieval -------------------------------------------------------
def score_text(query_terms, text, title, headings):
    """A small keyword relevance score. Title/heading hits weigh more than body."""
    body = Counter(tokenize(text))
    if not body:
        return 0.0
    title_set = set(tokenize(title))
    head_set = set(tokenize(" ".join(headings)))
    score = 0.0
    for term in query_terms:
        score += body.get(term, 0)
        if term in title_set:
            score += 5
        if term in head_set:
            score += 3
    # dampen by length so long files don't win purely on size
    return score / (len(body) ** 0.5)


def split_chunks(text):
    """Split a document into chunks at blank lines."""
    chunks, current = [], []
    for line in text.splitlines():
        if line.strip() == "":
            if current:
                chunks.append("\n".join(current).strip())
                current = []
        else:
            current.append(line)
    if current:
        chunks.append("\n".join(current).strip())
    return [c for c in chunks if c]


def best_excerpts(query_terms, text, max_chars=750):
    """Return the most relevant passage(s) from a document."""
    chunks = split_chunks(text)
    scored = []
    for ch in chunks:
        c_tokens = Counter(tokenize(ch))
        s = sum(c_tokens.get(t, 0) for t in query_terms)
        if s > 0:
            scored.append((s, ch))
    scored.sort(key=lambda x: x[0], reverse=True)
    out, used = [], 0
    for _, ch in scored:
        if used + len(ch) > max_chars and out:
            break
        out.append(ch)
        used += len(ch)
        if used >= max_chars:
            break
    return out


def ask(question, top_k=3):
    query_terms = set(tokenize(question))
    if not query_terms:
        print("Ask a real question with some keywords in it.")
        return
    manifest = load_manifest()
    results = []
    for entry in manifest["files"]:
        path = CORPUS_DIR / entry["path"]
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        s = score_text(query_terms, text, entry["title"], entry["headings"])
        if s > 0:
            s *= _importance_weight(entry["path"])
            results.append((s, entry, text))
    results.sort(key=lambda x: x[0], reverse=True)

    if not results:
        print(f'No matches in the corpus for: "{question}"')
        print("(Did you put your dossiers/profile in corpus/ and run `index`?)")
        return

    print(f'\nWORLD MODEL  -  query: "{question}"')
    print("=" * 64)
    for rank, (s, entry, text) in enumerate(results[:top_k], 1):
        print(f"\n[{rank}] {entry['title']}")
        print(f"    file: {entry['path']}   (relevance {s:.2f})")
        for ex in best_excerpts(query_terms, text):
            snippet = ex if len(ex) <= 750 else ex[:750] + " ..."
            print("\n".join("    " + ln for ln in snippet.splitlines()))
    print()


# --- Ground state: load / save ----------------------------------------------
def load_state():
    if not GROUND_STATE_PATH.exists():
        print(f"No ground_state.json at {GROUND_STATE_PATH}")
        print("A starter file should have shipped with Stage 1. Create it first.")
        sys.exit(1)
    return json.loads(GROUND_STATE_PATH.read_text(encoding="utf-8"))


def _write_locked(path, text):
    """Write text to path with OS-appropriate file locking to prevent corruption."""
    data = text.encode("utf-8")
    try:
        if sys.platform == "win32":
            import msvcrt
            with open(path, "wb") as f:
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                f.write(data)
                f.flush()
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            with open(path, "wb") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.write(data)
                f.flush()
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        path.write_bytes(data)


def save_state(state):
    state["last_updated"] = datetime.now().isoformat(timespec="seconds")
    _write_locked(GROUND_STATE_PATH, json.dumps(state, indent=2))


def parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def days_until(date_str):
    d = parse_date(date_str)
    return None if d is None else (d - date.today()).days


def age_days(date_str):
    d = parse_date(date_str)
    return None if d is None else (date.today() - d).days


# --- Ground state: display ---------------------------------------------------
def show_board():
    s = load_state()
    line = "=" * 64
    print(f"\n{line}\nTHE BOARD  -  {date.today().isoformat()}\n{line}")

    goal = s.get("goal", {})
    if goal:
        print(f"\nNORTH STAR: {goal.get('north_star', '(unset)')}")
        if goal.get("active_objective"):
            print(f"NOW:        {goal['active_objective']}")
        if goal.get("stakes"):
            print(f"STAKES:     {goal['stakes']}")

    print(f"\nCAPACITY:   {s.get('state_color', 'unknown').upper()}")

    deadlines = s.get("deadlines", [])
    if deadlines:
        print("\nDEADLINES")
        for d in sorted(deadlines, key=lambda x: x.get("date", "9999")):
            left = days_until(d.get("date"))
            if left is None:
                tag = "?"
            elif left > 0:
                tag = f"{left}d left"
            elif left == 0:
                tag = "TODAY"
            else:
                tag = f"{-left}d past"
            print(f"  - {d.get('what', '?'):<42} {d.get('date', '?')}  ({tag})")

    shipped = s.get("shipped", [])
    print(f"\nSHIPPED ({len(shipped)})  -  exists in the world")
    for item in shipped:
        seen = item.get("seen_by") or []
        seen_tag = f"seen by: {', '.join(seen)}" if seen else "NOT YET seen by anyone who matters"
        print(f"  + {item.get('what', '?')}")
        if item.get("url"):
            print(f"      {item['url']}")
        print(f"      {seen_tag}")

    pending = s.get("pending", [])
    print(f"\nPENDING ({len(pending)})  -  built, waiting on YOUR hand")
    for i, item in enumerate(pending):
        age = age_days(item.get("created"))
        age_tag = f"   [{age}d old]" if age is not None else ""
        print(f"  [{i}] {item.get('what', '?')}{age_tag}")
        if item.get("blocked_on"):
            print(f"      blocked on: {item['blocked_on']}")
        if item.get("trigger"):
            print(f"      TRIGGER:    {item['trigger']}")

    owed = s.get("owed", [])
    if owed:
        print(f"\nOWED ({len(owed)})  -  humans waiting on you")
        for item in owed:
            age = age_days(item.get("since"))
            age_tag = f"   [{age}d]" if age is not None else ""
            print(f"  - {item.get('who', '?')}: {item.get('what', '?')}{age_tag}")

    last = s.get("last_outcome", {})
    if last:
        print(f"\nLAST TRIGGER: {last.get('trigger_issued', '(none)')}")
        print(f"  pulled: {'YES' if last.get('did_you_pull_it') else 'NO'}"
              f"    ground moved: {'YES' if last.get('ground_moved') else 'NO'}")
        if last.get("notes"):
            print(f"  note: {last['notes']}")
    print()


# --- Ground state: mutations (the write-back path) ---------------------------
def cmd_ship(what, url=None, seen_by=None):
    s = load_state()
    s.setdefault("shipped", []).append({
        "what": what,
        "url": url,
        "date": date.today().isoformat(),
        "seen_by": [x.strip() for x in seen_by.split(",")] if seen_by else [],
    })
    save_state(s)
    print(f"SHIPPED: {what}")


def cmd_pending(what, trigger=None, blocked_on=None, where=None):
    s = load_state()
    s.setdefault("pending", []).append({
        "what": what,
        "where": where,
        "blocked_on": blocked_on,
        "trigger": trigger,
        "created": date.today().isoformat(),
    })
    save_state(s)
    print(f"PENDING added: {what}")


def cmd_resolve(index):
    s = load_state()
    pending = s.get("pending", [])
    if index < 0 or index >= len(pending):
        print(f"No pending item at index {index}. Run `board` to see indices.")
        return
    item = pending.pop(index)
    save_state(s)
    print(f"Resolved: {item.get('what', '?')}")
    print("If this produced something the world can see, record it with `ship`.")


def cmd_color(value):
    if value not in {"green", "yellow", "red"}:
        print("Color must be green, yellow, or red.")
        return
    s = load_state()
    s["state_color"] = value
    save_state(s)
    print(f"Capacity set to {value.upper()}.")


def cmd_outcome(trigger, pulled, moved, notes=None):
    s = load_state()
    s["last_outcome"] = {
        "trigger_issued": trigger,
        "did_you_pull_it": pulled,
        "ground_moved": moved,
        "notes": notes,
        "date": date.today().isoformat(),
    }
    save_state(s)
    print("Outcome recorded. This is the one signal the system learns from.")


# --- CLI ---------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="AgentOS Stage 1 - the State Layer")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("index", help="Build/refresh the corpus catalog")
    sub.add_parser("board", help="Print the current board")

    p_ask = sub.add_parser("ask", help="Query the world model")
    p_ask.add_argument("question")
    p_ask.add_argument("--top", type=int, default=3)

    p_ship = sub.add_parser("ship", help="Record something shipped")
    p_ship.add_argument("what")
    p_ship.add_argument("--url")
    p_ship.add_argument("--seen-by")

    p_pend = sub.add_parser("pending", help="Add a pending item")
    p_pend.add_argument("what")
    p_pend.add_argument("--trigger")
    p_pend.add_argument("--blocked-on")
    p_pend.add_argument("--where")

    p_res = sub.add_parser("resolve", help="Mark a pending item done")
    p_res.add_argument("index", type=int)

    p_col = sub.add_parser("color", help="Set capacity: green|yellow|red")
    p_col.add_argument("value")

    p_out = sub.add_parser("outcome", help="Record the last trigger outcome")
    p_out.add_argument("trigger")
    p_out.add_argument("--pulled", action="store_true")
    p_out.add_argument("--moved", action="store_true")
    p_out.add_argument("--notes")

    args = parser.parse_args()

    if args.cmd == "index":
        m = build_manifest()
        print(f"Indexed {m['file_count']} files from {CORPUS_DIR}")
    elif args.cmd == "board":
        show_board()
    elif args.cmd == "ask":
        ask(args.question, top_k=args.top)
    elif args.cmd == "ship":
        cmd_ship(args.what, url=args.url, seen_by=args.seen_by)
    elif args.cmd == "pending":
        cmd_pending(args.what, trigger=args.trigger,
                    blocked_on=args.blocked_on, where=args.where)
    elif args.cmd == "resolve":
        cmd_resolve(args.index)
    elif args.cmd == "color":
        cmd_color(args.value)
    elif args.cmd == "outcome":
        cmd_outcome(args.trigger, args.pulled, args.moved, notes=args.notes)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
