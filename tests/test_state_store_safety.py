"""
test_state_store_safety.py — Atomic save + backup_to helpers.

Run: python tests/test_state_store_safety.py
"""
import os
import sys
import json
import tempfile
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Pick the right StateStore — there's a base one used by Scanner 1/2/3 in
# core/state_store and a Strategy-4-specific one in strategies/strategy4.
# Both need atomic save. Test the base one (Strategy 4 imports it for the
# atomic-write semantics via inheritance pattern in their save() code).
from core.state_store import StateStore  # noqa: E402  (lives in this module)


def assert_(cond, msg):
    print(f"  {'✅' if cond else '❌'}  {msg}")
    if not cond:
        sys.exit(1)


def main():
    print("\n  StateStore safety helpers — atomic save + backup_to\n")

    tmp = tempfile.mkdtemp(prefix="wolffs_test_")
    try:
        state_file = os.path.join(tmp, "scanner_test_state.json")
        store      = StateStore(state_file)

        # Empty store should save cleanly.
        store.save()
        assert_(os.path.exists(state_file), "save() writes the state file")

        # The file should be a valid JSON.
        data = json.load(open(state_file))
        assert_("records" in data and "signal_log" in data,
                "saved file has expected top-level keys")

        # No leftover .tmp after a successful save.
        leftovers = [f for f in os.listdir(tmp) if f.endswith(".tmp")]
        assert_(not leftovers, "no .tmp leftover after successful save")

        # backup_to copies the file to a timestamped name in the backup dir.
        backup_dir = os.path.join(tmp, "backups")
        path       = store.backup_to(backup_dir)
        assert_(path and os.path.exists(path),
                f"backup_to returned a real file: {path}")
        assert_(os.path.basename(path).endswith(".bak"),
                "backup filename ends with .bak")

        # Removing the live file → backup_to is a quiet no-op (empty string).
        os.unlink(state_file)
        path = store.backup_to(backup_dir)
        assert_(path == "",
                "backup_to returns empty string when no state file exists")

        # Recreate and save again — should still work.
        store.save()
        assert_(os.path.exists(state_file),
                "save() works after backup-then-delete cycle")

        print("\n  ✅ All StateStore safety checks passed.\n")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
