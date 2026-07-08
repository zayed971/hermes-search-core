#!/usr/bin/env python3
"""
PHASE 2 — Full Anti-Hallucination Search Pipeline
===================================================
7 stages: Decompose → Search → Extract → Dedup → Verify → Synthesize → Sanity

Usage:
  python phase2_pipeline.py "query"          # full verification
  python phase2_pipeline.py "query" --json    # JSON output (clean stdout)
  python phase2_pipeline.py "query" --no-verify  # skip Claude, fast
"""

import os, sys, re, json, time, hashlib
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import List, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed


# ═══ CONFIG ═══

def _load_key(name: str) -> str:
    val = os.environ.get(name, "")
    if val and len(val) > 10: return val
    env = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(env):
        with open(env) as f:
            for line in f:
                if name in line and not line.strip().startswith("#"):
                    p = line.strip().split("=", 1)
                    if len(p) == 2 and len(p[1]) > 10: return p[1]
    return ""

DEEPSEEK_KEY = _load_key("DEEPSEEK_API_KEY")
CLAUDE_KEY   = _load_key("ANTHROPIC_API_KEY")
CLAUDE_MODEL = "claude-sonnet-4-5-20250929"
HAIKU_MODEL  = "claude-haiku-4-5-20251001"
CACHE_DIR     = os.path.expanduser("~/.hermes/cache/search")  # 24h cache

# ═══ CACHE ═══

class SearchCache:
    """File-based 24-hour cache. Same query = instant return, no API."""
    def __init__(self):
        self.dir = Path(CACHE_DIR); self.dir.mkdir(parents=True, exist_ok=True)
    def _key(self, q): return hashlib.sha256(q.strip().lower().encode()).hexdigest()[:16]
    def get(self, q):
        f = self.dir / f"{self._key(q)}.json"
        if not f.exists(): return None
        if time.time() - f.stat().st_mtime > 86400: return None  # 24h
        try:
            data = json.loads(f.read_text())
            return [SearchHit(**d) for d in data]
        except: return None
    def set(self, q, hits):
        if not hits: return
        try:
            (self.dir / f"{self._key(q)}.json").write_text(
                json.dumps([{"query":h.query,"url":h.url,"title":h.title,
                "snippet":h.snippet,"method":h.method} for h in hits]))
        except: pass
_cache = SearchCache()  # global singleton

AUTHORITY = {
    "gov":10,"edu":9,"org_official":9,"academic":8,
    "major_pub":7,"tech_corp":6,"blog_expert":5,"forum":3,"unknown":1}
CONTENT_FARMS = {"career209.com","egyincs.com","egyptfwd.com",
    "wuzzuf.net/blog","linkedin.com/pulse","forbes.com/sites"}
MAX_CLAIMS_AFTER_DEDUP = 15


# ═══ DATA ═══

@dataclass
class Claim:
    text: str; source_url: str = ""; source_title: str = ""
    source_tier: str = "unknown"; extraction_method: str = "pattern"
    verification_status: str = "unverified"; verification_notes: str = ""
    confidence: float = 0.0; simhash: str = ""
    authority_score: int = 1; is_content_farm: bool = False

@dataclass
class SearchHit:
    query: str; url: str; title: str; snippet: str; method: str = "web"

@dataclass
class Phase2Result:
    query: str; answer: str; confidence: float
    verified_claims: int; total_claims: int; deduped_out: int
    sources: List[Dict]; warnings: List[str]; sanity_flags: List[str]
    elapsed: float; cost_estimate: str


# ═══ HELPERS ═══

def _classify_url(url: str) -> str:
    u = url.lower()
    if ".gov" in u: return "gov"
    if ".edu" in u: return "edu"
    if any(d in u for d in ["who.int","un.org","worldbank.org"]): return "org_official"
    if any(d in u for d in ["arxiv.org","pubmed","ieee.org"]): return "academic"
    if any(d in u for d in ["reuters.com","bloomberg.com","nytimes.com","wsj.com"]): return "major_pub"
    if any(d in u for d in ["microsoft.com","google.com","apple.com","github.com"]): return "tech_corp"
    if "reddit.com" in u or "stackoverflow.com" in u: return "forum"
    if "blog" in u or "medium.com" in u: return "blog_expert"
    return "unknown"

def _is_content_farm(url: str) -> bool:
    return any(farm in url.lower() for farm in CONTENT_FARMS)

