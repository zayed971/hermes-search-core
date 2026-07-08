#!/usr/bin/env python3
"""
SEARCH-BENCH Runner
====================
Runs Phase 2 pipeline against 10 benchmark queries.
Scores: confidence, verified claims, source diversity, sanity, citations.
"""

import json, subprocess, sys, re, time
from pathlib import Path

BENCH_FILE = Path("/mnt/c/Users/HP/Desktop/search_bench.json")
PIPELINE   = "/mnt/c/Users/HP/Desktop/phase2_pipeline.py"

def load_bench():
    with open(BENCH_FILE) as f:
        return json.load(f)

def run_query(query: str, verify: bool = True) -> dict:
    """Run a single query through the pipeline. Returns parsed result."""
    args = ["python3", PIPELINE, query, "--json"]
    if not verify:
        args.insert(3, "--no-verify")
    
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            return {"error": r.stderr[-200:], "stdout": r.stdout[-200:]}
        
        # Parse JSON from output
        data = json.loads(r.stdout)
        return data
    except subprocess.TimeoutExpired:
        return {"error": "timeout after 300s"}
    except json.JSONDecodeError:
        return {"error": "JSON parse failed", "stdout": r.stdout[:500] if 'r' in dir() else ""}
    except Exception as e:
        return {"error": str(e)}

def score_result(result: dict, bench_query: dict, scoring: dict) -> dict:
    """Score a single benchmark result."""
    scores = {}
    
    # 1. Confidence score
    conf = result.get("confidence", 0)
    min_conf = bench_query.get("min_confidence", 0.5)
    scores["confidence"] = min(1.0, conf / max(min_conf, 0.01)) * scoring["confidence_weight"]
    
    # 2. Verified claims score
    verified = result.get("verified", 0)
    total = result.get("total", 0)
    if total > 0:
        scores["verified_claims"] = (verified / total) * scoring["verified_claims_weight"]
    else:
        scores["verified_claims"] = 0
    
    # 3. Source diversity
    sources = result.get("sources", [])
    tiers = set(s.get("tier","") for s in sources)
    scores["source_diversity"] = min(1.0, len(tiers) / 3) * scoring["source_diversity_weight"]
    
    # 4. Sanity clean
    sanity_flags = result.get("sanity_flags", [])
    red_flags = [f for f in sanity_flags if f.startswith("🔴")]
    scores["sanity_clean"] = max(0, 1.0 - len(red_flags) * 0.5) * scoring["sanity_clean_weight"]
    
    # 5. Citations present
    answer = result.get("answer", "")
    has_citations = bool(re.search(r'\[(\d+)\]', answer))
    scores["has_citations"] = (1.0 if has_citations else 0.0) * scoring["has_citations_weight"]
    
    total_score = sum(scores.values())
    
    return {
        "scores": scores,
        "total": round(total_score, 3),
        "confidence_raw": conf,
        "verified_raw": verified,
        "total_raw": total,
        "source_count": len(sources),
        "tier_count": len(tiers),
        "red_flags": len(red_flags),
        "has_citations": has_citations,
        "elapsed": result.get("elapsed", 0),
        "cost": result.get("cost", "N/A")
    }

def main():
    bench = load_bench()
    scoring = bench["scoring"]
    queries = bench["queries"]
    
    print("="*70)
    print("  HERMES SEARCH-BENCH v1 — Phase 2 Pipeline Benchmark")
    print(f"  {len(queries)} queries | scoring: {json.dumps(scoring)}")
    print("="*70)
    
    # Sample: first 3 with full verification, rest fast
    verify_sample = 3  # full Claude verification on first N
    results = []
    total_cost = 0
    
    for i, q in enumerate(queries):
        verify = i < verify_sample
        mode = "FULL" if verify else "FAST"
        
        print(f"\n[{i+1}/{len(queries)}] {mode} | {q['category']}: {q['query'][:70]}")
        t0 = time.time()
        
        result = run_query(q["query"], verify=verify)
        
        if "error" in result:
            print(f"  ❌ ERROR: {result['error'][:100]}")
            results.append({"id": q["id"], "error": result["error"]})
            continue
        
        scored = score_result(result, q, scoring)
        results.append({"id": q["id"], "query": q["query"], "category": q["category"],
                        "mode": mode, "scored": scored, "answer_preview": result.get("answer","")[:200]})
        
        elapsed = time.time() - t0
        cost = result.get("cost", "$0")
        total_cost += float(cost.replace("$","")) if isinstance(cost, str) and "$" in cost else 0
        
        print(f"  {'✅' if scored['total'] > 0.5 else '⚠️'} "
              f"Score: {scored['total']:.2f} | "
              f"Conf: {scored['confidence_raw']:.0%} | "
              f"Verified: {scored['verified_raw']}/{scored['total_raw']} | "
              f"Sources: {scored['source_count']} | "
              f"Red flags: {scored['red_flags']} | "
              f"Time: {elapsed:.0f}s | "
              f"Cost: {cost}")
    
    # Summary
    print("\n" + "="*70)
    print("  BENCHMARK SUMMARY")
    print("="*70)
    
    valid = [r for r in results if "error" not in r]
    if valid:
        avg_score = sum(r["scored"]["total"] for r in valid) / len(valid)
        avg_conf  = sum(r["scored"]["confidence_raw"] for r in valid) / len(valid)
        avg_verified = sum(r["scored"]["verified_raw"] for r in valid) / len(valid)
        avg_sources = sum(r["scored"]["source_count"] for r in valid) / len(valid)
        full_queries = [r for r in valid if r["mode"] == "FULL"]
        
        print(f"  Overall Score:     {avg_score:.2f}/1.00")
        print(f"  Avg Confidence:    {avg_conf:.0%}")
        print(f"  Avg Verified:      {avg_verified:.1f}")
        print(f"  Avg Sources:       {avg_sources:.1f}")
        print(f"  Total Cost:        ${total_cost:.3f}")
        print(f"  Full Verify Runs:  {len(full_queries)}")
        print(f"  Errors:            {len(results) - len(valid)}")
        
        # Per-category breakdown
        print(f"\n  By Category:")
        cats = {}
        for r in valid:
            cat = r["category"]
            if cat not in cats:
                cats[cat] = []
            cats[cat].append(r["scored"]["total"])
        for cat, scores in sorted(cats.items()):
            print(f"    {cat:<15} {sum(scores)/len(scores):.2f} ({len(scores)} queries)")
    
    # Save report
    report = {
        "bench": "SEARCH-BENCH v1",
        "pipeline": "phase2_pipeline.py",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            "overall_score": round(avg_score, 2) if valid else "N/A",
            "avg_confidence": round(avg_conf, 2) if valid else "N/A",
            "total_cost": round(total_cost, 3),
            "queries_run": len(results),
            "errors": len(results) - len(valid)
        },
        "results": [{
            "id": r["id"],
            "query": r["query"],
            "category": r["category"],
            "mode": r["mode"],
            "score": r["scored"]["total"],
            "confidence_raw": r["scored"]["confidence_raw"],
            "verified_raw": r["scored"]["verified_raw"],
            "sources": r["scored"]["source_count"],
            "red_flags": r["scored"]["red_flags"]
        } for r in valid]
    }
    
    report_path = Path("/mnt/c/Users/HP/Desktop/search_bench_results.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n  Report saved: {report_path}")
    
    return report

if __name__ == "__main__":
    main()
