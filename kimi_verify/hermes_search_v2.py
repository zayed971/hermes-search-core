#!/usr/bin/env python3
"""
hermes_search_v2.py — Anti-Hallucination Search Pipeline for Hermes Agent
============================================================================
7-stage pipeline built for Zayed's setup:
  DeepSeek V4 Pro (searcher/synthesizer) + Claude Sonnet (verifier)

Stages:
  1. DECOMPOSE   → Break complex query into sub-queries
  2. SEARCH      → Parallel web search + local cache (ripgrep)
  3. EXTRACT     → Pull structured claims from sources
  4. VERIFY      → Cross-check every claim (Claude Sonnet)
  5. SYNTHESIZE  → Build answer from verified claims only
  6. SANITY      → Final pass — catch obvious lies
  7. DELIVER     → Output with confidence scores + source map

Usage:
  python hermes_search_v2.py "What companies hire AI interns in UAE?"
  python hermes_search_v2.py --mode local "FastAPI authentication patterns"
  python hermes_search_v2.py --mode hybrid --verify "Is RAG dead in 2026?"

Author: Built for Zayed (Hermes Agent)
Date: 2026-07-08
"""

import os
import sys
import json
import re
import subprocess
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass, asdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# ── Configuration ──────────────────────────────────────────────────────────

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
CLAUDE_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MOONSHOT_API_KEY = os.environ.get("MOONSHOT_API_KEY", "")

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
CLAUDE_BASE_URL = "https://api.anthropic.com"
MOONSHOT_BASE_URL = "https://api.moonshot.ai/v1"

