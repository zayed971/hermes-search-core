"""
verify_sprint.py — Gate checker for AI Software Company sprints
Run after every sprint. If it fails, sprint didn't happen — no claims made.
"""

import os
import sys
import json
from pathlib import Path

# ── CONFIG ──────────────────────────────────────────

SPRINT_FILE = "/mnt/c/Users/HP/Desktop/ai_company_sprint_output.md"
MEMORY_DIR = "/mnt/c/Users/HP/Desktop/ai_company_memory/chroma"
SPRINT_V2_FILE = "/mnt/c/Users/HP/Desktop/ai_company_sprint_v2_output.md"

REQUIRED_SECTIONS = [
    "PRD",
    "Design Doc",
    "Implementation",
    "QA",
    "DevOps",
]

CODE_SIGNATURES = ["def ", "class ", "import ", "from ", "async def", "app =", "Base ="]

MIN_SIZE_BYTES = 5000
DEEPSEEK_INPUT_COST = 0.27   # per million tokens
DEEPSEEK_OUTPUT_COST = 1.10  # per million tokens

passed = 0
failed = 0
warnings = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  ✅ {name}: {detail}")
        passed += 1
    else:
        print(f"  ❌ {name}: {detail}")
        failed += 1

def warn(name, detail=""):
    global warnings
    print(f"  ⚠️  {name}: {detail}")
    warnings += 1

# ── CHECKS ──────────────────────────────────────────

print("=" * 60)
print("VERIFY SPRINT — AI Software Company")
print("=" * 60)

# 1. Output file exists
print("\n── File Integrity ──")
v1_exists = os.path.exists(SPRINT_FILE)
v2_exists = os.path.exists(SPRINT_V2_FILE)
check("v1 output exists", v1_exists, SPRINT_FILE)
check("v2 output exists", v2_exists, SPRINT_V2_FILE)

if v2_exists:
    v2_size = os.path.getsize(SPRINT_V2_FILE)
    check("v2 output > 5KB", v2_size > MIN_SIZE_BYTES, f"{v2_size:,} bytes")
elif v1_exists:
    v1_size = os.path.getsize(SPRINT_FILE)
    check("v1 output > 5KB", v1_size > MIN_SIZE_BYTES, f"{v1_size:,} bytes")

# 2. Content checks
print("\n── Content Quality ──")

target_file = SPRINT_V2_FILE if v2_exists else SPRINT_FILE
if os.path.exists(target_file):
    with open(target_file, "r") as f:
        content = f.read()
    
    # Check sections
    for section in REQUIRED_SECTIONS:
        found = section in content
        check(f"'{section}' section present", found)
    
    # Check for actual code
    code_lines = 0
    for line in content.split("\n"):
        if any(sig in line for sig in CODE_SIGNATURES):
            code_lines += 1
    
    check("Contains Python code", code_lines > 10, f"{code_lines} code lines")
    
    if code_lines < 50:
        warn("Low code density — may be mostly documentation")
    
    # Check it's not placeholder
    placeholder_markers = ["TODO", "implement this", "your code here", "placeholder"]
    ph_count = sum(content.lower().count(m.lower()) for m in placeholder_markers)
    if ph_count > 5:
        warn(f"Contains {ph_count} placeholder markers — may be incomplete")

# 3. Memory checks (v2)
print("\n── Memory Layer (v2) ──")
if os.path.exists(MEMORY_DIR):
    # Count ChromaDB collections by looking for subdirectories
    sqlite_path = os.path.join(MEMORY_DIR, "chroma.sqlite3")
    check("ChromaDB sqlite exists", os.path.exists(sqlite_path))
    
    if os.path.exists(sqlite_path):
        db_size = os.path.getsize(sqlite_path)
        check("ChromaDB > 500KB", db_size > 500000, f"{db_size:,} bytes")
    
    # Check memory annotations in v2
    if v2_exists:
        mem_annotations = content.count("Memory:")
        check("Memory annotations in output", mem_annotations >= 5, f"{mem_annotations} annotations (need ≥5)")
else:
    warn("Memory directory not found — v2 sprint may not have run")

# 4. Cost estimate
print("\n── Cost Estimate ──")
if os.path.exists(target_file):
    with open(target_file, "r") as f:
        content = f.read()
    
    # Rough estimate: output chars ≈ tokens, input is typically 3-5x output
    output_chars = len(content)
    estimated_output_tokens = output_chars / 4  # ~4 chars per token
    estimated_input_tokens = estimated_output_tokens * 4  # prompts + context
    
    cost = (estimated_input_tokens / 1_000_000 * DEEPSEEK_INPUT_COST) + \
           (estimated_output_tokens / 1_000_000 * DEEPSEEK_OUTPUT_COST)
    
    print(f"  📊 Estimated input tokens: {estimated_input_tokens:,.0f}")
    print(f"  📊 Estimated output tokens: {estimated_output_tokens:,.0f}")
    print(f"  💰 Estimated DeepSeek API cost: ${cost:.3f}")
    
    if cost > 1.00:
        warn(f"Cost above $1.00 — consider shorter prompts")
    if cost < 0.01:
        warn("Cost suspiciously low — verify output is real")

# 5. Runtime evidence
print("\n── Runtime Evidence ──")
script_v1 = "/mnt/c/Users/HP/Desktop/ai_software_company.py"
script_v2 = "/mnt/c/Users/HP/Desktop/ai_software_company_v2.py"
check("v1 script exists", os.path.exists(script_v1))
check("v2 script exists", os.path.exists(script_v2))

# ── RESULT ──────────────────────────────────────────

print("\n" + "=" * 60)
total = passed + failed
print(f"RESULTS: {passed}/{total} passed, {failed} failed, {warnings} warnings")

if failed == 0 and warnings == 0:
    print("✅ SPRINT VERIFIED — All gates passed")
    sys.exit(0)
elif failed == 0:
    print("⚠️  SPRINT PASSED WITH WARNINGS — Review above")
    sys.exit(0)
else:
    print("❌ SPRINT FAILED — Fix issues above before making claims")
    sys.exit(1)