def _mini_hash(text: str) -> str:
    text = re.sub(r'\s+', ' ', text.lower().strip())
    grams = [text[i:i+3] for i in range(max(0, len(text)-2))]
    if not grams: return hashlib.sha256(text.encode()).hexdigest()[:16]
    hashes = sorted(int(hashlib.sha256(g.encode()).hexdigest()[:8], 16) for g in grams)[:8]
    return "|".join(str(h) for h in hashes)

def _simhash_similarity(a: str, b: str) -> float:
    if not a or not b: return 0.0
    sa, sb = set(a.split("|")), set(b.split("|"))
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


# ═══ STAGE 1: DECOMPOSE ═══

def decompose(query: str) -> List[str]:
    q, ql = query.strip().rstrip("?"), query.strip().lower()
    m = re.search(r'compare\s+(.+?)\s+(?:and|vs\.?|versus)\s+(.+?)$', ql)
    if m: return [f"What is {m.group(1)}?", f"What is {m.group(2)}?",
                   f"Key differences: {m.group(1)} vs {m.group(2)}",
                   f"When to choose {m.group(1)} over {m.group(2)}"]
    if ql.startswith(("how to ","how do ","how can ")):
        t = re.sub(r'^how\s+(?:to|do|can|should)\s+','',ql)
        return [f"Step-by-step: {t}", f"Prerequisites: {t}", f"Common mistakes: {t}"]
    m = re.search(r'^(?:what|who)\s+(?:is|are|was|were)\s+(.+?)$', ql)
    if m: return [f"Definition: {m.group(1)}", f"Key facts: {m.group(1)}", f"Latest: {m.group(1)}"]
    if any(w in ql for w in ["latest","recent","now","current","2025","2026"]):
        return [f"Latest: {q}", f"Overview: {q}", f"Expert opinions: {q}"]
    # LLM fallback for complex queries patterns can't handle
    if DEEPSEEK_KEY:
        try:
            import openai
            client = openai.OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com")
            resp = client.chat.completions.create(model="deepseek-chat",
                messages=[{"role":"user","content":f"Break into 2-3 sub-queries:\n{q}\n\nJSON: {{\"queries\":[\"...\"]}}"}],
                temperature=0.1, max_tokens=300)
            m = re.search(r'\[.*\]', resp.choices[0].message.content, re.DOTALL)
            if m:
                subs = json.loads(m.group())
                if isinstance(subs, list) and len(subs) > 0:
                    return subs[:3]
        except: pass
    return [q]


# ═══ STAGE 2: SEARCH ═══

class Searcher:
    def __init__(self): self._ddgs = None
    def _get(self):
        if self._ddgs is None:
            try: from ddgs import DDGS; self._ddgs = DDGS()
            except ImportError: self._ddgs = False
        return self._ddgs if self._ddgs is not False else None
    def search(self, queries, max_per=5):
        hits = []
        with ThreadPoolExecutor(max_workers=3) as ex:
            fs = {ex.submit(self._one, q, max_per): q for q in queries}
            for f in as_completed(fs):
                try: hits.extend(f.result())
                except Exception as e: print(f"  [SEARCH] {e}", file=sys.stderr)
        return hits
    def _one(self, q, n):
        d = self._get()
        if not d: return []
        for attempt in range(3):
            try:
                if attempt > 0: time.sleep(3 * attempt)  # backoff
                return [SearchHit(query=q, url=r.get("href",""), title=r.get("title","Untitled"),
                        snippet=r.get("body",""), method="ddgs")
                        for r in d.text(q, max_results=n, timelimit=10)]
            except Exception as e:
                if attempt < 2:
                    print(f"  [SEARCH] retry {attempt+1}/3: {q[:40]}", file=sys.stderr)
                else:
                    print(f"  [SEARCH] FAILED '{q[:40]}': {e}", file=sys.stderr)
        return []


# ═══ LOCAL SEARCH (Phase 4) ═══

LOCAL_DIRS = [
    "/mnt/c/Users/HP/Desktop",
    "/mnt/c/Users/HP/Desktop/ai company hermes",
    "/mnt/c/Users/HP/Desktop/hermes_verify_systems",
    "/mnt/c/Users/HP/Desktop/hermes_core",
    "/mnt/c/Users/HP/Desktop/transcripts_batch2",
    "/mnt/c/Users/HP/Desktop/internships",
    "/mnt/c/Users/HP/Desktop/applications",
]
LOCAL_EXTENSIONS = "*.{md,txt,py,json}"

