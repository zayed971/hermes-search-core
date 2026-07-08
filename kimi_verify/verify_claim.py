#!/usr/bin/env python3
"""
verify_claim.py — Standalone Claim Verifier
=============================================
Uses Claude Sonnet to verify individual claims.
Can be used independently or as part of hermes_search_v2.py.

Usage:
  python verify_claim.py "Python 3.11 was released in October 2022"
  python verify_claim.py --batch claims.json --output verified.json
  python verify_claim.py --interactive
  python verify_claim.py --source-tier academic "CRISPR was discovered in 2012"

Author: Built for Zayed (Hermes Agent)
Date: 2026-07-08
"""

import os
import sys
import json
import re
import argparse
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, asdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Configuration ──────────────────────────────────────────────────────────

CLAUDE_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_VERIFY_MODEL", "claude-sonnet-5")

# Source tier authority scores (match hermes_search_v2.py)
AUTHORITY_TIERS = {
    "gov": 10,
    "edu": 9,
    "org_official": 9,
    "academic": 8,
    "major_pub": 7,
    "tech_corp": 6,
    "blog_expert": 5,
    "forum": 3,
    "unknown": 1,
}

# ── Data Structures ────────────────────────────────────────────────────────

@dataclass
class VerificationResult:
    """Result of verifying a single claim."""
    claim_text: str
    verdict: str           # VERIFIED | PARTIAL | FAILED | DISPUTED | UNVERIFIABLE
    confidence: float      # 0.0-1.0
    reason: str
    red_flags: List[str]
    source_tier: str
    source_url: str
    source_title: str
    verification_method: str  # "claude" | "pattern" | "cross_ref"
    timestamp: str
    latency_ms: int

@dataclass
class BatchResult:
    """Result of batch verification."""
    results: List[VerificationResult]
    total_claims: int
    verified_count: int
    failed_count: int
    disputed_count: int
    partial_count: int
    avg_confidence: float
    avg_latency_ms: int
    timestamp: str

# ── Core Verifier ──────────────────────────────────────────────────────────