CACHE_DIR = Path(os.environ.get("HERMES_CACHE_DIR", "/mnt/c/Users/HP/Desktop/hermes_cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

LOCAL_DOCS_DIR = Path(os.environ.get("HERMES_DOCS_DIR", "/mnt/c/Users/HP/Desktop/hermes_docs"))

# Authority scoring for sources
AUTHORITY_TIERS = {
    "gov": 10,          # .gov, official government
    "edu": 9,           # .edu, universities
    "org_official": 9,  # Official org sites (WHO, UN, etc.)
    "academic": 8,      # arXiv, PubMed, IEEE
    "major_pub": 7,     # Reuters, Bloomberg, NYT
    "tech_corp": 6,     # Google, Microsoft, Apple docs
    "blog_expert": 5,   # Known expert blogs
    "forum": 3,         # Reddit, StackOverflow
    "unknown": 1,       # Everything else
}

# ── Data Structures ────────────────────────────────────────────────────────

@dataclass
class Claim:
    """A single factual claim extracted from a source."""
    text: str
    source_url: str
    source_title: str
    source_tier: str
    extraction_method: str  # "direct", "inferred", "quoted"
    timestamp: str
    verification_status: str = "unverified"  # unverified | verified | failed | disputed
    verification_notes: str = ""
    confidence: float = 0.0  # 0.0-1.0

@dataclass
class SubQuery:
    """A decomposed sub-query."""
    id: str
    text: str
    query_type: str  # "factual", "temporal", "comparative", "procedural"
    priority: int
    claims: List[Claim] = None

    def __post_init__(self):
        if self.claims is None:
            self.claims = []

@dataclass
class SearchResult:
    """Raw search result before extraction."""
    query: str
    source_url: str
    source_title: str
    raw_content: str
    retrieval_method: str  # "web", "cache", "local_doc"
    retrieved_at: str

@dataclass
class PipelineOutput:
    """Final deliverable."""
    original_query: str
    answer: str
    confidence: float
    claims_used: List[Dict]
    claims_rejected: List[Dict]
    sources: List[Dict]
    metadata: Dict
    warnings: List[str]

# ── Stage 1: DECOMPOSE ─────────────────────────────────────────────────────

class QueryDecomposer:
    """Break complex queries into verifiable sub-queries."""

    def __init__(self, api_key: str = None):
        self.api_key = api_key or DEEPSEEK_API_KEY
        self.client = None
        if self.api_key:
            try:
                import openai
                self.client = openai.OpenAI(api_key=self.api_key, base_url=DEEPSEEK_BASE_URL)
            except ImportError:
                pass

    def decompose(self, query: str) -> List[SubQuery]:
        """Decompose a query into sub-queries."""
        # First, try pattern-based decomposition for common query types
        pattern_based = self._pattern_decompose(query)
        if pattern_based:
            return pattern_based

        # Fallback to LLM-based decomposition
        return self._llm_decompose(query)

    def _pattern_decompose(self, query: str) -> Optional[List[SubQuery]]:
        """Pattern-based decomposition — no API call needed."""
        query_lower = query.lower()
        subqueries = []

        # Comparison pattern: "Compare X and Y"
        compare_match = re.search(r'compare\s+(.+?)\s+(?:and|vs\.?|versus)\s+(.+?)(?:\?|$)', query_lower)
        if compare_match:
            x, y = compare_match.group(1).strip(), compare_match.group(2).strip()
            subqueries = [
                SubQuery("sq_1", f"What is {x}?", "factual", 1),
                SubQuery("sq_2", f"What is {y}?", "factual", 1),
                SubQuery("sq_3", f"Key differences between {x} and {y}", "comparative", 2),
                SubQuery("sq_4", f"When to use {x} vs {y}", "procedural", 3),
            ]
            return subqueries

        # "How to" pattern
        if query_lower.startswith(("how to ", "how do ", "how can ", "how should ")):
            topic = re.sub(r'^how\s+(?:to|do|can|should)\s+', '', query_lower).strip("?")
            subqueries = [
                SubQuery("sq_1", f"What is {topic}?", "factual", 1),
                SubQuery("sq_2", f"Prerequisites for {topic}", "factual", 2),
                SubQuery("sq_3", f"Step-by-step guide for {topic}", "procedural", 1),
                SubQuery("sq_4", f"Common mistakes when doing {topic}", "procedural", 3),
            ]
            return subqueries

        # "What is / Who is" pattern
        what_match = re.search(r'^(?:what|who)\s+(?:is|are|was|were)\s+(.+?)\??$', query_lower)
        if what_match:
            topic = what_match.group(1).strip()
            subqueries = [
                SubQuery("sq_1", f"Definition of {topic}", "factual", 1),
                SubQuery("sq_2", f"Key facts about {topic}", "factual", 1),
                SubQuery("sq_3", f"Recent developments related to {topic}", "temporal", 2),
            ]
            return subqueries

        # "Latest / Recent / 2026" pattern (temporal)
        if any(w in query_lower for w in ["latest", "recent", "2026", "2025", "now", "current"]):
            subqueries = [
                SubQuery("sq_1", f"Overview of {query}", "factual", 1),
                SubQuery("sq_2", f"Latest developments: {query}", "temporal", 1),
                SubQuery("sq_3", f"Expert opinions on {query}", "factual", 2),
            ]
            return subqueries

        return None

    def _llm_decompose(self, query: str) -> List[SubQuery]:
        """LLM-based decomposition using DeepSeek."""
        if not self.client:
            # Fallback: single sub-query
            return [SubQuery("sq_1", query, "factual", 1)]

        prompt = f"""You are a query decomposition engine. Break the following user query into 2-4 specific, verifiable sub-queries.
Each sub-query should be self-contained and answerable independently.

User query: "{query}"

Respond in this exact JSON format:
{{
  "sub_queries": [
    {{"id": "sq_1", "text": "...", "type": "factual|temporal|comparative|procedural", "priority": 1}},
    ...
  ]
}}

Rules:
- priority: 1 = must answer, 2 = important, 3 = nice to have
- type: factual (what/is), temporal (when/latest), comparative (vs), procedural (how)
- Each sub-query must be specific enough to search for directly"""

        try:
            response = self.client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=1000,
            )
            content = response.choices[0].message.content
            # Extract JSON
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                # Fix LLM field names: 'type' → 'query_type', 'id' stays, 'text' stays
                for sq in data.get("sub_queries", []):
                    if "type" in sq and "query_type" not in sq:
                        sq["query_type"] = sq.pop("type")
                return [SubQuery(**sq) for sq in data.get("sub_queries", [])]
        except Exception as e:
            print(f"[DECOMPOSE] LLM decomposition failed: {e}", file=sys.stderr)

        return [SubQuery("sq_1", query, "factual", 1)]

# ── Stage 2: SEARCH ────────────────────────────────────────────────────────

class SearchEngine:
    """Parallel search across web, cache, and local docs."""

    def __init__(self):
        self.cache = SearchCache()
        self.local_searcher = LocalDocSearcher()
        self.web_searcher = WebSearcher()

    def search(self, subqueries: List[SubQuery], mode: str = "hybrid") -> Dict[str, List[SearchResult]]:
        """
        Search for all sub-queries in parallel.
        mode: "web" | "local" | "hybrid" | "cache_only"
        """
        results = {}

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {}
            for sq in subqueries:
                future = executor.submit(self._search_single, sq, mode)
                futures[future] = sq.id

            for future in as_completed(futures):
                sq_id = futures[future]
                try:
                    results[sq_id] = future.result()
                except Exception as e:
                    print(f"[SEARCH] Failed for {sq_id}: {e}", file=sys.stderr)
                    results[sq_id] = []

        return results

    def _search_single(self, sq: SubQuery, mode: str) -> List[SearchResult]:
        """Search for a single sub-query."""
        all_results = []

        # 1. Check cache first (always)
        cached = self.cache.get(sq.text)
        if cached:
            all_results.extend(cached)

        if mode == "cache_only":
            return all_results

        # 2. Local docs search (if hybrid or local)
        if mode in ("hybrid", "local"):
            local_results = self.local_searcher.search(sq.text)
            all_results.extend(local_results)

        # 3. Web search (if hybrid or web)
        if mode in ("hybrid", "web"):
            web_results = self.web_searcher.search(sq.text)
            all_results.extend(web_results)
            # Cache web results
            self.cache.set(sq.text, web_results)

        return all_results


class SearchCache:
    """File-based cache for search results."""

    def __init__(self, cache_dir: Path = CACHE_DIR):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _key(self, query: str) -> str:
        return hashlib.sha256(query.encode()).hexdigest()[:16]

    def get(self, query: str) -> Optional[List[SearchResult]]:
        key = self._key(query)
        cache_file = self.cache_dir / f"{key}.json"

        if not cache_file.exists():
            return None

        # Check freshness (24 hours)
        age = time.time() - cache_file.stat().st_mtime
        if age > 86400:
            return None

        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [SearchResult(**r) for r in data]
        except Exception:
            return None

    def set(self, query: str, results: List[SearchResult]) -> None:
        key = self._key(query)
        cache_file = self.cache_dir / f"{key}.json"

        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump([asdict(r) for r in results], f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[CACHE] Write failed: {e}", file=sys.stderr)


class LocalDocSearcher:
    """Search local markdown docs using ripgrep."""

    def __init__(self, docs_dir: Path = LOCAL_DOCS_DIR):
        self.docs_dir = docs_dir
        self.ripgrep_available = self._check_ripgrep()

    def _check_ripgrep(self) -> bool:
        try:
            subprocess.run(["rg", "--version"], capture_output=True, check=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def search(self, query: str, max_results: int = 10) -> List[SearchResult]:
        """Search local docs for query."""
        if not self.docs_dir.exists():
            return []

        results = []

        if self.ripgrep_available:
            results = self._ripgrep_search(query, max_results)
        else:
            results = self._fallback_search(query, max_results)

        return results

    def _ripgrep_search(self, query: str, max_results: int) -> List[SearchResult]:
        """Use ripgrep for fast search."""
        try:
            # Extract key terms (remove stop words)
            terms = self._extract_terms(query)
            if not terms:
                return []

            # Build ripgrep command
            pattern = "|".join(terms)
            cmd = [
                "rg", "-i", "-n", "-C", "3",
                "--type", "md",
                "--type", "txt",
                "--max-count", str(max_results),
                pattern,
                str(self.docs_dir)
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode not in (0, 1):  # 1 = no matches
                return []

            return self._parse_ripgrep_output(result.stdout, query)

        except subprocess.TimeoutExpired:
            print("[LOCAL] ripgrep timed out", file=sys.stderr)
            return []
        except Exception as e:
            print(f"[LOCAL] ripgrep error: {e}", file=sys.stderr)
            return []

    def _fallback_search(self, query: str, max_results: int) -> List[SearchResult]:
        """Fallback to Python grep if ripgrep unavailable."""
        results = []
        terms = self._extract_terms(query)

        for root, _, files in os.walk(self.docs_dir):
            for fname in files:
                if not fname.endswith((".md", ".txt")):
                    continue

                fpath = Path(root) / fname
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()

                    # Simple term matching
                    score = sum(1 for t in terms if t.lower() in content.lower())
                    if score > 0:
                        # Extract snippet
                        idx = content.lower().find(terms[0].lower())
                        snippet = content[max(0, idx-200):idx+500]

                        results.append(SearchResult(
                            query=query,
                            source_url=f"file://{fpath}",
                            source_title=fname,
                            raw_content=snippet,
                            retrieval_method="local_doc",
                            retrieved_at=datetime.now(timezone.utc).isoformat()
                        ))

                        if len(results) >= max_results:
                            break
                except Exception:
                    continue

            if len(results) >= max_results:
                break

        return results

    def _extract_terms(self, query: str) -> List[str]:
        """Extract meaningful search terms."""
        stop_words = {"the", "a", "an", "is", "are", "was", "were", "be", "been",
                      "being", "have", "has", "had", "do", "does", "did", "will",
                      "would", "could", "should", "may", "might", "must", "shall",
                      "can", "need", "dare", "ought", "used", "to", "of", "in",
                      "for", "on", "with", "at", "by", "from", "as", "into",
                      "through", "during", "before", "after", "above", "below",
                      "between", "under", "and", "but", "or", "yet", "so", "if",
                      "because", "although", "though", "while", "where", "when",
                      "that", "which", "who", "whom", "whose", "what", "how",
                      "why", "this", "these", "those", "i", "me", "my", "myself",
                      "we", "our", "you", "your", "he", "him", "his", "she",
                      "her", "it", "its", "they", "them", "their"}

        words = re.findall(r'[a-zA-Z]{3,}', query.lower())
        return [w for w in words if w not in stop_words][:5]  # Top 5 terms

    def _parse_ripgrep_output(self, output: str, query: str) -> List[SearchResult]:
        """Parse ripgrep output into SearchResults."""
        results = []
        current_file = None
        current_lines = []

        for line in output.split("\n"):
            if not line.strip():
                continue

            # New file match
            if line.startswith("--"):
                if current_file and current_lines:
                    results.append(self._make_result(current_file, current_lines, query))
                current_file = None
                current_lines = []
            elif ":" in line and not line.startswith(" "):
                # File:line:content format
                parts = line.split(":", 2)
                if len(parts) >= 3:
                    if current_file and current_lines:
                        results.append(self._make_result(current_file, current_lines, query))
                    current_file = parts[0]
                    current_lines = [parts[2]]
            else:
                current_lines.append(line)

        if current_file and current_lines:
            results.append(self._make_result(current_file, current_lines, query))

        return results[:10]

    def _make_result(self, filepath: str, lines: List[str], query: str) -> SearchResult:
        content = "\n".join(lines)
        return SearchResult(
            query=query,
            source_url=f"file://{filepath}",
            source_title=Path(filepath).name,
            raw_content=content,
            retrieval_method="local_doc",
            retrieved_at=datetime.now(timezone.utc).isoformat()
        )


class WebSearcher:
    """Web search using available APIs."""

    def __init__(self):
        self.ddgs_available = self._check_ddgs()

    def _check_ddgs(self) -> bool:
        try:
            from ddgs import DDGS
            return True
        except ImportError:
            return False

    def search(self, query: str, max_results: int = 8) -> List[SearchResult]:
        """Search the web."""
        if self.ddgs_available:
            return self._ddgs_search(query, max_results)
        return self._curl_search(query, max_results)

    def _ddgs_search(self, query: str, max_results: int) -> List[SearchResult]:
        """Use DuckDuckGo search."""
        try:
            from ddgs import DDGS

            results = []
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=max_results):
                    results.append(SearchResult(
                        query=query,
                        source_url=r.get("href", ""),
                        source_title=r.get("title", "Untitled"),
                        raw_content=r.get("body", ""),
                        retrieval_method="web",
                        retrieved_at=datetime.now(timezone.utc).isoformat()
                    ))
            return results
        except Exception as e:
            print(f"[WEB] DDGS error: {e}", file=sys.stderr)
            return []

    def _curl_search(self, query: str, max_results: int) -> List[SearchResult]:
        """Fallback: basic curl to a search API."""
        # This is a minimal fallback — in production, use a real search API
        print("[WEB] Warning: No search backend available. Install duckduckgo-search.", file=sys.stderr)
        return []

# ── Stage 3: EXTRACT ───────────────────────────────────────────────────────

class ClaimExtractor:
    """Extract structured claims from raw search results."""

    def __init__(self, api_key: str = None):
        self.api_key = api_key or DEEPSEEK_API_KEY
        self.client = None
        if self.api_key:
            try:
                import openai
                self.client = openai.OpenAI(api_key=self.api_key, base_url=DEEPSEEK_BASE_URL)
            except ImportError:
                pass

    def extract(self, search_results: List[SearchResult]) -> List[Claim]:
        """Extract claims from all search results."""
        all_claims = []

        for result in search_results:
            claims = self._extract_from_source(result)
            all_claims.extend(claims)

        # Deduplicate
        return self._deduplicate_claims(all_claims)

    def _extract_from_source(self, result: SearchResult) -> List[Claim]:
        """Extract claims from a single source."""
        claims = []

        # Determine source tier
        tier = self._classify_source(result.source_url)

        # Pattern-based extraction first
        pattern_claims = self._pattern_extract(result)
        claims.extend(pattern_claims)

        # If few claims and we have an LLM, use it
        if len(claims) < 2 and self.client and len(result.raw_content) > 100:
            llm_claims = self._llm_extract(result)
            claims.extend(llm_claims)

        # Tag all claims
        for claim in claims:
            claim.source_url = result.source_url
            claim.source_title = result.source_title
            claim.source_tier = tier
            claim.timestamp = result.retrieved_at

        return claims

    def _classify_source(self, url: str) -> str:
        """Classify source authority tier."""
        url_lower = url.lower()

        if ".gov" in url_lower:
            return "gov"
        elif ".edu" in url_lower:
            return "edu"
        elif any(d in url_lower for d in ["who.int", "un.org", "worldbank.org", "imf.org"]):
            return "org_official"
        elif any(d in url_lower for d in ["arxiv.org", "pubmed.ncbi.nlm.nih.gov", "ieee.org"]):
            return "academic"
        elif any(d in url_lower for d in ["reuters.com", "bloomberg.com", "nytimes.com", "wsj.com"]):
            return "major_pub"
        elif any(d in url_lower for d in ["microsoft.com", "google.com", "apple.com", "amazon.com", "github.com"]):
            return "tech_corp"
        elif "reddit.com" in url_lower or "stackoverflow.com" in url_lower:
            return "forum"
        elif "blog" in url_lower or "medium.com" in url_lower:
            return "blog_expert"
        else:
            return "unknown"

    def _pattern_extract(self, result: SearchResult) -> List[Claim]:
        """Extract claims using regex patterns."""
        claims = []
        text = result.raw_content

        # Pattern: "X is Y" statements
        is_pattern = re.findall(r'([A-Z][^\.]{10,80})\s+is\s+([^\.]{10,200})\.', text)
        for match in is_pattern:
            claims.append(Claim(
                text=f"{match[0]} is {match[1]}",
                source_url="",
                source_title="",
                source_tier="",
                extraction_method="pattern",
                timestamp=""
            ))

        # Pattern: "According to X, Y"
        according_pattern = re.findall(r'According to ([^,]+),\s+([^\.]{20,300})\.', text, re.IGNORECASE)
        for match in according_pattern:
            claims.append(Claim(
                text=f"According to {match[0]}, {match[1]}",
                source_url="",
                source_title="",
                source_tier="",
                extraction_method="quoted",
                timestamp=""
            ))

        # Pattern: "In 202X, ..."
        year_pattern = re.findall(r'(In (?:19|20)\d{2}[^\.]{20,300}\.)', text)
        for match in year_pattern:
            claims.append(Claim(
                text=match,
                source_url="",
                source_title="",
                source_tier="",
                extraction_method="pattern",
                timestamp=""
            ))

        return claims[:10]  # Limit per source

    def _llm_extract(self, result: SearchResult) -> List[Claim]:
        """Use LLM to extract claims."""
        if not self.client:
            return []

        prompt = f"""Extract 3-5 factual claims from the following text. Each claim should be a single, verifiable statement.

Source: {result.source_title}
URL: {result.source_url}

Text:
{result.raw_content[:3000]}

Respond in this JSON format:
{{
  "claims": [
    {{"text": "...", "type": "fact|statistic|quote|date"}}
  ]
}}

Only include claims that are specific and checkable."""

        try:
            response = self.client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=1500,
            )
            content = response.choices[0].message.content
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                return [Claim(
                    text=c["text"],
                    source_url="",
                    source_title="",
                    source_tier="",
                    extraction_method="llm",
                    timestamp=""
                ) for c in data.get("claims", [])]
        except Exception as e:
            print(f"[EXTRACT] LLM extraction failed: {e}", file=sys.stderr)

        return []

    def _deduplicate_claims(self, claims: List[Claim]) -> List[Claim]:
        """Remove near-duplicate claims."""
        seen = set()
        unique = []

        for claim in claims:
            # Simple hash of normalized text
            normalized = re.sub(r'\s+', ' ', claim.text.lower().strip())[:100]
            h = hashlib.sha256(normalized.encode()).hexdigest()[:16]

            if h not in seen:
                seen.add(h)
                unique.append(claim)

        return unique

# ── Stage 4: VERIFY ────────────────────────────────────────────────────────

class ClaimVerifier:
    """Verify claims using Claude Sonnet."""

    def __init__(self, api_key: str = None):
        self.api_key = api_key or CLAUDE_API_KEY
        self.client = None
        if self.api_key:
            try:
                import anthropic
                self.client = anthropic.Anthropic(api_key=self.api_key)
            except ImportError:
                pass

    def verify_batch(self, claims: List[Claim]) -> List[Claim]:
        """Verify a batch of claims."""
        if not self.client:
            print("[VERIFY] Warning: No Claude API key. Skipping verification.", file=sys.stderr)
            for claim in claims:
                claim.verification_status = "unverified"
                claim.confidence = 0.5
            return claims

        verified = []
        for claim in claims:
            verified_claim = self._verify_single(claim)
            verified.append(verified_claim)

        return verified

    def _verify_single(self, claim: Claim) -> Claim:
        """Verify a single claim."""
        prompt = f"""You are a fact-checking verifier. Evaluate the following claim for accuracy.

CLAIM: "{claim.text}"
SOURCE: {claim.source_title} ({claim.source_url})
SOURCE TIER: {claim.source_tier} (gov=10, edu=9, academic=8, major_pub=7, tech_corp=6, blog=5, forum=3, unknown=1)

Evaluate:
1. Is this claim specific and checkable?
2. Does the source have authority to make this claim?
3. Are there any red flags (vague language, weasel words, missing context)?
4. What is your confidence this claim is accurate (0-100)?

Respond in this exact format:
VERDICT: VERIFIED | PARTIAL | FAILED | DISPUTED
CONFIDENCE: 0-100
REASON: One sentence explaining why.
RED_FLAGS: Any concerns, or "None".

Be strict. If you cannot verify from your knowledge, say FAILED."""

        try:
            response = self.client.messages.create(
                model="claude-sonnet-5",
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}]
            )

            content = response.content[0].text

            # Parse response
            verdict_match = re.search(r'VERDICT:\s*(\w+)', content, re.IGNORECASE)
            confidence_match = re.search(r'CONFIDENCE:\s*(\d+)', content)
            reason_match = re.search(r'REASON:\s*(.+?)(?:\n|RED_FLAGS)', content, re.DOTALL)
            redflags_match = re.search(r'RED_FLAGS:\s*(.+)', content, re.DOTALL)

            verdict = verdict_match.group(1).upper() if verdict_match else "FAILED"
            confidence = int(confidence_match.group(1)) / 100.0 if confidence_match else 0.0
            reason = reason_match.group(1).strip() if reason_match else "No reason given"
            redflags = redflags_match.group(1).strip() if redflags_match else "None"

            claim.verification_status = verdict.lower()
            claim.confidence = confidence
            claim.verification_notes = f"{reason} | Red flags: {redflags}"

        except Exception as e:
            print(f"[VERIFY] Verification failed for claim: {e}", file=sys.stderr)
            claim.verification_status = "failed"
            claim.confidence = 0.0
            claim.verification_notes = f"Verification error: {e}"

        return claim