class LocalSearcher:
    """Search Desktop documents with ripgrep. No API cost, instant."""
    def __init__(self):
        self._rg = None
        try:
            import subprocess; subprocess.run(["rg","--version"], capture_output=True, check=True)
            self._rg = True
        except: self._rg = False

    def search(self, queries, max_per=5):
        if not self._rg: return []
        hits = []
        for q in queries:
            hits.extend(self._one(q, max_per))
        return hits

    def _one(self, query, n):
        terms = self._terms(query)
        if not terms: return []
        hits = []
        try:
            import subprocess
            cmd = ["rg","-l","-i","--max-count","1","--max-filesize","2M",
                   "-g",LOCAL_EXTENSIONS, terms[0]]
            for d in LOCAL_DIRS:
                import os
                if not os.path.isdir(d): continue
                try:
                    r = subprocess.run(cmd+[d], capture_output=True, text=True, timeout=15)
                    if r.returncode == 0 and r.stdout.strip():
                        files = r.stdout.strip().split("\n")[:n]
                        for f in files:
                            snippet = self._snippet(f, terms)
                            title = os.path.basename(f)
                            hits.append(SearchHit(query=query, url=f"file://{f}",
                                title=f"📁 {title}", snippet=snippet, method="local"))
                except: continue
            return hits[:n]
        except Exception as e:
            print(f"  [LOCAL] {e}", file=sys.stderr); return []

    def _terms(self, query):
        stop = {"the","a","an","is","are","was","were","be","to","of","in","for","on","with",
                "at","by","from","as","and","or","but","if","it","its","i","we","you","they",
                "he","she","how","what","who","when","where","why","this","that","these","those"}
        import re
        words = re.findall(r'\b[a-zA-Z]{3,}\b', query.lower())
        return [w for w in words if w not in stop][:5]

    def _snippet(self, filepath, terms):
        try:
            with open(filepath, errors="ignore") as f:
                content = f.read()[:5000]
            # Find first term match and return surrounding context
            lower = content.lower()
            for t in terms:
                idx = lower.find(t)
                if idx >= 0:
                    start = max(0, idx-200)
                    end = min(len(content), idx+500)
                    return content[start:end]
            return content[:500]
        except: return ""


# ═══ STAGE 3: EXTRACT ═══

class Extractor:
    def __init__(self):
        self._client = None
        if DEEPSEEK_KEY:
            try: import openai; self._client = openai.OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com")
            except ImportError: pass
    def extract(self, hits):
        claims = []
        for h in hits: claims.extend(self._from_hit(h))
        return self._dedup(claims)
    def _from_hit(self, h):
        claims, tier, text = [], _classify_url(h.url), h.snippet
        for m in re.finditer(r'([A-Z][^.]{10,120})\s+is\s+([^.]{10,200})\.', text):
            claims.append(Claim(text=f"{m.group(1)} is {m.group(2)}", source_url=h.url,
                source_title=h.title, source_tier=tier, extraction_method="pattern"))
        for m in re.finditer(r'(In (?:19|20)\d{2}[^.]{20,250})\.', text):
            claims.append(Claim(text=m.group(1), source_url=h.url,
                source_title=h.title, source_tier=tier, extraction_method="pattern"))
        if len(claims) < 2 and self._client and len(text) > 80:
            claims.extend(self._llm_extract(h))
        return claims[:5]
    def _llm_extract(self, h):
        try:
            resp = self._client.chat.completions.create(model="deepseek-chat",
                messages=[{"role":"user","content":f"Extract 2-4 verifiable claims:\n\n{h.snippet[:2000]}\n\nJSON: {{\"claims\":[{{\"text\":\"...\"}}]}}"}],
                temperature=0.1, max_tokens=800)
            m = re.search(r'\{.*\}', resp.choices[0].message.content, re.DOTALL)
            if m:
                return [Claim(text=c["text"], source_url=h.url, source_title=h.title,
                    source_tier=_classify_url(h.url), extraction_method="llm")
                    for c in json.loads(m.group()).get("claims",[])]
        except Exception as e: print(f"  [EXTRACT] LLM: {e}", file=sys.stderr)
        return []
    def _dedup(self, claims):
        seen, out = set(), []
        for c in claims:
            h = hashlib.sha256(re.sub(r'\s+',' ',c.text.lower())[:80].encode()).hexdigest()[:12]
            if h not in seen: seen.add(h); out.append(c)
        return out