class ClaimVerifier:
    """Verify claims using Claude Sonnet + pattern matching."""

    def __init__(self, api_key: str = None, model: str = None):
        self.api_key = api_key or CLAUDE_API_KEY
        self.model = model or CLAUDE_MODEL
        self.client = None
        self.total_cost = 0.0

        if self.api_key:
            try:
                import anthropic
                self.client = anthropic.Anthropic(api_key=self.api_key)
            except ImportError:
                print("[ERROR] anthropic package not installed. Run: pip install anthropic", file=sys.stderr)
                sys.exit(1)
        else:
            print("[ERROR] ANTHROPIC_API_KEY not set in environment", file=sys.stderr)
            sys.exit(1)

    def verify(self, claim_text: str, source_url: str = "", source_title: str = "",
               source_tier: str = "unknown") -> VerificationResult:
        """Verify a single claim."""
        start = datetime.now(timezone.utc)

        # First: quick pattern-based checks
        pattern_result = self._pattern_check(claim_text)
        if pattern_result:
            # Pattern caught something definitive
            latency = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
            return VerificationResult(
                claim_text=claim_text,
                verdict=pattern_result["verdict"],
                confidence=pattern_result["confidence"],
                reason=pattern_result["reason"],
                red_flags=pattern_result["red_flags"],
                source_tier=source_tier,
                source_url=source_url,
                source_title=source_title,
                verification_method="pattern",
                timestamp=start.isoformat(),
                latency_ms=latency
            )

        # Second: Claude verification
        claude_result = self._claude_verify(claim_text, source_url, source_title, source_tier)
        latency = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)

        return VerificationResult(
            claim_text=claim_text,
            verdict=claude_result["verdict"],
            confidence=claude_result["confidence"],
            reason=claude_result["reason"],
            red_flags=claude_result["red_flags"],
            source_tier=source_tier,
            source_url=source_url,
            source_title=source_title,
            verification_method="claude",
            timestamp=start.isoformat(),
            latency_ms=latency
        )

    def verify_batch(self, claims: List[Dict], max_workers: int = 3) -> BatchResult:
        """Verify multiple claims in parallel."""
        start = datetime.now(timezone.utc)
        results = []

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for claim in claims:
                future = executor.submit(
                    self.verify,
                    claim.get("text", ""),
                    claim.get("source_url", ""),
                    claim.get("source_title", ""),
                    claim.get("source_tier", "unknown")
                )
                futures[future] = claim

            for future in as_completed(futures):
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    claim = futures[future]
                    results.append(VerificationResult(
                        claim_text=claim.get("text", "unknown"),
                        verdict="FAILED",
                        confidence=0.0,
                        reason=f"Verification error: {e}",
                        red_flags=["system_error"],
                        source_tier=claim.get("source_tier", "unknown"),
                        source_url=claim.get("source_url", ""),
                        source_title=claim.get("source_title", ""),
                        verification_method="error",
                        timestamp=start.isoformat(),
                        latency_ms=0
                    ))

        # Calculate stats
        verified = sum(1 for r in results if r.verdict == "VERIFIED")
        failed = sum(1 for r in results if r.verdict == "FAILED")
        disputed = sum(1 for r in results if r.verdict == "DISPUTED")
        partial = sum(1 for r in results if r.verdict == "PARTIAL")
        avg_conf = sum(r.confidence for r in results) / len(results) if results else 0
        avg_lat = sum(r.latency_ms for r in results) / len(results) if results else 0

        return BatchResult(
            results=results,
            total_claims=len(results),
            verified_count=verified,
            failed_count=failed,
            disputed_count=disputed,
            partial_count=partial,
            avg_confidence=avg_conf,
            avg_latency_ms=int(avg_lat),
            timestamp=start.isoformat()
        )

    def _pattern_check(self, claim_text: str) -> Optional[Dict]:
        """Quick pattern-based verification checks."""
        text_lower = claim_text.lower()

        # Check 1: Future dates
        current_year = datetime.now().year
        future_years = re.findall(r'\b(20[3-9]\d|2[1-9]\d{2})\b', claim_text)
        for fy in future_years:
            if int(fy) > current_year:
                return {
                    "verdict": "FAILED",
                    "confidence": 0.95,
                    "reason": f"Claim references future year {fy} (current year is {current_year})",
                    "red_flags": ["future_date", "temporal_impossibility"]
                }

        # Check 2: Known falsehoods (expandable database)
        known_falsehoods = [
            (r'\b(the earth is flat|flat earth)\b', "Flat Earth is scientifically disproven"),
            (r'\b(vaccines cause autism)\b', "No credible evidence links vaccines to autism"),
            (r'\b(5g causes (covid|coronavirus))\b', "5G does not cause COVID-19"),
            (r'\b(homeopathy (cures|treats) cancer)\b', "Homeopathy is not evidence-based cancer treatment"),
        ]

        for pattern, correction in known_falsehoods:
            if re.search(pattern, text_lower):
                return {
                    "verdict": "FAILED",
                    "confidence": 0.99,
                    "reason": correction,
                    "red_flags": ["known_falsehood", "pseudoscience"]
                }

        # Check 3: Self-contradictory
        contradiction_patterns = [
            (r'\b(all|every|always)\b.*\b(none|never|no one)\b', "Absolute contradiction"),
            (r'\b(is|are)\b.*\b(is not|are not|isn\'t|aren\'t)\b', "Direct negation"),
        ]

        for pattern, desc in contradiction_patterns:
            if re.search(pattern, text_lower):
                return {
                    "verdict": "FAILED",
                    "confidence": 0.90,
                    "reason": f"Self-contradictory claim detected: {desc}",
                    "red_flags": ["contradiction", "logical_error"]
                }

        # Check 4: Vague / unfalsifiable
        vague_patterns = [
            r'\b(some people say|many believe|it is said|they say)\b',
            r'\b(possibly|maybe|could be|might be|may be)\b.*\b(definitely|certainly|absolutely)\b',
        ]

        for pattern in vague_patterns:
            if re.search(pattern, text_lower):
                return {
                    "verdict": "PARTIAL",
                    "confidence": 0.40,
                    "reason": "Claim contains vague or unfalsifiable language",
                    "red_flags": ["vague_attribution", "unfalsifiable"]
                }

        return None  # No pattern match — need Claude

    def _claude_verify(self, claim_text: str, source_url: str, source_title: str,
                       source_tier: str) -> Dict:
        """Verify using Claude Sonnet."""
        tier_score = AUTHORITY_TIERS.get(source_tier, 1)

        prompt = f"""You are a rigorous fact-checking system. Evaluate the following claim with extreme skepticism.

CLAIM TO VERIFY:
"{claim_text}"

SOURCE INFORMATION:
- Title: {source_title or "Unknown"}
- URL: {source_url or "Unknown"}
- Authority Tier: {source_tier} (score: {tier_score}/10)
  (gov=10, edu=9, academic=8, major_pub=7, tech_corp=6, blog=5, forum=3, unknown=1)

INSTRUCTIONS:
1. Evaluate whether the claim is accurate based on your knowledge.
2. Consider the source authority — low-tier sources require extra scrutiny.
3. Look for: specific numbers, dates, named entities, causal claims.
4. If the claim is too vague to verify, say UNVERIFIABLE.
5. If you lack information to verify, say FAILED (do not guess).
6. If sources disagree on this claim, say DISPUTED.

RESPONSE FORMAT (exactly):
VERDICT: VERIFIED | PARTIAL | FAILED | DISPUTED | UNVERIFIABLE
CONFIDENCE: 0-100
REASON: One clear sentence explaining your verdict.
RED_FLAGS: comma-separated list, or "None"

Be strict. A claim that "sounds right" is not enough."""

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}]
            )

            content = response.content[0].text

            # Track cost
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            # Claude Sonnet pricing ~$3/M input, $15/M output
            cost = (input_tokens * 3 + output_tokens * 15) / 1_000_000
            self.total_cost += cost

            # Parse response
            return self._parse_claude_response(content)

        except Exception as e:
            return {
                "verdict": "FAILED",
                "confidence": 0.0,
                "reason": f"Claude API error: {e}",
                "red_flags": ["api_error"]
            }

    def _parse_claude_response(self, content: str) -> Dict:
        """Parse Claude's verification response."""
        verdict_match = re.search(r'VERDICT:\s*(VERIFIED|PARTIAL|FAILED|DISPUTED|UNVERIFIABLE)', content, re.IGNORECASE)
        confidence_match = re.search(r'CONFIDENCE:\s*(\d+)', content)
        reason_match = re.search(r'REASON:\s*(.+?)(?:\n|RED_FLAGS|$)', content, re.DOTALL)
        redflags_match = re.search(r'RED_FLAGS:\s*(.+)', content, re.DOTALL)

        verdict = verdict_match.group(1).upper() if verdict_match else "FAILED"
        confidence = int(confidence_match.group(1)) / 100.0 if confidence_match else 0.0
        reason = reason_match.group(1).strip() if reason_match else "No reason provided"

        red_flags_str = redflags_match.group(1).strip() if redflags_match else "None"
        red_flags = [f.strip() for f in red_flags_str.split(",") if f.strip().lower() != "none"]

        return {
            "verdict": verdict,
            "confidence": confidence,
            "reason": reason,
            "red_flags": red_flags
        }

    def get_cost_report(self) -> Dict:
        """Get cost summary."""
        return {
            "total_cost_usd": round(self.total_cost, 4),
            "model": self.model,
            "note": "Approximate cost at Sonnet pricing rates"
        }

