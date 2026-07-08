#!/usr/bin/env python3
"""
hard_verify.py — Hermes Hard-Default Verification Pipeline
============================================================
Fuses TWO verification systems into one mandatory gate:

  SYSTEM A — Infrastructure Verifier (AgentOS)
    Checks: files exist, commands succeed, URLs are reachable.
    Zero AI. Pure Python stdlib. Instant.

  SYSTEM B — Fact Verifier (Kimi/Claude)
    Checks: factual claims via Claude Sonnet.
    AI-powered. ~$0.003 per claim.

Every claim Hermes makes MUST pass through this pipeline before reaching the user.
NO EXCEPTIONS unless user explicitly says "--no-verify" or "skip verification".

Usage:
  python hard_verify.py "Python 3.11 was released in October 2022"
  python hard_verify.py --ref "file:/mnt/c/Users/HP/Desktop/report.pdf"
  python hard_verify.py --ref "url:https://api.example.com/health"
  python hard_verify.py --ref "cmd:pytest tests/ -q"
  python hard_verify.py --batch claims.json
  python hard_verify.py --json -- "Some claim text"

Output modes:
  --json     → Machine-readable JSON
  --brief    → One-line verdict only
  (default)  → Human-readable with emoji

Author: Built for Zayed (Hermes Agent) — 2026-07-09
"""

import os
import sys
import json
import re
import hashlib
import subprocess
import urllib.error
import urllib.request
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Tuple

# ── Configuration ──────────────────────────────────────────────────────────

def _load_anthropic_key():
    """Load Anthropic API key from env or .env file."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key and len(key) > 10:
        return key
    # Fallback: read from Hermes .env file
    env_path = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if "ANTHROPIC_API_KEY" in line and not line.strip().startswith("#"):
                    val = line.strip().split("=", 1)
                    if len(val) == 2 and len(val[1]) > 10:
                        return val[1]
    return ""

ANTHROPIC_API_KEY = _load_anthropic_key()
CLAUDE_MODEL = "claude-sonnet-4-5-20250929"
HAIKU_MODEL = "claude-haiku-4-5-20251001"

COMMAND_TIMEOUT = 30
URL_TIMEOUT = 10

# ── System A: Infrastructure Verifier (AgentOS) ───────────────────────────

def _split_ref(artifact_ref: str) -> Tuple[str, str]:
    """Return (kind, value) for an artifact_ref string."""
    ref = (artifact_ref or "").strip()
    for scheme in ("file", "cmd", "url"):
        prefix = scheme + ":"
        if ref.startswith(prefix):
            return scheme, ref[len(prefix):].strip()
    if ref.startswith("http://") or ref.startswith("https://"):
        return "url", ref
    return "file", ref


def _verify_file(path_str: str) -> Tuple[bool, str]:
    path = Path(path_str)
    if not path.is_file():
        return False, f"FILE NOT FOUND: {path_str}"
    stat = path.stat()
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    return True, (
        f"FILE EXISTS: {path_str} | "
        f"size={stat.st_size}B | "
        f"sha256={sha256[:16]}..."
    )


def _verify_command(command: str, timeout: int = COMMAND_TIMEOUT) -> Tuple[bool, str]:
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return False, f"COMMAND TIMED OUT ({timeout}s): {command}"
    except OSError as e:
        return False, f"COMMAND FAILED TO START: {command} ({e})"
    ok = result.returncode == 0
    stdout_tail = (result.stdout or "")[-200:]
    stderr_tail = (result.stderr or "")[-200:]
    return ok, (
        f"COMMAND exit={result.returncode} | "
        f"stdout_tail={stdout_tail!r} | "
        f"stderr_tail={stderr_tail!r}"
    )


def _verify_url(url: str, timeout: int = URL_TIMEOUT) -> Tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"URL UNREACHABLE: {url} ({e})"
    ok = 200 <= status < 400
    return ok, f"URL status={status} | {url}"


def infrastructure_verify(artifact_ref: str) -> Dict:
    """System A: Verify a file, command, or URL."""
    kind, value = _split_ref(artifact_ref)
    if not value:
        return {"ok": False, "evidence": "EMPTY artifact_ref", "system": "A-infra"}
    if kind == "file":
        ok, evidence = _verify_file(value)
    elif kind == "cmd":
        ok, evidence = _verify_command(value)
    elif kind == "url":
        ok, evidence = _verify_url(value)
    else:
        ok, evidence = False, f"UNKNOWN artifact_ref kind: {kind}"
    return {"ok": ok, "evidence": evidence, "system": "A-infra"}


# ── System B: Fact Verifier (Kimi/Claude) ─────────────────────────────────

def _detect_artifact_refs(text: str) -> List[str]:
    """Detect if claim contains file/cmd/url references that System A can verify."""
    refs = []
    for match in re.finditer(r'\b(file|cmd|url):(\S+)', text):
        refs.append(match.group(0))
    for match in re.finditer(r'https?://\S+', text):
        refs.append(match.group(0))
    for match in re.finditer(r'(?:/mnt/)?[cC]:/(?:Users/\S+?/\S+?\.\w{2,4})', text):
        refs.append(match.group(0))
    for match in re.finditer(r'/home/\S+?/\S+?\.\w{2,4}', text):
        refs.append(match.group(0))
    return refs


def _is_local_claim(text: str) -> bool:
    """Detect if claim references local state Claude can't see."""
    local_patterns = [
        r'\b(Desktop|hermes_core|hermes_verify|AgentOS)\b',
        r'\b(repo|repository|git repo)\b',
        r'\b(local files?|on disk|on your machine|on my Desktop)\b',
        r'\bcontains?\s+(AgentOS|Kimi|code|files?)\b',
        r'/mnt/c/Users/', r'/home/hp/',
    ]
    for pat in local_patterns:
        if re.search(pat, text, re.IGNORECASE):
            return True
    return False


