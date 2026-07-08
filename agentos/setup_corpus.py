#!/usr/bin/env python3
"""
AgentOS - Corpus Setup

Copies .md files from claude_history (and optionally dossiers/) into agentos/corpus/,
then runs `state_layer.py index` to rebuild the manifest.

Usage:
  python setup_corpus.py
"""

import shutil
import subprocess
import sys
from pathlib import Path

AGENTOS_DIR = Path(__file__).resolve().parent
CORPUS_DIR = AGENTOS_DIR / "corpus"
CLAUDE_HISTORY_DIR = Path(r"C:\Users\HP\Desktop\zayed-master\claude_history")
DOSSIERS_DIR = Path(r"C:\Users\HP\Desktop\zayed-dossiers\vault\04-KNOWLEDGE\dossiers")


def copy_dir(src_dir, label, corpus_dir):
    """Copy all .md files from src_dir into corpus_dir. Returns (count, bytes)."""
    copied, total_size = 0, 0
    for src in sorted(src_dir.glob("*.md")):
        dst = corpus_dir / src.name
        shutil.copy2(src, dst)
        copied += 1
        total_size += src.stat().st_size
        print(f"  copied ({label}): {src.name}")
    return copied, total_size


def main():
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)

    copied = 0
    total_size = 0

    if CLAUDE_HISTORY_DIR.exists():
        n, s = copy_dir(CLAUDE_HISTORY_DIR, "claude_history", CORPUS_DIR)
        copied += n
        total_size += s
    else:
        print(f"WARNING: claude_history not found at {CLAUDE_HISTORY_DIR}")

    if DOSSIERS_DIR.exists():
        n, s = copy_dir(DOSSIERS_DIR, "dossiers", CORPUS_DIR)
        copied += n
        total_size += s
    else:
        print(f"WARNING: dossiers folder not found at {DOSSIERS_DIR}")

    print(f"\nCopied {copied} files ({total_size / 1024:.1f} KB total)")

    print("\nIndexing corpus...")
    result = subprocess.run(
        [sys.executable, str(AGENTOS_DIR / "state_layer.py"), "index"],
        cwd=str(AGENTOS_DIR),
        capture_output=True,
        text=True,
    )
    output = (result.stdout or result.stderr or "").strip()
    print(output or "Done.")
    print(f"\nSetup complete: {copied} files indexed.")


if __name__ == "__main__":
    main()