# ── CLI Interface ──────────────────────────────────────────────────────────

def print_result(result: VerificationResult, verbose: bool = False):
    """Print a verification result."""
    verdict_emoji = {
        "VERIFIED": "✅",
        "PARTIAL": "⚠️",
        "FAILED": "❌",
        "DISPUTED": "⚡",
        "UNVERIFIABLE": "❓"
    }.get(result.verdict, "❓")

    print(f"\n{verdict_emoji} VERDICT: {result.verdict}")
    print(f"   Claim: {result.claim_text[:100]}{'...' if len(result.claim_text) > 100 else ''}")
    print(f"   Confidence: {result.confidence:.0%}")
    print(f"   Reason: {result.reason}")
    print(f"   Method: {result.verification_method}")
    print(f"   Latency: {result.latency_ms}ms")

    if result.red_flags:
        print(f"   Red Flags: {', '.join(result.red_flags)}")

    if verbose and result.source_url:
        print(f"   Source: {result.source_title} ({result.source_url[:60]})")


def interactive_mode(verifier: ClaimVerifier):
    """Interactive verification mode."""
    print("=" * 60)
    print("VERIFY CLAIM — Interactive Mode")
    print("Type a claim to verify, or 'quit' to exit.")
    print("=" * 60)

    while True:
        try:
            claim = input("\nClaim> ").strip()
            if claim.lower() in ("quit", "exit", "q"):
                break
            if not claim:
                continue

            result = verifier.verify(claim)
            print_result(result, verbose=True)

        except KeyboardInterrupt:
            print("\nExiting...")
            break
        except EOFError:
            break

    cost = verifier.get_cost_report()
    print(f"\nTotal cost: ${cost['total_cost_usd']}")