# ═══ STAGE 4: DEDUP ═══

class Deduplicator:
    def deduplicate(self, claims):
        if len(claims) <= MAX_CLAIMS_AFTER_DEDUP:
            for c in claims:
                c.simhash = _mini_hash(c.text)
                c.authority_score = AUTHORITY.get(c.source_tier, 1)
                c.is_content_farm = _is_content_farm(c.source_url)
            return claims
        for c in claims: c.simhash = _mini_hash(c.text)
        clusters, used = [], set()
        for i, c1 in enumerate(claims):
            if i in used: continue
            cluster = [c1]; used.add(i)
            for j, c2 in enumerate(claims[i+1:], i+1):
                if j in used: continue
                if _simhash_similarity(c1.simhash, c2.simhash) > 0.7:
                    cluster.append(c2); used.add(j)
            clusters.append(cluster)
        survivors = []
        for cluster in clusters:
            best, best_score = None, -1
            for c in cluster:
                c.authority_score = AUTHORITY.get(c.source_tier, 1)
                c.is_content_farm = _is_content_farm(c.source_url)
                score = c.authority_score
                if c.is_content_farm: score *= 0.3
                if len(c.text) < 30: score *= 0.5
                if score > best_score: best_score = score; best = c
            if best: survivors.append(best)
        survivors.sort(key=lambda c: c.authority_score, reverse=True)
        return survivors[:MAX_CLAIMS_AFTER_DEDUP]


# ═══ STAGE 5: VERIFY ═══

class Verifier:
    def __init__(self):
        self._client = None
        if CLAUDE_KEY:
            try: import anthropic; self._client = anthropic.Anthropic(api_key=CLAUDE_KEY)
            except ImportError: pass
    def verify(self, claims):
        if not self._client:
            for c in claims: c.verification_status = "unverified"; c.confidence = 0.5
            return claims
        return [self._one(c) for c in claims]
    def _one(self, c):
        try:
            resp = self._client.messages.create(model=CLAUDE_MODEL, max_tokens=400,
                messages=[{"role":"user","content":f"""Fact-check. Be strict.

CLAIM: "{c.text}"
SOURCE: {c.source_title} ({c.source_url})
TIER: {c.source_tier}

VERDICT: VERIFIED | PARTIAL | FAILED | DISPUTED
CONFIDENCE: 0-100
REASON: one sentence
RED_FLAGS: any, or "None"."""}])
            txt = resp.content[0].text
            v = re.search(r'VERDICT:\s*(\w+)', txt, re.I)
            cf = re.search(r'CONFIDENCE:\s*(\d+)', txt)
            r = re.search(r'REASON:\s*(.+?)(?:\n|RED_FLAGS)', txt, re.S)
            rf = re.search(r'RED_FLAGS:\s*(.+)', txt, re.S)
            c.verification_status = v.group(1).lower() if v else "failed"
            c.confidence = int(cf.group(1))/100 if cf else 0.0
            c.verification_notes = f"{r.group(1).strip() if r else '?'} | {rf.group(1).strip() if rf else 'None'}"
        except Exception as e:
            c.verification_status = "failed"; c.confidence = 0.0; c.verification_notes = f"Error: {e}"
        return c


# ═══ STAGE 6: SYNTHESIZE ═══