def _gather_local_evidence(claim_text: str) -> str:
    """Gather file listings for local claims so Claude can verify."""
    evidence_parts = []
    
    # Detect paths mentioned
    paths = []
    if 'hermes_core' in claim_text.lower():
        paths.append('/mnt/c/Users/HP/Desktop/hermes_core')
    if 'Desktop' in claim_text:
        paths.append('/mnt/c/Users/HP/Desktop')
    
    for base in paths:
        if os.path.exists(base):
            try:
                # Show directory structure (ls -R, limited depth)
                cmd = f"find {base} -maxdepth 3 -not -path '*/.git/*' | sort | head -40"
                result = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=5
                )
                if result.stdout.strip():
                    evidence_parts.append(f"Directory structure of {base}:\n{result.stdout.strip()[:3000]}")
            except:
                pass
    
    if evidence_parts:
        return "\n\nEVIDENCE (gathered from local system — use this to verify):\n" + "\n".join(evidence_parts)
    return ""


def _claude_verify(claim_text: str) -> Dict:
    """System B: Verify factual claim using Claude Sonnet."""
    if not ANTHROPIC_API_KEY:
        return {
            "verdict": "UNVERIFIABLE",
            "confidence": 0.0,
            "reason": "ANTHROPIC_API_KEY not set — cannot verify",
            "red_flags": ["no_api_key"],
            "system": "B-fact"
        }

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    except ImportError:
        return {
            "verdict": "UNVERIFIABLE",
            "confidence": 0.0,
            "reason": "anthropic package not installed",
            "red_flags": ["missing_dependency"],
            "system": "B-fact"
        }

    # Check if this is a local claim — if so, gather evidence
    evidence = ""
    if _is_local_claim(claim_text):
        evidence = _gather_local_evidence(claim_text)

    prompt = f"""You are a rigorous fact-checker. Verify this claim:

CLAIM: "{claim_text}"
{evidence}
RULES:
- Be strict. "Sounds right" is not enough.
- If you lack knowledge, say FAILED — never guess.
- If sources disagree, say DISPUTED.
- If too vague, say UNVERIFIABLE.
- Consider: dates, numbers, named entities, causal claims.
- If EVIDENCE is provided above, use it to verify the claim.

Respond EXACTLY:
VERDICT: VERIFIED | PARTIAL | FAILED | DISPUTED | UNVERIFIABLE
CONFIDENCE: 0-100
REASON: One clear sentence.
RED_FLAGS: comma-separated, or "None" """

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        content = response.content[0].text

        verdict_m = re.search(r'VERDICT:\s*(VERIFIED|PARTIAL|FAILED|DISPUTED|UNVERIFIABLE)', content, re.IGNORECASE)
        conf_m = re.search(r'CONFIDENCE:\s*(\d+)', content)
        reason_m = re.search(r'REASON:\s*(.+?)(?:\n|RED_FLAGS|$)', content, re.DOTALL)
        flags_m = re.search(r'RED_FLAGS:\s*(.+)', content, re.DOTALL)

        verdict = verdict_m.group(1).upper() if verdict_m else "FAILED"
        confidence = int(conf_m.group(1)) / 100.0 if conf_m else 0.0
        reason = reason_m.group(1).strip() if reason_m else "No reason"
        flags_str = flags_m.group(1).strip() if flags_m else "None"
        red_flags = [f.strip() for f in flags_str.split(",") if f.strip().lower() != "none"]

        return {
            "verdict": verdict,
            "confidence": confidence,
            "reason": reason,
            "red_flags": red_flags,
            "system": "B-fact"
        }
    except Exception as e:
        return {
            "verdict": "FAILED",
            "confidence": 0.0,
            "reason": f"Claude API error: {e}",
            "red_flags": ["api_error"],
            "system": "B-fact"
        }


