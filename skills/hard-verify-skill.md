---
name: hard-verify
description: MANDATORY verification gate — every factual claim and artifact reference MUST pass through hard_verify.py before reaching the user. This is the DEFAULT. No claim leaves Hermes' mouth unverified.
category: devops
auto_load: true
priority: critical
---

# HARD VERIFY — Default Verification Gate

## RULE (MANDATORY — NO EXCEPTIONS)

**Every factual claim, file reference, URL, or command output you present to the user MUST be verified through `/home/hp/.hermes/scripts/hard_verify.py` BEFORE you say it.**

This is not optional. This is not "when you think it's important." This is ALWAYS.

## What Needs Verification

### Category A — Infrastructure (instant, no API cost)
- File existence claims: "I saved the report to Desktop/report.pdf" → `hard_verify.py --ref "file:/mnt/c/Users/HP/Desktop/report.pdf"`
- URL reachability: "The API is at https://api.example.com" → `hard_verify.py --ref "url:https://api.example.com"`
- Command success: "The tests passed" → `hard_verify.py --ref "cmd:pytest tests/ -q"`
- Build output: "make check succeeded" → `hard_verify.py --ref "cmd:make check"`

### Category B — Factual Claims (Claude API, ~$0.003/claim)
- Dates and numbers: "Python 3.11 was released in October 2022"
- Named entities: "Microsoft acquired GitHub in 2018"
- Technical facts: "FastAPI uses Starlette underneath"
- Causal claims: "The bug was caused by a race condition in the middleware"
- Comparisons: "Claude Sonnet 5 outperforms GPT-4 on coding benchmarks"

### Category C — Embedded References
When a claim contains BOTH factual content AND file/URL references, run BOTH checks.

## HOW TO VERIFY

```bash
# Single claim
python /home/hp/.hermes/scripts/hard_verify.py "your claim text here" --brief

# Artifact ref
python /home/hp/.hermes/scripts/hard_verify.py --ref "file:/path/to/file" --brief

# JSON output (for parsing)
python /home/hp/.hermes/scripts/hard_verify.py "claim" --json
```

**Exit code 0 = PASS (verified). Exit code 1 = BLOCK (failed).**

## Verification Flow

```
You want to say: "The report is at Desktop/analysis.md and shows 15% growth"

1. Check infrastructure: hard_verify.py --ref "file:/mnt/c/Users/HP/Desktop/analysis.md"
   → Does the file exist? ✅
2. Check claim: hard_verify.py "the report shows 15% growth" --brief  
   → Is this factually true? (Claude verifies)
3. ONLY if BOTH pass → present to user
   If either fails → tell user: "Claim blocked by verification: [reason]"
```

## SETUP — ANTHROPIC_API_KEY

System B (Claude fact-checking) requires a real Anthropic API key.

### Where the key lives
- **File**: `/home/hp/.hermes/.env`
- **Line**: 491
- **Format**: `ANTHROPIC_API_KEY=sk-ant-api03...` (108 chars)

### How hard_verify.py loads it
The script tries two methods (see `_load_anthropic_key()` in hard_verify.py):
1. Environment variable `ANTHROPIC_API_KEY`
2. Fallback: reads `/home/hp/.hermes/.env` directly, finds the line containing `ANTHROPIC_API_KEY`, splits on `=`

This means you do NOT need to export the key — it's read from the file on every invocation.

### Finding the correct model name
**CRITICAL:** Model IDs are account-specific. Do NOT guess. Always query first:

```bash
curl -s https://api.anthropic.com/v1/models \
  -H "x-api-key: $(grep ANTHROPIC /home/hp/.hermes/.env | cut -d= -f2)" \
  -H "anthropic-version: 2023-06-01" | python3 -c "import json,sys; [print(m['id']) for m in json.load(sys.stdin)['data']]"
```

**Current available models (this account, July 2026):**
- `claude-sonnet-4-5-20250929` ← Used for fact verification (System B)
- `claude-haiku-4-5-20251001` ← Used for sanity checks
- `claude-sonnet-5`, `claude-opus-4-8`, `claude-opus-4-7`, etc.

The format is `claude-{variant}-{major}-{minor}-{date}` — NOT `claude-{major}-{minor}-{variant}-{date}` like older Anthropic models.