# ── Stage 5: SYNTHESIZE ────────────────────────────────────────────────────

class Synthesizer:
    """Build answer from verified claims only."""

    def __init__(self, api_key: str = None):
        self.api_key = api_key or DEEPSEEK_API_KEY
        self.client = None
        if self.api_key:
            try:
                import openai
                self.client = openai.OpenAI(api_key=self.api_key, base_url=DEEPSEEK_BASE_URL)
            except ImportError:
                pass

    def synthesize(self, query: str, claims: List[Claim], subqueries: List[SubQuery]) -> Tuple[str, float, List[str]]:
        """
        Synthesize answer from verified claims.
        Returns: (answer_text, confidence_score, warnings)
        """
        # Filter to verified claims only
        verified = [c for c in claims if c.verification_status == "verified" and c.confidence >= 0.6]
        partial = [c for c in claims if c.verification_status == "partial" and c.confidence >= 0.4]

        warnings = []

        if not verified and not partial:
            warnings.append("WARNING: No claims passed verification. Answer may be unreliable.")
            # Use unverified but flag it
            usable = claims[:3]
        else:
            usable = verified + partial

        # Build source map
        source_map = self._build_source_map(usable)

        # Calculate overall confidence
        if usable:
            avg_confidence = sum(c.confidence for c in usable) / len(usable)
            # Boost if multiple independent sources agree
            unique_sources = len(set(c.source_url for c in usable))
            consensus_boost = min(0.15, (unique_sources - 1) * 0.05)
            confidence = min(1.0, avg_confidence + consensus_boost)
        else:
            confidence = 0.0

        # Generate answer
        if self.client and len(usable) > 0:
            answer = self._llm_synthesize(query, usable, subqueries)
        else:
            answer = self._template_synthesize(query, usable)

        # Add warnings
        if len(verified) < 2:
            warnings.append(f"Only {len(verified)} fully verified claims. Consider more research.")

        low_tier_sources = [c for c in usable if c.source_tier in ("forum", "unknown", "blog_expert")]
        if len(low_tier_sources) > len(usable) * 0.5:
            warnings.append("Majority of sources are low-tier. Verify independently.")

        return answer, confidence, warnings

    def _build_source_map(self, claims: List[Claim]) -> Dict[str, Dict]:
        """Build a map of sources used."""
        sources = {}
        for claim in claims:
            url = claim.source_url
            if url not in sources:
                sources[url] = {
                    "title": claim.source_title,
                    "tier": claim.source_tier,
                    "tier_score": AUTHORITY_TIERS.get(claim.source_tier, 1),
                    "claims": []
                }
            sources[url]["claims"].append(claim.text[:200])
        return sources

    def _llm_synthesize(self, query: str, claims: List[Claim], subqueries: List[SubQuery]) -> str:
        """Use LLM to synthesize answer."""
        # Build context
        claims_text = "\n\n".join([
            f"[{i+1}] {c.text} (confidence: {c.confidence:.0%}, source: {c.source_title}, tier: {c.source_tier})"
            for i, c in enumerate(claims[:15])  # Top 15 claims
        ])

        prompt = f"""You are a research synthesizer. Build a clear, accurate answer to the user's question using ONLY the provided verified claims.

USER QUESTION: {query}

VERIFIED CLAIMS:
{claims_text}

RULES:
1. Use ONLY the claims above. Do not add outside knowledge.
2. Cite claims using [1], [2], etc.
3. If claims conflict, note the conflict.
4. If the claims don't fully answer the question, say what's missing.
5. Be concise but complete.

Write the answer now:"""

        try:
            response = self.client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=2000,
            )
            return response.choices[0].message.content
        except Exception as e:
            print(f"[SYNTHESIZE] LLM synthesis failed: {e}", file=sys.stderr)
            return self._template_synthesize(query, claims)

    def _template_synthesize(self, query: str, claims: List[Claim]) -> str:
        """Template-based synthesis when LLM unavailable."""
        lines = [f"Answer to: {query}\n"]

        for i, claim in enumerate(claims[:10]):
            lines.append(f"{i+1}. {claim.text} [{claim.source_title}]")

        if not claims:
            lines.append("\nNo verified claims found.")

        return "\n".join(lines)