class Synthesizer:
    def __init__(self):
        self._client = None
        if DEEPSEEK_KEY:
            try: import openai; self._client = openai.OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com")
            except ImportError: pass
    def synthesize(self, query, claims):
        verified = [c for c in claims if c.verification_status == "verified" and c.confidence >= 0.6]
        partial  = [c for c in claims if c.verification_status == "partial" and c.confidence >= 0.4]
        usable = verified + partial
        if not usable: return (f"## {query}\n\n**No verified claims found.**", 0.0, ["No claims passed"], [])
        claim_lines = []
        for i, c in enumerate(usable[:15]):
            s = "✅" if c.verification_status == "verified" else "⚠️"
            claim_lines.append(f"[{i+1}] {s} {c.text}")
            claim_lines.append(f"    Source: {c.source_title} ({c.source_url}) | {c.confidence:.0%}")
        if self._client: answer = self._llm_synth(query, claim_lines)
        else: answer = self._template_synth(query, usable)
        conf = sum(c.confidence for c in verified)/len(verified) if verified else (sum(c.confidence for c in partial)/len(partial)*0.7 if partial else 0)
        warnings = []
        if len(verified) < 3: warnings.append(f"Only {len(verified)} fully verified claims")
        low_tier = [c for c in usable if c.source_tier in ("forum","unknown")]
        if len(low_tier) > len(usable)*0.5: warnings.append("Majority low-authority sources")
        return answer, conf, warnings, []
    def _llm_synth(self, query, claim_lines):
        ctx = "\n\n".join(claim_lines)
        try:
            resp = self._client.chat.completions.create(model="deepseek-chat",
                messages=[{"role":"user","content":f"Build answer from verified claims only. Cite [1][2]...\n\nQUERY: {query}\n\nCLAIMS:\n{ctx}\n\nAnswer:"}],
                temperature=0.3, max_tokens=1500)
            return resp.choices[0].message.content
        except Exception as e:
            print(f"  [SYNTHESIZE] LLM: {e}", file=sys.stderr); return self._template_synth(query, [])
    def _template_synth(self, query, claims):
        lines = [f"## {query}\n"]
        for i, c in enumerate(claims[:10], 1):
            lines.append(f"{i}. {c.text}\n   *{c.source_title} — {c.confidence:.0%}*\n")
        return "\n".join(lines) if claims else f"## {query}\n\n*No verified claims*"


# ═══ STAGE 7: SANITY CHECK ═══

class SanityChecker:
    RED_FLAGS = [
        (r'\b(I think|I believe|maybe|probably|likely)\b', "hedging"),
        (r'\b(always|never|all|none|every|impossible)\b', "absolute_claim"),
        (r'\b(undoubtedly|certainly|definitely)\b', "overconfidence"),
        (r'\d{4,}', "large_number"),
        (r'\$\d+[\d,]*\s*(million|billion|trillion)', "large_money"),
        (r'\b(according to (?:some|many|several|various) (?:sources|people))\b', "vague_attribution"),
    ]
    def __init__(self):
        self._client = None
        if CLAUDE_KEY:
            try: import anthropic; self._client = anthropic.Anthropic(api_key=CLAUDE_KEY)
            except ImportError: pass
    def check(self, answer, claims):
        flags = []
        for pat, name in self.RED_FLAGS:
            matches = re.findall(pat, answer, re.I)
            if matches: flags.append(f"🔴 {name}: found {len(matches)} — \"{matches[0][:50]}...\"")
        years = re.findall(r'\b(20\d{2})\b', answer)
        future = [int(y) for y in years if int(y) > datetime.now().year + 1]
        if future: flags.append(f"🔴 Future year(s): {future}")
        tiers = set(c.source_tier for c in claims)
        if len(tiers) < 2 and len(claims) > 3: flags.append("🟡 Single source tier")
        if self._client and len(claims) >= 3:
            import random
            spot = random.sample(claims, min(3, len(claims)))
            try:
                check_texts = "\n".join(f"[{i+1}] {c.text}" for i,c in enumerate(spot))
                resp = self._client.messages.create(model=HAIKU_MODEL, max_tokens=200,
                    messages=[{"role":"user","content":f"Spot-check. Any false?\n\n{check_texts}\n\nALL_OK or list:"}])
                haiku = resp.content[0].text.strip()
                if "ALL_OK" not in haiku.upper(): flags.append(f"🟡 Haiku: {haiku[:120]}")
            except Exception as e: flags.append(f"⚪ Haiku unavailable: {e}")
        passed = len([f for f in flags if f.startswith("🔴")]) == 0
        return flags, passed


# ═══ PIPELINE ═══