# ── Quick Pattern Checks (no API call) ────────────────────────────────────

def _pattern_check(text: str) -> Optional[Dict]:
    """Quick pattern-based verification — catches obvious lies instantly."""
    text_lower = text.lower()
    current_year = datetime.now().year

    # Future dates
    future_years = re.findall(r'\b(20[3-9]\d|2[1-9]\d{2})\b', text)
    for fy in future_years:
        if int(fy) > current_year:
            return {
                "verdict": "FAILED",
                "confidence": 0.95,
                "reason": f"References future year {fy} (current: {current_year})",
                "red_flags": ["future_date"],
                "system": "pattern"
            }

    # Known falsehoods
    falsehoods = [
        (r'\b(flat earth)\b', "Flat Earth is scientifically disproven"),
        (r'\b(vaccines cause autism)\b', "No credible evidence links vaccines to autism"),
        (r'\b(5g causes covid)\b', "5G does not cause COVID-19"),
    ]
    for pattern, correction in falsehoods:
        if re.search(pattern, text_lower):
            return {
                "verdict": "FAILED",
                "confidence": 0.99,
                "reason": correction,
                "red_flags": ["known_falsehood"],
                "system": "pattern"
            }

    # Self-contradiction
    if re.search(r'\b(all|every|always)\b.*\b(none|never|no one)\b', text_lower):
        return {
            "verdict": "FAILED",
            "confidence": 0.90,
            "reason": "Self-contradictory claim detected",
            "red_flags": ["contradiction"],
            "system": "pattern"
        }

    # Vague
    if re.search(r'\b(some people say|many believe|it is said)\b', text_lower):
        return {
            "verdict": "PARTIAL",
            "confidence": 0.30,
            "reason": "Vague, unfalsifiable language — cannot verify",
            "red_flags": ["vague_attribution"],
            "system": "pattern"
        }

    return None


# ── Unified Verification ──────────────────────────────────────────────────