# ── Stage 6: SANITY CHECK ──────────────────────────────────────────────────

class SanityChecker:
    """Final pass to catch obvious hallucinations and issues."""

    RED_FLAG_PATTERNS = [
        (r'\b(I think|I believe|maybe|probably|likely|seems like)\b', "hedging_language"),
        (r'\b(always|never|all|none|every|impossible)\b', "absolute_claim"),
        (r'\b(undoubtedly|certainly|definitely|without a doubt)\b', "overconfidence"),
        (r'\d{4,}', "large_number_unsourced"),
        (r'\$\d+[\d,]*\s*(million|billion|trillion)', "large_money_unsourced"),
        (r'\b(according to (?:some|many|several|various) (?:sources|people|experts))\b', "vague_attribution"),
        (r'\b(it is said|it is believed|it is thought)\b', "passive_vague"),
    ]

    def check(self, answer: str, claims: List[Claim]) -> List[str]:
        """Run sanity checks on the final answer."""
        warnings = []

        # Check 1: Red flag patterns
        for pattern, flag_type in self.RED_FLAG_PATTERNS:
            matches = re.findall(pattern, answer, re.IGNORECASE)
            if matches:
                warnings.append(f"SANITY: Found {flag_type} — {len(matches)} instance(s). Verify: {matches[0]}")

        # Check 2: Claims without citations
        uncited_claims = [c for c in claims if c.verification_status != "verified"]
        if len(uncited_claims) > len(claims) * 0.5:
            warnings.append(f"SANITY: {len(uncited_claims)}/{len(claims)} claims are unverified. Answer reliability LOW.")

        # Check 3: Self-contradiction
        contradictions = self._find_contradictions(claims)
        if contradictions:
            warnings.append(f"SANITY: Found {len(contradictions)} potential contradictions in claims.")

        # Check 4: Temporal sanity
        year_matches = re.findall(r'\b(20\d{2})\b', answer)
        if year_matches:
            current_year = datetime.now().year
            future_years = [int(y) for y in year_matches if int(y) > current_year]
            if future_years:
                warnings.append(f"SANITY: Answer references future year(s): {future_years}. Check if intentional.")

        # Check 5: Source diversity
        unique_tiers = set(c.source_tier for c in claims)
        if len(unique_tiers) < 2 and len(claims) > 3:
            warnings.append("SANITY: All claims from same source tier. Seek diverse sources.")

        return warnings

    def _find_contradictions(self, claims: List[Claim]) -> List[Tuple[Claim, Claim]]:
        """Find potentially contradictory claims."""
        contradictions = []

        # Simple: look for negation patterns
        for i, c1 in enumerate(claims):
            for c2 in claims[i+1:]:
                # Check if one negates the other
                text1 = c1.text.lower()
                text2 = c2.text.lower()

                # Extract core subject
                if " is " in text1 and " is " in text2:
                    subj1 = text1.split(" is ")[0].strip()
                    subj2 = text2.split(" is ")[0].strip()

                    if subj1 == subj2:
                        # Same subject, check for contradiction markers
                        neg_words = ["not", "no", "never", "false", "incorrect", "untrue"]
                        has_neg1 = any(w in text1 for w in neg_words)
                        has_neg2 = any(w in text2 for w in neg_words)

                        if has_neg1 != has_neg2:
                            contradictions.append((c1, c2))

        return contradictions

