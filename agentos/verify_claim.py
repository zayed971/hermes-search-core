#!/usr/bin/env python3
"""
AgentOS - Hermes HQ Phase 1 (P1-A): verify_claim

The first piece of the verification spine. Nothing in the framework is
believed without an artifact - this module takes the artifact_ref named by a
claim and independently checks it. It does not trust whoever wrote the claim.

artifact_ref format is "<scheme>:<value>":
  file:<path>     -> path must exist as a file; reports stat (size, mtime) + sha256.
  cmd:<command>   -> command is run; reports exit code + stdout/stderr tail.
  url:<url>       -> URL is fetched; reports HTTP status (2xx/3xx = ok).
A scheme-less ref is auto-detected: http(s):// -> url, anything else -> file.
Commands always need the explicit "cmd:" prefix - a bare string is ambiguous
between "a path" and "a command", and guessing wrong here is exactly the kind
of unverified claim this module exists to prevent.

Stdlib only (urllib instead of curl) - no extra dependencies, consistent with
the rest of AgentOS (see state_layer.py).

CLI:
  python verify_claim.py file:C:/path/to/thing.txt
  python verify_claim.py "cmd:python script.py --check"
  python verify_claim.py url:https://example.com/health
"""

import argparse
import hashlib
import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_COMMAND_TIMEOUT = 30
DEFAULT_URL_TIMEOUT = 10
TAIL_CHARS = 500


def _split_ref(artifact_ref):
    """Return (kind, value) for an artifact_ref string."""
    ref = (artifact_ref or "").strip()
    for scheme in ("file", "cmd", "url"):
        prefix = scheme + ":"
        if ref.startswith(prefix):
            return scheme, ref[len(prefix):].strip()
    if ref.startswith("http://") or ref.startswith("https://"):
        return "url", ref
    return "file", ref


def _tail(text, n=TAIL_CHARS):
    return (text or "")[-n:]


def _verify_file(path_str):
    path = Path(path_str)
    if not path.is_file():
        return False, f"file not found: {path_str}"
    stat = path.stat()
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    return True, (
        f"file exists: {path_str} size={stat.st_size}B "
        f"mtime={stat.st_mtime:.0f} sha256={sha256}"
    )


def _verify_command(command, timeout=DEFAULT_COMMAND_TIMEOUT):
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return False, f"command timed out after {timeout}s: {command}"
    except OSError as e:
        return False, f"command failed to start: {command} ({e})"
    ok = result.returncode == 0
    evidence = (
        f"exit={result.returncode} "
        f"stdout_tail={_tail(result.stdout)!r} stderr_tail={_tail(result.stderr)!r}"
    )
    return ok, evidence


def _verify_url(url, timeout=DEFAULT_URL_TIMEOUT):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"url unreachable: {url} ({e})"
    ok = 200 <= status < 400
    return ok, f"status={status} url={url}"


def verify_claim(artifact_ref, command_timeout=DEFAULT_COMMAND_TIMEOUT,
                  url_timeout=DEFAULT_URL_TIMEOUT):
    """Verify a single artifact_ref. Returns {"ok": bool, "evidence": str}."""
    kind, value = _split_ref(artifact_ref)
    if not value:
        return {"ok": False, "evidence": "empty artifact_ref"}
    if kind == "file":
        ok, evidence = _verify_file(value)
    elif kind == "cmd":
        ok, evidence = _verify_command(value, timeout=command_timeout)
    elif kind == "url":
        ok, evidence = _verify_url(value, timeout=url_timeout)
    else:
        ok, evidence = False, f"unknown artifact_ref kind: {kind}"
    return {"ok": ok, "evidence": evidence}


def main():
    ap = argparse.ArgumentParser(description="Verify a claim's artifact_ref")
    ap.add_argument("artifact_ref", help='e.g. file:path, "cmd:command", url:https://...')
    args = ap.parse_args()
    print(json.dumps(verify_claim(args.artifact_ref), indent=2))


if __name__ == "__main__":
    main()