def hard_verify(claim_or_ref: str) -> Dict:
    """
    THE GATE. Every claim passes through here.
    
    Decision tree:
    1. Is it an artifact_ref (file:/cmd:/url:)? → System A only
    2. Is it a factual claim? → Pattern check → System B (Claude)
    3. Does it contain embedded refs? → System A on refs + System B on text
    """
    result = {
        "input": claim_or_ref,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "infra_check": None,
        "fact_check": None,
        "overall_verdict": "UNVERIFIED",
        "overall_confidence": 0.0,
        "gate": "PASS"
    }

    # Branch 1: Explicit artifact ref → System A only
    if re.match(r'^(file|cmd|url):', claim_or_ref):
        infra = infrastructure_verify(claim_or_ref)
        result["infra_check"] = infra
        result["overall_verdict"] = "VERIFIED" if infra["ok"] else "FAILED"
        result["overall_confidence"] = 0.99 if infra["ok"] else 0.0
        result["gate"] = "PASS" if infra["ok"] else "BLOCK"
        return result

    # Branch 2: Looks like a URL → System A
    if re.match(r'^https?://', claim_or_ref):
        infra = infrastructure_verify(f"url:{claim_or_ref}")
        result["infra_check"] = infra
        result["overall_verdict"] = "VERIFIED" if infra["ok"] else "FAILED"
        result["overall_confidence"] = 0.99 if infra["ok"] else 0.0
        result["gate"] = "PASS" if infra["ok"] else "BLOCK"
        return result

    # Branch 3: Factual claim → pattern check → Claude
    embedded_refs = _detect_artifact_refs(claim_or_ref)
    if embedded_refs:
        infra_results = []
        for ref in embedded_refs:
            infra_results.append(infrastructure_verify(ref))
        result["infra_check"] = infra_results

    # Pattern check
    pattern = _pattern_check(claim_or_ref)
    if pattern:
        result["fact_check"] = pattern
        result["overall_verdict"] = pattern["verdict"]
        result["overall_confidence"] = pattern["confidence"]
        result["gate"] = "PASS" if pattern["verdict"] == "VERIFIED" else "BLOCK"
        return result

    # Claude verification
    claude = _claude_verify(claim_or_ref)
    result["fact_check"] = claude
    result["overall_verdict"] = claude["verdict"]
    result["overall_confidence"] = claude["confidence"]
    result["gate"] = "PASS" if claude["verdict"] in ("VERIFIED", "PARTIAL") else "BLOCK"

    return result


# ── CLI ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Hermes Hard-Default Verification Pipeline"
    )
    parser.add_argument(
        "claim", nargs="?", 
        help="Claim text or artifact_ref (file:/cmd:/url:)"
    )
    parser.add_argument("--ref", type=str, help="Artifact ref (file:/cmd:/url:)")
    parser.add_argument("--batch", type=str, help="JSON file with claims array")
    parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    parser.add_argument("--brief", "-b", action="store_true", help="One-line verdict only")

    args = parser.parse_args()

    if not args.claim and not args.ref and not args.batch:
        parser.print_help()
        sys.exit(1)

    target = args.ref or args.claim

    if args.batch:
        with open(args.batch, "r") as f:
            claims = json.load(f)
        if not isinstance(claims, list):
            claims = claims.get("claims", [])
        results = []
        for c in claims:
            text = c if isinstance(c, str) else c.get("text", "")
            results.append(hard_verify(text))
        
        if args.json:
            print(json.dumps(results, indent=2, ensure_ascii=False))
        else:
            for r in results:
                _print_result(r, brief=args.brief)
        return

    result = hard_verify(target)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _print_result(result, brief=args.brief)

    sys.exit(0 if result["gate"] == "PASS" else 1)


def _print_result(result: Dict, brief: bool = False):
    v = result["overall_verdict"]
    emoji = {"VERIFIED": "✅", "PARTIAL": "⚠️", "FAILED": "❌",
             "DISPUTED": "⚡", "UNVERIFIABLE": "❓", "UNVERIFIED": "⏳"}.get(v, "❓")

    if brief:
        gate = "🟢 PASS" if result["gate"] == "PASS" else "🔴 BLOCK"
        print(f"{emoji} {v} ({result['overall_confidence']:.0%}) | {gate}")
        return

    print(f"\n{'='*60}")
    print(f"  HERMES HARD VERIFY — GATE: {result['gate']}")
    print(f"{'='*60}")
    print(f"  Input:    {result['input'][:120]}")
    print(f"  Verdict:  {emoji} {v}")
    print(f"  Conf:     {result['overall_confidence']:.0%}")

    if result.get("infra_check"):
        ic = result["infra_check"]
        if isinstance(ic, list):
            for i, check in enumerate(ic):
                print(f"  Infra[{i}]: {'✅' if check['ok'] else '❌'} {check['evidence'][:100]}")
        else:
            print(f"  Infra:    {'✅' if ic['ok'] else '❌'} {ic['evidence'][:100]}")

    if result.get("fact_check"):
        fc = result["fact_check"]
        print(f"  Reason:   {fc.get('reason', 'N/A')}")
        red_flags = fc.get('red_flags', [])
        if red_flags:
            print(f"  Flags:    {', '.join(red_flags)}")

    print(f"  Time:     {result['timestamp']}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