# ── Stage 7: DELIVER ───────────────────────────────────────────────────────

class Pipeline:
    """Main pipeline orchestrator."""

    def __init__(self):
        self.decomposer = QueryDecomposer()
        self.searcher = SearchEngine()
        self.extractor = ClaimExtractor()
        self.verifier = ClaimVerifier()
        self.synthesizer = Synthesizer()
        self.sanity = SanityChecker()

    def run(self, query: str, mode: str = "hybrid", verify: bool = True) -> PipelineOutput:
        """Run the full pipeline."""
        start_time = time.time()
        metadata = {
            "query": query,
            "mode": mode,
            "verify_enabled": verify,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }

        print(f"[PIPELINE] Starting: {query[:60]}...")

        # Stage 1: Decompose
        print("[STAGE 1] Decomposing query...")
        subqueries = self.decomposer.decompose(query)
        print(f"[STAGE 1] → {len(subqueries)} sub-queries")
        metadata["subqueries"] = [asdict(sq) for sq in subqueries]

        # Stage 2: Search
        print("[STAGE 2] Searching...")
        search_results = self.searcher.search(subqueries, mode=mode)
        total_results = sum(len(r) for r in search_results.values())
        print(f"[STAGE 2] → {total_results} raw results")
        metadata["total_raw_results"] = total_results

        # Stage 3: Extract
        print("[STAGE 3] Extracting claims...")
        all_claims = []
        for sq_id, results in search_results.items():
            claims = self.extractor.extract(results)
            all_claims.extend(claims)
            # Attach to subquery
            for sq in subqueries:
                if sq.id == sq_id:
                    sq.claims = claims

        print(f"[STAGE 3] → {len(all_claims)} claims extracted")
        metadata["total_claims"] = len(all_claims)

        # Stage 4: Verify
        if verify:
            print("[STAGE 4] Verifying claims with Claude...")
            all_claims = self.verifier.verify_batch(all_claims)
            verified_count = sum(1 for c in all_claims if c.verification_status == "verified")
            print(f"[STAGE 4] → {verified_count}/{len(all_claims)} verified")
            metadata["verified_count"] = verified_count
        else:
            print("[STAGE 4] Verification skipped")
            for c in all_claims:
                c.verification_status = "unverified"
                c.confidence = 0.5

        # Stage 5: Synthesize
        print("[STAGE 5] Synthesizing answer...")
        answer, confidence, synth_warnings = self.synthesizer.synthesize(query, all_claims, subqueries)
        print(f"[STAGE 5] → Confidence: {confidence:.0%}")
        metadata["confidence"] = confidence

        # Stage 6: Sanity
        print("[STAGE 6] Running sanity checks...")
        sanity_warnings = self.sanity.check(answer, all_claims)
        all_warnings = synth_warnings + sanity_warnings

        # Build output
        claims_used = [asdict(c) for c in all_claims if c.verification_status in ("verified", "partial")]
        claims_rejected = [asdict(c) for c in all_claims if c.verification_status in ("failed", "disputed")]

        sources = []
        seen_urls = set()
        for c in all_claims:
            if c.source_url and c.source_url not in seen_urls:
                seen_urls.add(c.source_url)
                sources.append({
                    "url": c.source_url,
                    "title": c.source_title,
                    "tier": c.source_tier,
                    "tier_score": AUTHORITY_TIERS.get(c.source_tier, 1)
                })

        # Sort sources by tier
        sources.sort(key=lambda s: s["tier_score"], reverse=True)

        elapsed = time.time() - start_time
        metadata["elapsed_seconds"] = round(elapsed, 2)

        print(f"[PIPELINE] Done in {elapsed:.1f}s")

        return PipelineOutput(
            original_query=query,
            answer=answer,
            confidence=confidence,
            claims_used=claims_used,
            claims_rejected=claims_rejected,
            sources=sources,
            metadata=metadata,
            warnings=all_warnings
        )

# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Hermes Search v2 — Anti-Hallucination Pipeline")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--mode", choices=["web", "local", "hybrid", "cache_only"], default="hybrid",
                        help="Search mode (default: hybrid)")
    parser.add_argument("--verify", action="store_true", default=True,
                        help="Enable Claude verification (default: True)")
    parser.add_argument("--no-verify", action="store_true",
                        help="Skip verification (faster, less reliable)")
    parser.add_argument("--json", action="store_true",
                        help="Output raw JSON")
    parser.add_argument("--save", type=str, default=None,
                        help="Save output to file")

    args = parser.parse_args()

    verify = not args.no_verify

    pipeline = Pipeline()
    result = pipeline.run(args.query, mode=args.mode, verify=verify)

    if args.json:
        output = json.dumps({
            "query": result.original_query,
            "answer": result.answer,
            "confidence": result.confidence,
            "claims_used": len(result.claims_used),
            "claims_rejected": len(result.claims_rejected),
            "sources": result.sources,
            "warnings": result.warnings,
            "metadata": result.metadata
        }, indent=2, ensure_ascii=False)
    else:
        # Human-readable output
        lines = [
            "=" * 70,
            "HERMES SEARCH v2 — ANTI-HALLUCINATION RESULT",
            "=" * 70,
            f"",
            f"QUERY: {result.original_query}",
            f"CONFIDENCE: {result.confidence:.0%}",
            f"TIME: {result.metadata.get('elapsed_seconds', 'N/A')}s",
            f"",
            "─" * 70,
            "ANSWER",
            "─" * 70,
            result.answer,
            f"",
            "─" * 70,
            f"SOURCES ({len(result.sources)})",
            "─" * 70,
        ]

        for i, src in enumerate(result.sources[:10], 1):
            tier_emoji = {"gov": "🏛️", "edu": "🎓", "academic": "📚", "major_pub": "📰",
                         "tech_corp": "💻", "blog_expert": "📝", "forum": "💬", "unknown": "❓"}.get(src["tier"], "📄")
            lines.append(f"{i}. {tier_emoji} [{src['tier']}] {src['title']}")
            lines.append(f"   {src['url'][:80]}")

        if result.warnings:
            lines.extend([
                f"",
                "─" * 70,
                "⚠️ WARNINGS",
                "─" * 70,
            ])
            for w in result.warnings:
                lines.append(f"  • {w}")

        lines.extend([
            f"",
            "─" * 70,
            f"CLAIMS: {len(result.claims_used)} used | {len(result.claims_rejected)} rejected",
            "=" * 70,
        ])

        output = "\n".join(lines)

    print(output)

    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"\n[✓] Saved to {args.save}")

if __name__ == "__main__":
    main()