def main():
    parser = argparse.ArgumentParser(description="Verify claims using Claude Sonnet")
    parser.add_argument("claim", nargs="?", help="Claim to verify")
    parser.add_argument("--batch", type=str, help="JSON file with claims to verify")
    parser.add_argument("--output", "-o", type=str, help="Output file for results")
    parser.add_argument("--interactive", "-i", action="store_true", help="Interactive mode")
    parser.add_argument("--source-url", type=str, default="", help="Source URL")
    parser.add_argument("--source-title", type=str, default="", help="Source title")
    parser.add_argument("--source-tier", type=str, default="unknown", 
                        choices=list(AUTHORITY_TIERS.keys()),
                        help="Source authority tier")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    parser.add_argument("--max-workers", type=int, default=3, help="Max parallel workers for batch")

    args = parser.parse_args()

    verifier = ClaimVerifier()

    # Interactive mode
    if args.interactive or (not args.claim and not args.batch):
        interactive_mode(verifier)
        return

    # Batch mode
    if args.batch:
        with open(args.batch, "r", encoding="utf-8") as f:
            claims_data = json.load(f)

        if isinstance(claims_data, list):
            claims = claims_data
        else:
            claims = claims_data.get("claims", [])

        print(f"[BATCH] Verifying {len(claims)} claims...")
        batch_result = verifier.verify_batch(claims, max_workers=args.max_workers)

        # Print summary
        print(f"\n{'='*60}")
        print(f"BATCH VERIFICATION COMPLETE")
        print(f"{'='*60}")
        print(f"Total:    {batch_result.total_claims}")
        print(f"Verified: {batch_result.verified_count} ✅")
        print(f"Partial:  {batch_result.partial_count} ⚠️")
        print(f"Failed:   {batch_result.failed_count} ❌")
        print(f"Disputed: {batch_result.disputed_count} ⚡")
        print(f"Avg Confidence: {batch_result.avg_confidence:.0%}")
        print(f"Avg Latency: {batch_result.avg_latency_ms}ms")

        cost = verifier.get_cost_report()
        print(f"Total Cost: ~${cost['total_cost_usd']}")

        # Output
        output_data = {
            "summary": {
                "total": batch_result.total_claims,
                "verified": batch_result.verified_count,
                "partial": batch_result.partial_count,
                "failed": batch_result.failed_count,
                "disputed": batch_result.disputed_count,
                "avg_confidence": round(batch_result.avg_confidence, 2),
                "avg_latency_ms": batch_result.avg_latency_ms,
                "cost_usd": cost["total_cost_usd"],
            },
            "results": [asdict(r) for r in batch_result.results]
        }

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(output_data, f, indent=2, ensure_ascii=False)
            print(f"\n[✓] Saved to {args.output}")
        elif args.json:
            print(json.dumps(output_data, indent=2, ensure_ascii=False))

        return

    # Single claim mode
    result = verifier.verify(
        args.claim,
        source_url=args.source_url,
        source_title=args.source_title,
        source_tier=args.source_tier
    )

    if args.json:
        print(json.dumps(asdict(result), indent=2, ensure_ascii=False))
    else:
        print_result(result, verbose=args.verbose)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(asdict(result), f, indent=2, ensure_ascii=False)
        print(f"\n[✓] Saved to {args.output}")


if __name__ == "__main__":
    main()