### Editing the key
```bash
nano /home/hp/.hermes/.env
# Ctrl+_ then 491 then Enter → jump to line 491
# Ctrl+X, Y, Enter to save
```
Or from Windows: `\\wsl$\Ubuntu\home\hp\.hermes\.env` in Notepad.

## WHEN CLAUDE API IS UNAVAILABLE

If ANTHROPIC_API_KEY is not set:
- Infrastructure verification (System A) still works — ALWAYS use it
- Pattern checks (future dates, contradictions, known falsehoods) still work — ALWAYS use them
- For factual claims: mark as UNVERIFIABLE and TELL THE USER
  - Format: "⚠️ Unverified claim: [claim]. ANTHROPIC_API_KEY not available for verification."
  - NEVER present an unverified factual claim as if it were confirmed

## ESCAPE HATCH (ONLY WHEN USER EXPLICITLY REQUESTS)

The user may say:
- "--no-verify" → Skip verification for this claim only
- "skip verification" → Skip for this response only  
- "just tell me, don't check" → Skip for this response only

Any other wording does NOT bypass verification. "I think", "probably", "maybe" are NOT escape hatches — they're red flags that the claim NEEDS verification.

## PRESENTING VERIFIED CLAIMS

When a claim passes verification, present it with confidence:

```
✅ VERIFIED (92%): Python was created by Guido van Rossum in 1991
```

When a claim is blocked, be honest:

```
🔴 BLOCKED: That claim references a future date (2030). Cannot verify.
```

When verification is unavailable:

```
⚠️ UNABLE TO VERIFY (no API key): "Python 3.14 will include pattern matching"
```

## PITFALLS

- **Do NOT skip verification because "it's common knowledge."** Common knowledge is often wrong.
- **Do NOT present a blocked claim with softened language.** "Might be", "possibly", "some sources say" are weasel words. If blocked, say it's blocked.
- **Do NOT run verification and then ignore the result.** If hard_verify.py says FAILED, do NOT present the claim.
- **File paths on Windows Desktop:** Always use `/mnt/c/Users/HP/Desktop/...` not `C:\Users\...`
- **Commands with pipes/redirects:** Quote them: `--ref "cmd:python script.py 2>&1"`

### ⚠️ PITFALL — `***` string corruption in write_file/patch

The `***` masking in `.env` files will CORRUPT Python string literals when referenced in code via `write_file` or `patch`. The string `'ANTHROPIC_API_KEY=***` breaks the parser and produces SyntaxError: unterminated string literal.

**DO NOT write code that contains the literal string `ANTHROPIC_API_KEY=*** Use these safe patterns instead:

```python
# SAFE: check for substring
if "ANTHROPIC_API_KEY" in line and not line.strip().startswith("#"):
    val = line.strip().split("=", 1)
    if len(val) == 2 and len(val[1]) > 10:
        return val[1]

# SAFE: use startswith with just the prefix
if line.startswith("ANTHROPIC_API_KEY"):
    ...

# SAFE: regex without the value
if re.match(r'^ANTHROPIC_API_KEY=', line):
    ...
```

The corruption happens because `***` is a multi-line comment token in some parsers, and `write_file`/`patch` process the content before writing. This caused hard_verify.py to be corrupted 3+ times in one session before the fix was found.

### ⚠️ PITFALL — Do NOT declare API key missing without testing

The `grep` output `ANTHROPIC_API_KEY=*** is TERMINAL MASKING, not the actual file content. The file contains a real 108-character key starting with `sk-ant-`. Before telling the user "key is missing" or "key is masked":

1. Read the key with `python3 -c "..."` — Python reads the file directly, bypassing terminal masking
2. Test the key against the API — a 404 on model name means the KEY WORKS but the model name is wrong; a 401 means the key is invalid
3. Query `/v1/models` to discover available model names
4. Only report "key missing" if the key actually fails authentication (401/403), not if it's a model name issue (404)

In this session, the key was working the entire time. The model name was wrong (`claude-sonnet-4-20250514` → should be `claude-sonnet-4-5-20250929`). Telling the user "your key is masked, add it" when it was already there eroded trust.

## REMEMBER

This skill is **auto_load: true** and **priority: critical**. You follow it on EVERY message, EVERY response, EVERY claim. It's not a suggestion. It's the gate between you and the user, and nothing unverified gets through.
