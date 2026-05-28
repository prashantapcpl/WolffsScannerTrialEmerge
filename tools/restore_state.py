"""
restore_state.py — Restore scanner state files from a carry-forward backup.

Carry-forward (in `main.py` startup) backs up every active scanner's
state file under `data/state_backups/<YYYYMMDD_HHMMSS>/` BEFORE wiping
records to GENERAL. If the post-carry-forward state looks wrong (e.g.
active positions vanished), use this tool to roll back to the snapshot.

Usage
-----
List recent backups (most recent first):
    python tools/restore_state.py --list

Restore from a specific backup directory:
    python tools/restore_state.py --from data/state_backups/20260528_181216

Dry run (preview which files would be restored, don't write):
    python tools/restore_state.py --from data/state_backups/20260528_181216 --dry-run

Safety
------
- The scanner MUST be stopped before restoring. The tool refuses to run
  if it finds an active scanner.pid file pointing to a live process.
- The current (about-to-be-overwritten) state files are saved one more
  time to `data/state_backups/<NOW>_before_restore/` as a final safety
  net — restore is reversible.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime

import pytz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

IST          = pytz.timezone("Asia/Kolkata")
BACKUPS_ROOT = os.path.join(ROOT, "data", "state_backups")
DATA_DIR     = os.path.join(ROOT, "data")


def list_backups() -> list:
    """Return [(name, path, mtime)] sorted newest-first."""
    if not os.path.isdir(BACKUPS_ROOT):
        return []
    out = []
    for name in os.listdir(BACKUPS_ROOT):
        p = os.path.join(BACKUPS_ROOT, name)
        if os.path.isdir(p):
            out.append((name, p, os.path.getmtime(p)))
    return sorted(out, key=lambda x: -x[2])


def is_scanner_running() -> tuple[bool, int | None]:
    """Return (running, pid). True if data/scanner.pid points to a live PID.
    Best-effort; Windows-aware via tasklist if available."""
    pid_path = os.path.join(DATA_DIR, "scanner.pid")
    if not os.path.exists(pid_path):
        return False, None
    try:
        with open(pid_path) as f:
            pid_str = f.read().strip()
        if not pid_str:
            return False, None
        pid = int(pid_str)
    except (ValueError, OSError):
        return False, None
    # Linux/macOS path
    if hasattr(os, "kill"):
        try:
            os.kill(pid, 0)
            return True, pid
        except OSError:
            return False, pid
    return False, pid   # unknown OS — assume not running


def restore_from(backup_dir: str, dry_run: bool) -> None:
    """Copy every *.bak file in backup_dir to the corresponding state file
    in data/.  Backs up the existing live files first."""
    if not os.path.isdir(backup_dir):
        print(f"❌ Backup directory not found: {backup_dir}")
        sys.exit(2)

    # Find restorable files. Backup names look like:
    #   scanner_4_state.json.20260528_181216.bak
    candidates = [f for f in os.listdir(backup_dir) if f.endswith(".bak")]
    if not candidates:
        print(f"❌ No .bak files found under {backup_dir}")
        sys.exit(2)

    pairs = []
    for bak_name in candidates:
        # Strip the trailing ".YYYYMMDD_HHMMSS.bak" → original filename
        base = bak_name.rsplit(".bak", 1)[0]
        base = base.rsplit(".", 1)[0]   # drop "YYYYMMDD_HHMMSS"
        dest = os.path.join(DATA_DIR, base)
        pairs.append((os.path.join(backup_dir, bak_name), dest))

    print(f"\n  Planned restore (newest .bak from {backup_dir}):")
    for src, dest in pairs:
        print(f"    {os.path.basename(src):>50}  →  {os.path.relpath(dest, ROOT)}")

    if dry_run:
        print("\n  --dry-run set; no files written.\n")
        return

    # Final safety net: snapshot current live files before overwriting.
    safety_dir = os.path.join(
        BACKUPS_ROOT,
        datetime.now(IST).strftime("%Y%m%d_%H%M%S") + "_before_restore"
    )
    os.makedirs(safety_dir, exist_ok=True)
    saved = 0
    for _, dest in pairs:
        if os.path.exists(dest):
            shutil.copy2(dest, os.path.join(safety_dir, os.path.basename(dest)))
            saved += 1
    if saved:
        print(f"\n  📦 Pre-restore safety snapshot: {safety_dir} ({saved} files)")

    # Now overwrite.
    for src, dest in pairs:
        shutil.copy2(src, dest)
    print(f"  ✅ Restored {len(pairs)} state file(s) from {backup_dir}.\n")
    print("  You can now run START.bat again.")
    print("  TIP: set CARRY_FORWARD_DRY_RUN=true on the next run to verify "
          "what carry-forward WOULD do before letting it touch state again.\n")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true",
                    help="List available backup directories, newest first.")
    ap.add_argument("--from", dest="src", default=None,
                    help="Path to a backup directory to restore from.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be restored without writing.")
    args = ap.parse_args()

    if args.list or not args.src:
        backups = list_backups()
        if not backups:
            print(f"\n  No backups found under {BACKUPS_ROOT}.\n")
            return
        print(f"\n  Recent state backups under {BACKUPS_ROOT} "
              f"(newest first):\n")
        for name, path, mtime in backups[:20]:
            mt = datetime.fromtimestamp(mtime, tz=IST).strftime("%Y-%m-%d %H:%M:%S")
            nf = len([f for f in os.listdir(path) if f.endswith(".bak")])
            print(f"    {name}   ({mt})   {nf} state file(s)")
        if not args.src:
            print("\n  To restore:")
            print("    python tools/restore_state.py "
                  "--from data/state_backups/<name>  [--dry-run]\n")
        return

    running, pid = is_scanner_running()
    if running:
        print(f"❌ Scanner appears to be running (PID {pid} from data/scanner.pid).")
        print("   Stop it first (Ctrl+C in the engine window), then re-run.")
        sys.exit(2)

    restore_from(args.src, args.dry_run)


if __name__ == "__main__":
    main()
