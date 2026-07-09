#!/usr/bin/env python3
"""
Kimi Evaluation Framework — SEARCH-BENCH 250
=============================================
Implements Kimi's empirical evaluation protocol from the AI squeeze.
5 categories × 50 queries = 250 total.

Metrics:
  CF  — Citation Fidelity (CF-2/CF-1/CF-0/CF-H)
  RC  — Refusal Calibration (precision/recall of "I don't know")
  MHA — Multi-Hop Accuracy
  SD  — Source Diversity
  CH  — Contradiction Handling

Usage:
  python kimi_eval.py                               # score pipeline against bench
  python kimi_eval.py --category F1                 # only factual queries
  python kimi_eval.py --sample 10 --verify          # 10 random with Claude
"""

import json, sys, re, time, subprocess
from pathlib import Path
from collections import Counter

BENCH_FILE = Path("/mnt/c/Users/HP/Desktop/Researchs/ai squeeze/kimi SEARCH_BENCH_250.json")
PIPELINE    = "/mnt/c/Users/HP/Desktop/phase2_pipeline.py"
RESULTS_DIR = Path("/mnt/c/Users/HP/Desktop/kimi_eval_results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


# ═══ METRICS ═══

def score_citation_fidelity(answer: str, sources: list, claims: list) -> dict:
    """Kimi's CF rubric:
    CF-2: claim directly supported by cited source, URL accessible
    CF-1: claim partially supported, source relevant but doesn't fully substantiate
    CF-0: claim unsupported, source broken/irrelevant
    CF-H: claim hallucinated, no source cited or contradicts source
    """
    scores = {"CF-2": 0, "CF-1": 0, "CF-0": 0, "CF-H": 0}
    
    if not claims:
        scores["CF-H"] = 0
        return {"breakdown": scores, "cf_score": 0.0, "total": 0}
    
    for claim in claims:
        text = claim.get("text", "")
        source_url = claim.get("source_url", "")
        confidence = claim.get("confidence", 0)
        
        # CF-H: No source cited
        if not source_url:
            scores["CF-H"] += 1
            continue
        
        # CF-H: Hallucination markers
        hallucination_words = ["I think", "I believe", "maybe", "probably", "likely",
                               "it is said", "it is believed", "according to some"]
        if any(hw.lower() in text.lower() for hw in hallucination_words) and confidence < 0.5:
            scores["CF-H"] += 1
            continue
        
        # CF-2: High confidence verified claim
        if confidence >= 0.7:
            scores["CF-2"] += 1
        # CF-1: Partial confidence
        elif confidence >= 0.4:
            scores["CF-1"] += 1
        # CF-0: Low confidence or unverified
        else:
            scores["CF-0"] += 1
    
    total = sum(scores.values())
    # Kimi's formula: (2×CF-2 + 1×CF-1) / (2×Total_Claims)
    cf_score = (2 * scores["CF-2"] + scores["CF-1"]) / (2 * total) if total > 0 else 0
    return {"breakdown": scores, "cf_score": round(cf_score, 3), "total": total}


def score_refusal_calibration(answer: str, expected_behavior: str, claims: list) -> dict:
    """Kimi's RC metric:
    Correct Refusal: query unanswerable → says "I don't know"
    False Answer: query unanswerable → gives plausible wrong answer
    Correct Answer: query answerable → gives correct answer
    False Refusal: query answerable → refuses incorrectly
    """
    expects_refusal = "refusal" in expected_behavior.lower() or "REFUSE" in expected_behavior
    actually_refused = any(phrase in answer.lower() for phrase in [
        "no verified", "cannot answer", "i don't know", "not found",
        "no claims found", "no verified claims", "unable to",
        "does not exist", "not available", "unknown"
    ])
    has_claims = len(claims) > 0 and any(c.get("confidence", 0) > 0.5 for c in claims)
    
    if expects_refusal and actually_refused:
        outcome = "correct_refusal"
    elif expects_refusal and has_claims:
        outcome = "false_answer"
    elif not expects_refusal and has_claims:
        outcome = "correct_answer"
    elif not expects_refusal and not has_claims:
        outcome = "false_refusal"
    else:
        outcome = "correct_refusal"  # err on side of safety
    
    return {"outcome": outcome, "expects_refusal": expects_refusal,
            "actually_refused": actually_refused, "has_claims": has_claims}


def score_multi_hop(query_text: str, answer: str, ground_truth: str) -> dict:
    """Kimi's MHA: decompose into sub-questions, score each.
    Simplified: check if answer contains expected intermediate facts."""
    # Extract expected facts from ground truth
    facts = [f.strip() for f in ground_truth.split(";")]
    found = 0
    for fact in facts:
        # Simple substring match (real version would use NLI)
        keywords = fact.lower().split()
        if len(keywords) >= 2:
            # Check if ~60% of keywords present
            matches = sum(1 for w in keywords if w in answer.lower())
            if matches >= len(keywords) * 0.6:
                found += 1
    
    total = len(facts) if facts else 1
    return {"sub_facts": len(facts), "found": found, "mha_score": round(found / total, 3)}


def score_source_diversity(sources: list) -> dict:
    """Count unique domains cited."""
    domains = set()
    for s in sources:
        url = s.get("url", "")
        # Extract domain
        m = re.search(r'https?://([^/]+)', url)
        if m:
            domains.add(m.group(1))
    return {"unique_domains": len(domains), "domains": list(domains)[:10]}


def score_contradiction_handling(answer: str, ground_truth: str) -> dict:
    """CH: does the answer acknowledge conflict or falsely synthesize consensus?"""
    # Check for conflict acknowledgment
    acknowledges = any(phrase in answer.lower() for phrase in [
        "conflicting", "disagree", "disputed", "different", "varies",
        "depending on", "one side", "others argue", "however", "disputed",
        "both", "two", "multiple", "some sources", "while"
    ])
    # Check ground truth for expected conflict
    expects_conflict = any(w in ground_truth.lower() for w in [
        "differ", "vs", "versus", "conflicting", "both", "two", "multiple",
        "compar", ";", "debate", "depending"
    ])
    
    if expects_conflict and acknowledges:
        return {"ch_score": 1.0, "expects_conflict": True, "acknowledges": True}
    elif expects_conflict and not acknowledges:
        return {"ch_score": 0.0, "expects_conflict": True, "acknowledges": False}
    else:
        return {"ch_score": 1.0, "expects_conflict": False, "acknowledges": True}  # not applicable


# ═══ RUNNER ═══

def run_pipeline(query: str, verify: bool = False) -> dict:
    """Run phase2 pipeline, return parsed result."""
    args = ["python3", PIPELINE, query, "--json"]
    if not verify:
        args.insert(3, "--no-verify")
    
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return {"error": r.stderr[-200:]}
        return json.loads(r.stdout)
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    except json.JSONDecodeError:
        return {"error": "json parse", "stdout": r.stdout[:300] if 'r' in dir() else ""}
    except Exception as e:
        return {"error": str(e)}


def evaluate_query(bench_q: dict, verify: bool = False) -> dict:
    """Run one bench query through pipeline and score."""
    query = bench_q["query_text"]
    gt = bench_q.get("ground_truth", "")
    expected = bench_q.get("expected_behavior", "")
    category = bench_q["category"]
    
    t0 = time.time()
    result = run_pipeline(query, verify=verify)
    elapsed = time.time() - t0
    
    if "error" in result:
        return {"query_id": bench_q["query_id"], "error": result["error"],
                "category": category}
    
    # Extract claims from answer (simplified — pipeline returns structured claims)
    claims_list = []
    # The pipeline returns "verified" and "total" but not individual claim details in JSON
    # We approximate: if confidence > 0, some claims passed
    answer = result.get("answer", "")
    confidence = result.get("confidence", 0)
    sources = result.get("sources", [])
    verified_count = result.get("verified", 0)
    
    # Build synthetic claims for scoring
    claims = []
    if verified_count > 0 and confidence > 0:
        # Extract claim-like sentences from answer
        claim_sentences = re.findall(r'[^.!?]+\b(is|was|are|were|has|have|can|will)\b[^.!?]*[.!?]', answer)
        for i, cs in enumerate(claim_sentences[:15]):
            src_idx = i % len(sources) if sources else 0
            claims.append({
                "text": cs.strip(),
                "source_url": sources[src_idx]["url"] if sources else "",
                "confidence": confidence
            })
    
    # Score
    cf = score_citation_fidelity(answer, sources, claims)
    rc = score_refusal_calibration(answer, expected, claims)
    mha = score_multi_hop(query, answer, gt) if "F3" in category else {"mha_score": "N/A"}
    sd = score_source_diversity(sources)
    ch = score_contradiction_handling(answer, gt)
    
    return {
        "query_id": bench_q["query_id"],
        "category": category,
        "subcategory": bench_q.get("subcategory", ""),
        "difficulty": bench_q.get("difficulty", ""),
        "query": query,
        "confidence": confidence,
        "verified": verified_count,
        "total_claims": result.get("total", 0),
        "sources_count": len(sources),
        "elapsed": round(elapsed, 2),
        "cf_score": cf["cf_score"],
        "cf_breakdown": cf["breakdown"],
        "rc_outcome": rc["outcome"],
        "mha_score": mha.get("mha_score", "N/A"),
        "sd_score": sd["unique_domains"],
        "ch_score": ch["ch_score"],
        "cost": result.get("cost", "N/A"),
        "answer_preview": answer[:300]
    }


# ═══ MAIN ═══

def main():
    import argparse, random
    ap = argparse.ArgumentParser(description="Kimi Evaluation Framework — SEARCH-BENCH 250")
    ap.add_argument("--category", choices=["F1_Factual_Verification","F2_Temporal_Freshness",
        "F3_MultiHop_Reasoning","F4_Comparative_Synthesis","F5_Adversarial_Edge"],
        help="Single category")
    ap.add_argument("--f1", action="store_true", help="Shorthand for F1 (factual)")
    ap.add_argument("--f5", action="store_true", help="Shorthand for F5 (adversarial)")
    ap.add_argument("--sample", type=int, default=0, help="Random sample N queries")
    ap.add_argument("--verify", action="store_true", help="Full Claude verification")
    ap.add_argument("--start", type=int, default=0, help="Start index")
    ap.add_argument("--limit", type=int, default=0, help="Max queries")
    args = ap.parse_args()
    
    with open(BENCH_FILE) as f:
        bench = json.load(f)
    
    # Filter
    queries = bench
    if args.f1:
        queries = [q for q in queries if q["category"] == "F1_Factual_Verification"]
    if args.f5:
        queries = [q for q in queries if q["category"] == "F5_Adversarial_Edge"]
    if args.category:
        queries = [q for q in queries if q["category"] == args.category]
    if args.sample > 0:
        queries = random.sample(queries, min(args.sample, len(queries)))
    if args.start > 0:
        queries = queries[args.start:]
    if args.limit > 0:
        queries = queries[:args.limit]
    
    print(f"SEARCH-BENCH 250 — Kimi Evaluation Framework")
    print(f"{len(queries)} queries | verify={'ON' if args.verify else 'OFF'}")
    print("="*70)
    
    results = []
    total_cost = 0
    
    for i, q in enumerate(queries):
        print(f"\n[{i+1}/{len(queries)}] {q['category']} {q.get('difficulty','?')}: {q['query_text'][:80]}")
        r = evaluate_query(q, verify=args.verify)
        
        if "error" in r:
            print(f"  ❌ {r['error'][:80]}")
        else:
            cost_str = r.get("cost", "$0").replace("$","")
            try: total_cost += float(cost_str)
            except: pass
            mha = f" MHA:{r['mha_score']}" if r['mha_score'] != 'N/A' else ""
            print(f"  CF:{r['cf_score']:.2f} RC:{r['rc_outcome']} SD:{r['sd_score']}{mha} CH:{r['ch_score']} | "
                  f"Conf:{r['confidence']:.0%} V:{r['verified']}/{r['total_claims']} S:{r['sources_count']} | "
                  f"{r['elapsed']}s {r['cost']}")
        
        results.append(r)
        
        # Save incrementally
        with open(RESULTS_DIR / "latest.json", "w") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
    
    # Summary
    valid = [r for r in results if "error" not in r]
    if valid:
        cf_avg = sum(r["cf_score"] for r in valid) / len(valid)
        sd_avg = sum(r["sd_score"] for r in valid) / len(valid)
        ch_avg = sum(r["ch_score"] for r in valid) / len(valid)
        conf_avg = sum(r["confidence"] for r in valid) / len(valid)
        
        # RC breakdown
        rc_counts = Counter(r["rc_outcome"] for r in valid)
        correct_refusals = rc_counts.get("correct_refusal", 0)
        false_answers = rc_counts.get("false_answer", 0)
        precision_rc = correct_refusals / (correct_refusals + false_answers) if (correct_refusals + false_answers) > 0 else 0
        
        print("\n" + "="*70)
        print("  KIMI EVALUATION SUMMARY")
        print("="*70)
        print(f"  Queries:          {len(valid)}")
        print(f"  CF Score:         {cf_avg:.2f}  (Citation Fidelity)")
        print(f"  SD Score:         {sd_avg:.1f}  (Source Diversity)")
        print(f"  CH Score:         {ch_avg:.2f}  (Contradiction Handling)")
        print(f"  RC Precision:     {precision_rc:.2f}  (Refusal Calibration)")
        print(f"  Avg Confidence:   {conf_avg:.0%}")
        print(f"  Total Cost:       ${total_cost:.3f}")
        print(f"  Results saved:    {RESULTS_DIR / 'latest.json'}")
        
        # By category
        print(f"\n  By Category:")
        cats = {}
        for r in valid:
            c = r["category"]
            if c not in cats: cats[c] = []
            cats[c].append(r["cf_score"])
        for cat, scores in sorted(cats.items()):
            print(f"    {cat}: CF={sum(scores)/len(scores):.2f} ({len(scores)} queries)")


if __name__ == "__main__":
    main()
