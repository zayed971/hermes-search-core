#!/usr/bin/env python3
"""
persona_backup.py — Save Finch before the machine forgets.
==========================================================
Usage:
  python persona_backup.py              # backup current config
  python persona_backup.py --restore N  # restore backup N
  python persona_backup.py --list       # show all backups

One wrong `hermes config set personality` wipes everything.
This backs up the full config before every change.
"""

import os, sys, json, shutil
from datetime import datetime
from pathlib import Path

BACKUP_DIR = Path(os.path.expanduser("~/.hermes/backups/persona"))
CONFIG_PATH = Path(os.path.expanduser("~/.hermes/config.yaml"))


def backup():
    """Save current config with timestamp."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    
    if not CONFIG_PATH.exists():
        print("❌ No config.yaml found")
        return None
    
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"config_{ts}.yaml"
    shutil.copy2(CONFIG_PATH, dest)
    
    # Also save a JSON snapshot for easy reading
    try:
        import yaml
        with open(CONFIG_PATH) as f:
            data = yaml.safe_load(f)
        personality = data.get("personality", "(empty)")
        model = data.get("model", "(unknown)")
        
        meta = {
            "timestamp": ts,
            "personality_preview": str(personality)[:200],
            "model": str(model),
            "file": str(dest)
        }
        # Keep a manifest
        manifest = BACKUP_DIR / "manifest.json"
        entries = []
        if manifest.exists():
            entries = json.loads(manifest.read_text())
        entries.append(meta)
        manifest.write_text(json.dumps(entries, indent=2))
        
        print(f"✅ Backup {ts}")
        print(f"   Personality: {str(personality)[:80]}...")
        print(f"   Model: {model}")
        print(f"   Saved: {dest}")
        return str(dest)
    except Exception as e:
        print(f"⚠️ Backup saved but metadata failed: {e}")
        return str(dest)


def list_backups():
    """Show all saved backups."""
    if not BACKUP_DIR.exists():
        print("No backups yet.")
        return
    
    manifest = BACKUP_DIR / "manifest.json"
    if manifest.exists():
        entries = json.loads(manifest.read_text())
        for i, e in enumerate(entries, 1):
            print(f"[{i}] {e['timestamp']} | {e['personality_preview'][:60]}...")
        print(f"\n{len(entries)} backups total. Restore: python persona_backup.py --restore <N>")
    else:
        files = sorted(BACKUP_DIR.glob("config_*.yaml"))
        for i, f in enumerate(files, 1):
            print(f"[{i}] {f.name}")
        print(f"\n{len(files)} backups.")


def restore(index: int):
    """Restore config from backup N."""
    manifest = BACKUP_DIR / "manifest.json"
    if manifest.exists():
        entries = json.loads(manifest.read_text())
        if index < 1 or index > len(entries):
            print(f"❌ Invalid index. Choose 1-{len(entries)}")
            return
        src = entries[index-1]["file"]
    else:
        files = sorted(BACKUP_DIR.glob("config_*.yaml"))
        if index < 1 or index > len(files):
            print(f"❌ Invalid index. Choose 1-{len(files)}")
            return
        src = str(files[index-1])
    
    if not Path(src).exists():
        print(f"❌ Backup file missing: {src}")
        return
    
    # Backup current before restoring
    backup()
    shutil.copy2(src, CONFIG_PATH)
    print(f"✅ Restored from [{index}] {Path(src).name}")
    print("   Current config was backed up first.")


if __name__ == "__main__":
    if "--restore" in sys.argv:
        try:
            idx = int(sys.argv[sys.argv.index("--restore") + 1])
            restore(idx)
        except (ValueError, IndexError):
            print("Usage: python persona_backup.py --restore <N>")
    elif "--list" in sys.argv or "-l" in sys.argv:
        list_backups()
    else:
        backup()