def run(query: str, verify: bool = True) -> Phase2Result:
    t0 = time.time(); cost = 0.0

    subs = decompose(query)
    print(f"[1:DECOMPOSE] {len(subs)} sub-queries", file=sys.stderr)

    searcher = Searcher()
    local = LocalSearcher()
    # Check cache first
    cached = _cache.get(query)
    if cached:
        print(f"[2:SEARCH] {len(cached)} cached (instant)", file=sys.stderr)
        hits = cached
    else:
        # Parallel: web + local
        from concurrent.futures import ThreadPoolExecutor, as_completed as ac
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_web = ex.submit(searcher.search, subs, 5)
            f_local = ex.submit(local.search, subs, 5)
            web_hits = f_web.result()
            local_hits = f_local.result()
        hits = web_hits + local_hits
        _cache.set(query, web_hits)
        print(f"[2:SEARCH] {len(web_hits)} web + {len(local_hits)} local = {len(hits)} results", file=sys.stderr)

    extractor = Extractor()
    claims = extractor.extract(hits)
    cost += 0.001
    print(f"[3:EXTRACT] {len(claims)} claims", file=sys.stderr)

    in_count = len(claims)
    deduplicator = Deduplicator()
    claims = deduplicator.deduplicate(claims)
    print(f"[4:DEDUP] {in_count} → {len(claims)} ({in_count-len(claims)} removed)", file=sys.stderr)

    if verify:
        verifier = Verifier()
        claims = verifier.verify(claims)
        cost += len(claims) * 0.003
        vc = sum(1 for c in claims if c.verification_status == "verified")
        print(f"[5:VERIFY] {vc}/{len(claims)} verified (${cost:.3f})", file=sys.stderr)
    else:
        for c in claims: c.verification_status = "unverified"; c.confidence = 0.5
        vc = 0

    synthesizer = Synthesizer()
    answer, confidence, warnings, gaps = synthesizer.synthesize(query, claims)
    cost += 0.001
    print(f"[6:SYNTHESIZE] {confidence:.0%}", file=sys.stderr)

    sanity = SanityChecker()
    flags, ok = sanity.check(answer, claims)
    if sanity._client: cost += 0.003
    print(f"[7:SANITY] {len(flags)} flags — {'PASS' if ok else 'WARNINGS'}", file=sys.stderr)

    seen = set(); sources = []
    for c in claims:
        if c.source_url and c.source_url not in seen:
            seen.add(c.source_url)
            sources.append({"url":c.source_url,"title":c.source_title,
                "tier":c.source_tier,"score":AUTHORITY.get(c.source_tier,1)})
    sources.sort(key=lambda s: s["score"], reverse=True)

    return Phase2Result(
        query=query, answer=answer, confidence=confidence,
        verified_claims=vc, total_claims=len(claims),
        deduped_out=in_count-len(claims), sources=sources,
        warnings=warnings+flags, sanity_flags=flags,
        elapsed=time.time()-t0, cost_estimate=f"${cost:.3f}")


# ═══ CLI ═══

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("query"); ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--json", action="store_true"); ap.add_argument("--save", type=str)
    args = ap.parse_args()
    r = run(args.query, verify=not args.no_verify)
    if args.json:
        out = json.dumps({"query":r.query,"answer":r.answer,"confidence":round(r.confidence,2),
            "verified":r.verified_claims,"total":r.total_claims,"deduped_out":r.deduped_out,
            "sources":r.sources,"warnings":r.warnings,"sanity_flags":r.sanity_flags,
            "elapsed":round(r.elapsed,2),"cost":r.cost_estimate}, indent=2, ensure_ascii=False)
    else:
        emoji = {"gov":"🏛️","edu":"🎓","academic":"📚","major_pub":"📰","tech_corp":"💻","blog_expert":"📝","forum":"💬","unknown":"📄"}
        lines = ["="*65, f"  {r.query}", f"  CONFIDENCE: {r.confidence:.0%} | VERIFIED: {r.verified_claims}/{r.total_claims} | DEDUPED: {r.deduped_out} | COST: {r.cost_estimate} | {r.elapsed:.1f}s", "="*65, "", r.answer, "", f"📚 Sources ({len(r.sources)}):"]
        for s in r.sources[:8]: lines.append(f"  {emoji.get(s['tier'],'📄')} [{s['tier']}] {s['title'][:70]}\n     {s['url'][:90]}")
        if r.sanity_flags: lines.append("\n🛡️ Sanity:\n"); [lines.append(f"  {f}") for f in r.sanity_flags]
        if r.warnings: lines.append("\n⚠️:\n"); [lines.append(f"  • {w}") for w in r.warnings if w not in r.sanity_flags]
        lines.append("\n"+"="*65)
        out = "\n".join(lines)
    print(out)
    if args.save:
        with open(args.save,"w",encoding="utf-8") as f: f.write(out)
